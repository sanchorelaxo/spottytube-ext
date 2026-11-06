#!/usr/bin/env python3
"""Spotykach USB transfer transport — host side, stdlib only.

The device speaks a framed request/reply protocol over its USB CDC serial port
(0483:5740). To get there it first has to be switched out of USB-MIDI mode with
a SysEx message; the firmware then re-enumerates as a serial port, which this
module opens directly.

    SYSEX_ENTER  F0 7D 53 4B 01 F7        (sent to the MIDI port)
    SYSEX_REPLY  F0 7D 53 4B 02 SS F7     (SS = 0 ok, 1 busy, 2 no card)

Frames:

    'S' 'K' type seq  len:u32le  crc:u32le  payload[len]
    crc = zlib.crc32(header[2:8] + payload)
    a reply is type|0x80, same seq, payload[0] = status

Audio goes in through UPLOAD_BEGIN / UPLOAD_DATA* / UPLOAD_COMMIT. The device
writes a temp file and only replaces the slot after the CRC and the header
check, so a failed upload keeps the previous sample.

No third-party modules: the serial port is a raw termios file and MIDI is
driven through alsa-utils' ``amidi``, which every ALSA install ships.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import select
import struct
import subprocess
import termios
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

USB_VENDOR_ID = 0x0483
USB_PRODUCT_ID = 0x5740

PROTO_MAJOR = 1
PROTO_MINOR = 2

SYSEX_ENTER = bytes([0xF0, 0x7D, 0x53, 0x4B, 0x01, 0xF7])
SYSEX_REPLY_PREFIX = bytes([0xF0, 0x7D, 0x53, 0x4B, 0x02])

MAGIC = b"SK"
HEADER_SIZE = 12
MAX_PAYLOAD = 32768 + 16
RESPONSE_FLAG = 0x80

CMD = {
    "hello": 0x01,
    "ping": 0x02,
    "list": 0x03,
    "upload_begin": 0x10,
    "upload_data": 0x11,
    "upload_commit": 0x12,
    "upload_abort": 0x13,
    "download_open": 0x20,
    "download_read": 0x21,
    "download_close": 0x22,
    "remove": 0x30,
    "exit": 0x7F,
}

STATUS = {
    0: "OK",
    1: "transfer error (corrupted frame)",
    2: "unknown command, update the firmware",
    3: "device is busy with another operation",
    4: "invalid request",
    5: "SD card not available",
    6: "SD card read/write error",
    7: "slot is empty",
    8: "not enough free space on the SD card",
    9: "file is too large",
    10: "transfer out of sync",
    11: "upload corrupted (checksum mismatch)",
    12: "device rejected the file format",
}

# Upload retry policy, mirrors the firmware's tolerance for lost replies.
MAX_UPLOAD_RETRIES = 5
UPLOAD_DATA_TIMEOUT = 4.0
REQUEST_TIMEOUT = 5.0
DEVICE_QUIET = 0.25

DEVICE_FORMAT = {"sample_rate": 48000, "channels": 2, "bits": 32, "format": 3}


class DeviceNotFound(RuntimeError):
    """No Spotykach serial port and no way to make one appear."""


class DeviceError(RuntimeError):
    """The device answered with a status other than OK."""

    def __init__(self, status, reply=None, message=None):
        super().__init__(message or STATUS.get(status, f"Spotykach error {status}"))
        self.status = status
        self.reply = reply


class TransferTimeout(RuntimeError):
    """No complete reply in time."""


# --- locating the device --------------------------------------------------


def _usb_ids(sys_path):
    """(idVendor, idProduct) of the USB device behind a /sys tty entry."""
    for candidate in (Path(sys_path) / "device" / "..", Path(sys_path) / "device"):
        try:
            vendor = int((candidate / "idVendor").read_text().strip(), 16)
            product = int((candidate / "idProduct").read_text().strip(), 16)
        except (OSError, ValueError):
            continue
        return vendor, product
    return None


def _node_ready(path, mode=os.R_OK | os.W_OK):
    """True once the node exists *and* our user may open it.

    The kernel publishes /dev/ttyACMn before udev has applied the mode bits, so
    a freshly enumerated port can be visible yet still 0600 for a few
    milliseconds. Opening in that window raises EACCES, so poll instead.
    """
    try:
        return os.access(path, mode)
    except OSError:
        return False


def find_serial_port(wait=0.0):
    """The device's serial port in transfer mode, or None.

    ``wait`` seconds allows for udev still running its rules on a fresh node.
    ``SPOTTY_USB_PORT`` wins, which is how the tests point this at a pty.
    """
    override = os.environ.get("SPOTTY_USB_PORT")
    if override:
        if not Path(override).exists():
            return None
        return override if _node_ready(override) else None

    deadline = time.monotonic() + max(wait, 0.0)
    while True:
        for sys_path in sorted(glob.glob("/sys/class/tty/ttyACM*")):
            ids = _usb_ids(sys_path)
            if ids != (USB_VENDOR_ID, USB_PRODUCT_ID):
                continue
            node = "/dev/" + Path(sys_path).name
            if _node_ready(node):
                return node
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


_AMIDI_LINE = re.compile(r"^(?:IO|I|O)\s+(\S+)\s+(.*)$")


def find_midi_port():
    """The ALSA rawmidi port of a Spotykach in MIDI mode, or None."""
    amidi = shutil.which("amidi")
    if amidi is None:
        return None
    try:
        out = subprocess.run([amidi, "-l"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stdout.splitlines()[1:]:
        match = _AMIDI_LINE.match(line.strip())
        if not match:
            continue
        device, name = match.groups()
        if "spotykach" in name.lower():
            return device
    return None


def parse_sysex_reply(data):
    """Status byte from an ``F0 7D 53 4B 02 SS F7`` reply, or None."""
    index = data.find(SYSEX_REPLY_PREFIX)
    while index >= 0:
        if len(data) >= index + 7 and data[index + 6] == 0xF7:
            return data[index + 5]
        index = data.find(SYSEX_REPLY_PREFIX, index + 1)
    return None


def enter_transfer_mode(midi_port=None, timeout=4.0):
    """Send the SysEx that switches the device to serial transfer mode.

    Returns the reply status (0 = ok). Raises DeviceNotFound when there is no
    MIDI port to talk to, DeviceError when the device refuses.
    """
    amidi = shutil.which("amidi")
    if amidi is None:
        raise DeviceNotFound("alsa-utils is missing (needs amidi)")

    port = midi_port or find_midi_port()
    if not port:
        raise DeviceNotFound(
            "no Spotykach USB MIDI port — connect it through the rear USB-C port"
        )

    reader = None
    try:
        reader = subprocess.Popen([amidi, "-p", port, "-d"], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL)
        time.sleep(0.15)
        subprocess.run([amidi, "-p", port, "-S", SYSEX_ENTER.hex().upper()],
                       capture_output=True, timeout=5)
        deadline = time.monotonic() + timeout
        buffer = b""
        while time.monotonic() < deadline:
            if find_serial_port(wait=0.2):
                return 0
            chunk = _read_available(reader, 0.2)
            if chunk:
                buffer += chunk
                status = parse_sysex_reply(buffer)
                if status is not None:
                    if status != 0:
                        raise DeviceError(status, message={
                            1: "Spotykach is busy (loading, saving or recording)",
                            2: "no SD card in Spotykach",
                        }.get(status, f"Spotykach refused transfer mode ({status})"))
                    return status
    finally:
        if reader is not None:
            reader.kill()
            reader.wait(timeout=5)

    if find_serial_port():
        return 0
    raise TransferTimeout("Spotykach didn't answer the transfer-mode request")


def _read_available(process, seconds):
    if process.stdout is None:
        return b""
    ready, _, _ = select.select([process.stdout], [], [], seconds)
    if not ready:
        return b""
    return os.read(process.stdout.fileno(), 4096)


# --- serial port ----------------------------------------------------------


class SerialPort:
    """A raw termios serial port; CDC ACM ignores the baud rate."""

    def __init__(self, path, baud=termios.B115200):
        self.path = str(path)
        self.fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attrs = termios.tcgetattr(self.fd)
            attrs[0] = 0                      # iflag: no translation
            attrs[1] = 0                      # oflag: no post-processing
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[3] = 0                      # lflag: raw
            attrs[4] = baud
            attrs[5] = baud
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            termios.tcflush(self.fd, termios.TCIOFLUSH)
        except termios.error:
            os.close(self.fd)
            raise

    def write(self, data):
        view = memoryview(data)
        while view:
            try:
                written = os.write(self.fd, view)
            except BlockingIOError:
                select.select([], [self.fd], [], 1.0)
                continue
            view = view[written:]

    def read(self, seconds):
        ready, _, _ = select.select([self.fd], [], [], seconds)
        if not ready:
            return b""
        try:
            return os.read(self.fd, 65536)
        except BlockingIOError:
            return b""

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


# --- protocol -------------------------------------------------------------


@dataclass
class DeviceInfo:
    proto_major: int
    proto_minor: int
    tapes: int
    slots: int
    tape_names: str
    max_chunk: int
    max_frames: int
    max_cues: int
    total_kib: int
    free_kib: int
    firmware: str

    @property
    def max_bytes(self):
        return self.max_frames * DEVICE_FORMAT["channels"] * (DEVICE_FORMAT["bits"] // 8)


@dataclass
class SlotInfo:
    tape: str
    slot: int
    flags: int
    file_size: int
    frames: int
    sample_rate: int
    cues: int
    channels: int
    bits: int
    name: str

    occupied = property(lambda self: bool(self.flags & 0x01))


def build_frame(seq, cmd, payload=b""):
    header = MAGIC + bytes([cmd, seq]) + struct.pack("<I", len(payload))
    return header + struct.pack("<I", zlib.crc32(header[2:] + payload)) + payload


def parse_frames(buffer):
    """Split a byte stream into complete, CRC-checked (type, seq, payload) frames."""
    frames = []
    while len(buffer) >= HEADER_SIZE:
        if buffer[:2] != MAGIC:
            buffer = buffer[1:]
            continue
        length = struct.unpack("<I", buffer[4:8])[0]
        if length > MAX_PAYLOAD:
            buffer = buffer[1:]
            continue
        end = HEADER_SIZE + length
        if len(buffer) < end:
            break
        raw = buffer[:end]
        buffer = buffer[end:]
        if struct.unpack("<I", raw[8:12])[0] != zlib.crc32(raw[2:8] + raw[HEADER_SIZE:]):
            continue
        frames.append((raw[2], raw[3], raw[HEADER_SIZE:]))
    return frames, buffer


class SkLink:
    """One open connection to a device in transfer mode."""

    def __init__(self, port, timeout=REQUEST_TIMEOUT):
        self.port = port if isinstance(port, SerialPort) else SerialPort(port)
        self.timeout = timeout
        self.seq = 0
        self.buffer = b""
        self.info = None

    @classmethod
    def open(cls, path=None, timeout=REQUEST_TIMEOUT):
        """Open the device, inventing a serial port by switching modes if needed."""
        port = path or find_serial_port(wait=0.0)
        if not port:
            try:
                enter_transfer_mode()
            except DeviceError:
                raise
            port = find_serial_port(wait=6.0)
        if not port:
            raise DeviceNotFound("Spotykach serial port did not appear")
        return cls(port, timeout=timeout)

    # -- framing --

    def _read_frames(self, deadline):
        frames = []
        while time.monotonic() < deadline:
            chunk = self.port.read(min(0.2, max(0.0, deadline - time.monotonic())))
            if not chunk:
                continue
            self.buffer += chunk
            parsed, self.buffer = parse_frames(self.buffer)
            frames.extend(parsed)
            if parsed:
                break
        return frames

    def request(self, cmd, payload=b"", timeout=None):
        """Send one request, return the reply payload (status byte first)."""
        self.seq = (self.seq + 1) & 0xFF
        self.port.write(build_frame(self.seq, cmd, payload))
        deadline = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < deadline:
            for frame_type, seq, payload_reply in self._read_frames(deadline):
                if frame_type == 0x80:  # frame error, never a real reply
                    raise DeviceError(payload_reply[0] if payload_reply else 1,
                                      reply=payload_reply,
                                      message="Spotykach rejected the request frame")
                if frame_type != (cmd | RESPONSE_FLAG) or seq != self.seq:
                    continue
                if not payload_reply or payload_reply[0] != 0:
                    status = payload_reply[0] if payload_reply else 1
                    raise DeviceError(status, reply=payload_reply)
                return payload_reply
        raise TransferTimeout(f"no reply to command 0x{cmd:02x}")

    # -- commands --

    def hello(self):
        payload = self.request(CMD["hello"], timeout=10)
        firmware_length = payload[34] if len(payload) > 34 else 0
        info = DeviceInfo(
            proto_major=payload[1], proto_minor=payload[2],
            tapes=payload[3], slots=payload[4],
            tape_names=payload[5:11].decode("ascii", "replace"),
            max_chunk=struct.unpack("<I", payload[14:18])[0],
            max_frames=struct.unpack("<I", payload[18:22])[0],
            max_cues=struct.unpack("<H", payload[22:24])[0],
            total_kib=struct.unpack("<I", payload[26:30])[0],
            free_kib=struct.unpack("<I", payload[30:34])[0],
            firmware=payload[35:35 + firmware_length].decode("ascii", "replace"),
        )
        if info.proto_major != PROTO_MAJOR:
            raise DeviceError(
                4, message=(f"Spotykach speaks transfer protocol "
                            f"{info.proto_major}.{info.proto_minor}; this tool "
                            f"supports {PROTO_MAJOR}.x"))
        self.info = info
        return info

    def list_slots(self):
        payload = self.request(CMD["list"], timeout=15)
        slots, offset = [], 2
        for _ in range(payload[1]):
            name_length = payload[offset + 3]
            slots.append(SlotInfo(
                tape=chr(payload[offset]),
                slot=payload[offset + 1],
                flags=payload[offset + 2],
                file_size=struct.unpack("<I", payload[offset + 4:offset + 8])[0],
                frames=struct.unpack("<I", payload[offset + 8:offset + 12])[0],
                sample_rate=struct.unpack("<I", payload[offset + 12:offset + 16])[0],
                cues=struct.unpack("<H", payload[offset + 16:offset + 18])[0],
                channels=payload[offset + 18],
                bits=payload[offset + 19],
                name=payload[offset + 20:offset + 20 + name_length].decode("ascii", "replace"),
            ))
            offset += 20 + name_length
        return slots

    def upload(self, tape, slot, data, progress=None):
        """Write WAV bytes into a slot; the old sample survives a failed upload."""
        if self.info is None:
            self.hello()
        problems = check_uploadable(data, self.info)
        if problems:
            raise ValueError("Spotykach can't load this file: " + " ".join(problems))

        begin = struct.pack("<BBHII", tape, slot, 0, len(data), zlib.crc32(data))
        self.request(CMD["upload_begin"], begin, timeout=10)
        try:
            for offset in range(0, len(data), self.info.max_chunk):
                chunk = data[offset:offset + self.info.max_chunk]
                self._send_chunk(offset, chunk)
                if progress:
                    progress(min(offset + len(chunk), len(data)) / len(data))
            self.request(CMD["upload_commit"], timeout=10)
        except Exception:
            try:
                self.request(CMD["upload_abort"])
            except Exception:
                pass
            raise
        return len(data)

    def _send_chunk(self, offset, chunk):
        payload = struct.pack("<I", offset) + chunk
        for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
            try:
                self.request(CMD["upload_data"], payload, timeout=UPLOAD_DATA_TIMEOUT)
                return
            except (DeviceError, TransferTimeout) as error:
                device_offset = None
                if isinstance(error, DeviceError) and error.reply and len(error.reply) >= 8:
                    device_offset = struct.unpack("<I", error.reply[4:8])[0]
                if device_offset == offset + len(chunk):
                    return  # written; only the reply was lost
                retriable = (isinstance(error, TransferTimeout)
                             or (isinstance(error, DeviceError) and error.status in (1, 11)))
                if not retriable or attempt == MAX_UPLOAD_RETRIES:
                    raise
                time.sleep(DEVICE_QUIET)
                self.buffer = b""

    def download(self, tape, slot):
        """The slot's bytes, byte-exact (reads are sector aligned)."""
        opened = self.request(CMD["download_open"], bytes([tape, slot, 0, 0]))
        size = struct.unpack("<I", opened[4:8])[0]
        out = bytearray()
        try:
            while len(out) < size:
                wanted = min(self.info.max_chunk if self.info else 4096, size - len(out))
                args = struct.pack("<II", len(out), wanted)
                reply = self.request(CMD["download_read"], args, timeout=5)
                out += reply[4:4 + wanted]
        finally:
            try:
                self.request(CMD["download_close"])
            except Exception:
                pass
        return bytes(out)

    def remove(self, tape, slot):
        """Erase the sample in a slot."""
        self.request(CMD["remove"], bytes([tape, slot, 0, 0]), timeout=15)

    def close(self):
        try:
            self.request(CMD["exit"], timeout=1.0)
        except Exception:
            pass
        self.port.close()

    def __enter__(self):
        self.hello()
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# --- WAV checks -----------------------------------------------------------


def inspect_wav(data):
    """Format fields of a RIFF/WAVE byte string, or None if it isn't one."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    info = {"format": 0, "channels": 0, "sample_rate": 0, "bits": 0,
            "data_offset": -1, "data_size": 0, "frames": 0}
    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset:offset + 4]
        chunk_size = struct.unpack("<I", data[offset + 4:offset + 8])[0]
        body = offset + 8
        if chunk_id == b"fmt " and chunk_size >= 16:
            (info["format"], info["channels"], info["sample_rate"]) = struct.unpack(
                "<HHI", data[body:body + 8])
            info["bits"] = struct.unpack("<H", data[body + 14:body + 16])[0]
            if info["format"] == 0xFFFE:  # WAVE_FORMAT_EXTENSIBLE: real tag at +24
                info["format"] = struct.unpack("<H", data[body + 24:body + 26])[0]
        elif chunk_id == b"data":
            info["data_offset"] = body
            info["data_size"] = min(chunk_size, len(data) - body)
        offset = body + chunk_size + (chunk_size & 1)
    block = max(info["channels"] * (info["bits"] // 8), 1)
    info["frames"] = info["data_size"] // block
    return info


def check_uploadable(data, info):
    """Why the device would refuse this file, as a list of sentences."""
    problems = []
    wav = inspect_wav(data)
    if wav is None:
        return ["it isn't a RIFF/WAVE file."]
    for key, wanted in (("sample_rate", DEVICE_FORMAT["sample_rate"]),
                        ("channels", DEVICE_FORMAT["channels"]),
                        ("bits", DEVICE_FORMAT["bits"]),
                        ("format", DEVICE_FORMAT["format"])):
        if wav[key] != wanted:
            problems.append(f"{key} is {wav[key]}, the device needs {wanted}.")
    if info and wav["frames"] > info.max_frames:
        problems.append(f"it holds {wav['frames']} frames, the deck takes "
                        f"{info.max_frames}.")
    return problems


__all__ = [
    "CMD", "DeviceError", "DeviceInfo", "DeviceNotFound", "SerialPort", "SkLink",
    "SlotInfo", "TransferTimeout", "build_frame", "check_uploadable",
    "enter_transfer_mode", "find_midi_port", "find_serial_port", "inspect_wav",
    "parse_frames", "parse_sysex_reply",
]
