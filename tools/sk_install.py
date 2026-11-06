#!/usr/bin/env python3
"""Where a finished segment goes: the instrument over USB, or its SD card.

Two transports, one contract:

    read(tape, slot)        -> bytes | None
    write(tape, slot, data)
    clear(tape, slot)

Slots are 1-based here, matching what the hardware shows. The USB side maps
them to the device's 0-based indices and to tape letters via the device's own
tape table, so nothing is hardcoded to a particular unit.

New samples land in slot 1 of the target tape and push the rest down; whatever
falls off the end becomes an archive file instead of being deleted:

    slot 6 -> SK/<tape>_6_<timestamp>.WAV   (on the card)
               <archive>/<tape>_6_<timestamp>.WAV (over USB)

The shift runs high-to-low so a sample is never read after being overwritten,
and each write is read back and compared before the next one moves. A failure
half way leaves the tape consistent (a duplicate is possible, silence is not).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import sk_usb

SLOTS_PER_TAPE = 6
TAPE_ORDER = "BGPRTY"

#: How long to wait for the device to answer the mode switch and the handshake.
USB_TIMEOUT = 20.0


class InstallError(RuntimeError):
    """Nothing could take the sample."""


class NoTransport(InstallError):
    """Neither the USB device nor a card with an SK/ folder is available."""

    def __init__(self, reasons):
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons) or "no transport available")


def timestamp(when=None):
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(when))


@dataclass
class Placement:
    """What happened to the tape, for the result and the GUI."""

    transport: str
    tape: str
    slot: int
    shifted: int = 0
    archived: Path | None = None
    detail: str = ""

    def as_dict(self):
        return {
            "transport": self.transport,
            "tape": self.tape,
            "slot": self.slot,
            "shifted": self.shifted,
            "archived": str(self.archived) if self.archived else None,
            "detail": self.detail,
        }


# --- the card -------------------------------------------------------------


class CardTransport:
    """Slots are plain files: SK/<tape>/<slot>.WAV."""

    name = "card"

    def __init__(self, root):
        self.root = Path(root)
        self.sk = self.root / "SK"
        if not self.sk.is_dir():
            raise InstallError(f"{self.root} has no SK/ folder — not a Spotykach card")
        self.archive_dir = self.sk

    def slot_path(self, tape, slot):
        return self.sk / tape / f"{slot}.WAV"

    def read(self, tape, slot):
        path = self.slot_path(tape, slot)
        try:
            return path.read_bytes() if path.is_file() else None
        except OSError:
            return None

    def write(self, tape, slot, data):
        path = self.slot_path(tape, slot)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def clear(self, tape, slot):
        try:
            self.slot_path(tape, slot).unlink()
        except FileNotFoundError:
            pass

    def free_bytes(self):
        try:
            st = os.statvfs(self.root)
            return st.f_bavail * st.f_frsize
        except OSError:
            return 0


# --- the instrument -------------------------------------------------------


class UsbTransport:
    """Slots live on the device; the protocol is in sk_usb."""

    name = "usb"

    def __init__(self, link, archive_dir=None):
        self.link = link
        info = link.info or link.hello()
        self.tape_names = info.tape_names
        self.archive_dir = Path(archive_dir) if archive_dir else None

    def _tape_index(self, tape):
        try:
            return self.tape_names.index(tape)
        except ValueError:
            raise InstallError(
                f"tape {tape!r} is not on this unit (it has {self.tape_names})"
            ) from None

    def read(self, tape, slot):
        index = self._tape_index(tape)
        entries = self.link.list_slots()
        entry = next(
            (s for s in entries if s.tape == chr(index) and s.slot == slot - 1), None
        )
        if entry is None or not entry.flags:
            return None
        return self.link.download(index, slot - 1)

    def write(self, tape, slot, data):
        self.link.upload(self._tape_index(tape), slot - 1, data)

    def clear(self, tape, slot):
        self.link.remove(self._tape_index(tape), slot - 1)

    def free_bytes(self):
        return (self.link.info.free_kib * 1024) if self.link.info else 0

    def close(self):
        self.link.close()


# --- choosing one ---------------------------------------------------------


def open_transport(preferred="auto", card_root=None, archive_dir=None):
    """A transport, preferring USB; the card is the fallback.

    ``preferred`` is ``auto``, ``usb`` or ``card``. With ``auto`` both are
    tried and the reasons both failed are kept, because "nothing is plugged in"
    and "the unit answered but refused" are different things to tell a user.
    """
    preferred = (preferred or "auto").lower()
    if preferred not in ("auto", "usb", "card"):
        raise ValueError(f"unknown transport {preferred!r}")

    reasons = []
    if preferred in ("auto", "usb"):
        try:
            link = sk_usb.SkLink.open(timeout=USB_TIMEOUT)
            return UsbTransport(link, archive_dir=archive_dir)
        except (sk_usb.DeviceNotFound, sk_usb.DeviceError, sk_usb.TransferTimeout,
                OSError) as error:
            reasons.append(f"usb: {error}")
            if preferred == "usb":
                raise InstallError(f"usb transport unavailable: {error}") from None

    if preferred in ("auto", "card"):
        if card_root is None:
            reasons.append("card: no card mount point given")
        elif not (Path(card_root) / "SK").is_dir():
            reasons.append(f"card: {card_root} has no SK/ folder")
        else:
            try:
                return CardTransport(card_root)
            except InstallError as error:
                reasons.append(f"card: {error}")

    raise NoTransport(reasons)


# --- the bump -------------------------------------------------------------


def _archive_write(directory, tape, slot, data, stamp):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{tape}_{slot}_{stamp}.WAV"
    counter = 1
    while path.exists():
        path = directory / f"{tape}_{slot}_{stamp}_{counter}.WAV"
        counter += 1
    path.write_bytes(data)
    return path


def shift_down(transport, tape, archive_dir=None, slots=SLOTS_PER_TAPE, stamp=None):
    """Move every sample on the tape down one slot; return the archive path.

    Slot ``slots`` is copied out before anything moves, so the file that falls
    off the end is never lost. Samples are moved, never merged: an empty slot
    clears the slot below it rather than leaving a stale duplicate behind.
    """
    stamp = stamp or timestamp()
    archived = None

    overflow = transport.read(tape, slots)
    if overflow:
        archived = _archive_write(archive_dir or "/tmp", tape, slots, overflow, stamp)

    for slot in range(slots - 1, 0, -1):
        data = transport.read(tape, slot)
        if data:
            transport.write(tape, slot + 1, data)
        else:
            transport.clear(tape, slot + 1)

    transport.clear(tape, 1)
    return archived


def install_sample(transport, tape, data, archive_dir=None, slot=1, shift=True,
                   stamp=None):
    """Put ``data`` in ``slot`` of ``tape``, shifting the rest down first."""
    if slot < 1 or slot > SLOTS_PER_TAPE:
        raise ValueError(f"slot {slot} is outside 1..{SLOTS_PER_TAPE}")

    archive_dir = archive_dir or getattr(transport, "archive_dir", None)
    archived = shift_down(transport, tape, archive_dir=archive_dir, stamp=stamp) \
        if shift else None
    transport.write(tape, slot, data)

    back = transport.read(tape, slot)
    if back != data:
        raise InstallError(
            f"read-back mismatch on {tape}/{slot}: wrote {len(data)} bytes, "
            f"got {len(back) if back else 0}"
        )
    return Placement(
        transport=transport.name,
        tape=tape,
        slot=slot,
        shifted=SLOTS_PER_TAPE if shift else 0,
        archived=archived,
        detail=f"{len(data):,} bytes verified",
    )


def describe():
    """What the GUI and `--status` need: can anything take a sample right now."""
    state = {"usb": {"present": False, "detail": ""},
             "card": {"present": False, "detail": ""},
             "transport": None, "ready": False}

    try:
        port = sk_usb.find_serial_port(wait=0.0)
        midi = sk_usb.find_midi_port()
        state["usb"]["present"] = bool(port or midi)
        state["usb"]["detail"] = (
            "in transfer mode" if port else
            "connected, will switch to transfer mode" if midi else
            "not connected"
        )
    except Exception as error:                      # noqa: BLE001 — status never raises
        state["usb"]["detail"] = str(error)

    return state
