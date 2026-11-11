#!/usr/bin/env python3
"""Where a finished segment goes: the instrument over USB, or its SD card.

Two transports, one contract:

    read(tape, slot)        -> bytes | None
    write(tape, slot, data)
    clear(tape, slot)

Slots are 1-based here, matching what the hardware shows. The USB side maps
them to the device's 0-based indices and to tape letters via the device's own
tape table, so nothing is hardcoded to a particular unit.

Samples are placed on the six-track chain ``B G P R T Y`` in two modes:

* ``bump``   — the new sample takes slot 1 of its tape and every occupied
  slot behind it moves one place down the chain (tape by tape, in the
  device's own order). Only a file pushed past the very last slot of the
  chain (Y/6) falls off, and it is archived instead of deleted.
* ``append`` — the new sample takes the first empty slot of the chain;
  nothing moves. A full chain is an error, not an overwrite.

Archive locations:

    card: <card root>/<tape>_<slot>_<timestamp>.WAV
    usb:  <archive>/<tape>_<slot>_<timestamp>.WAV

Moves run from the end of the chain backwards so a sample is never read
after being overwritten, and the final write is read back and compared. A
failure half way leaves the chain consistent (a duplicate is possible,
silence is not).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

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
    tape_names = TAPE_ORDER

    def __init__(self, root):
        self.root = Path(root)
        self.sk = self.root / "SK"
        if not self.sk.is_dir():
            raise InstallError(f"{self.root} has no SK/ folder — not a Spotykach card")
        # Overflow lands on the card root, where the user can see it without
        # digging through SK/.
        self.archive_dir = self.root

    def close(self):
        """Nothing is held open: slots are files."""
        self.wrote = getattr(self, "wrote", False)

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

    def move(self, tape, slot, to_tape, to_slot):
        """Rename one slot file onto another; the source disappears."""
        source = self.slot_path(tape, slot)
        target = self.slot_path(to_tape, to_slot)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)

    def occupancy(self):
        """Which slots hold a file: {tape letter: {1-based slots}}."""
        return {
            letter: {slot for slot in range(1, SLOTS_PER_TAPE + 1)
                     if self.slot_path(letter, slot).is_file()}
            for letter in self.tape_names
        }

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
        self.wrote = False        # anything on the tape makes a retry unsafe
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
        self.wrote = True
        self.link.upload(self._tape_index(tape), slot - 1, data)

    def move(self, tape, slot, to_tape, to_slot):
        """Copy one slot to another and delete the source."""
        data = self.read(tape, slot)
        if data is None:
            raise InstallError(f"cannot move {tape}/{slot} — the slot is empty")
        self.write(to_tape, to_slot, data)
        self.clear(tape, slot)

    def occupancy(self):
        """Which slots the unit reports as filled: {tape letter: {slots}}."""
        occupied = {letter: set() for letter in self.tape_names}
        for entry in self.link.list_slots():
            index = ord(entry.tape)
            if entry.flags and index < len(self.tape_names):
                occupied[self.tape_names[index]].add(entry.slot + 1)
        return occupied

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


# --- placement ------------------------------------------------------------


def tape_chain(transport, tape):
    """The flat slot chain from ``tape`` to the end, in device order.

    ``B G P R T Y`` with six slots each is how the unit itself lays its tapes
    out; a chain starts at the tape being written and runs to the last slot of
    the last tape, so bumping can cascade across tapes and appending can find
    the next free slot wherever it lives.
    """
    names = getattr(transport, "tape_names", None) or TAPE_ORDER
    if tape not in names:
        raise InstallError(
            f"tape {tape!r} is not on this unit (it has {' '.join(names)})")
    start = names.index(tape)
    return [(letter, slot) for letter in names[start:]
            for slot in range(1, SLOTS_PER_TAPE + 1)]


def _occupancy(transport):
    """Which slots hold a sample: {tape letter: {1-based slots}}."""
    probe = getattr(transport, "occupancy", None)
    if callable(probe):
        return cast("dict[str, set[int]]", probe())
    # Transports without a cheap probe (test doubles) get asked slot by slot.
    names = getattr(transport, "tape_names", None) or TAPE_ORDER
    return {letter: {slot for slot in range(1, SLOTS_PER_TAPE + 1)
                     if transport.read(letter, slot)}
            for letter in names}


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


def _bump(transport, chain, archive_dir, stamp=None):
    """Push every occupied slot on the chain one place down.

    Returns ``(moves, archived_path)``. Only the file sitting on the very
    last slot of the chain can fall off; it is copied to ``archive_dir``
    with a timestamp suffix before anything moves. Slots are moved end-first
    so nothing is ever overwritten before it has been read.
    """
    occupied = _occupancy(transport)
    filled = [i for i, (letter, slot) in enumerate(chain)
              if slot in occupied.get(letter, ())]
    if not filled:
        return 0, None
    if 0 not in filled:
        # The slot being written is already free — nothing is displaced, so
        # nothing moves, wherever else on the chain samples live.
        return 0, None

    archived = None
    if filled[-1] == len(chain) - 1:            # something falls off the end
        letter, slot = chain[-1]
        data = transport.read(letter, slot)
        if data:
            archived = _archive_write(
                archive_dir or "/tmp", letter, slot, data, stamp or timestamp())
        filled.pop()

    moves = 0
    for i in reversed(filled):
        from_tape, from_slot = chain[i]
        to_tape, to_slot = chain[i + 1]
        transport.move(from_tape, from_slot, to_tape, to_slot)
        moves += 1
    return moves, archived


def _next_free(transport, chain):
    """The first empty slot on the chain, or an error when it is full."""
    occupied = _occupancy(transport)
    for letter, slot in chain:
        if slot not in occupied.get(letter, ()):
            return letter, slot
    raise InstallError(
        f"every slot from {chain[0][0]}/1 to {chain[-1][0]}/{chain[-1][1]} is "
        "full — free a slot on the unit or use bump mode")


def install_sample(transport, tape, data, archive_dir=None, mode="bump",
                   stamp=None):
    """Place ``data`` on the chain starting at ``tape``.

    ``mode="bump"``   — slot 1 of the tape; occupied slots cascade down
                        ``B G P R T Y``; only what falls off the very end is
                        archived (to ``archive_dir``, defaulting to the
                        transport's own archive location).
    ``mode="append"`` — the first empty slot of the chain; nothing moves.
    """
    if mode not in ("bump", "append"):
        raise ValueError(f"unknown placement mode {mode!r} (bump or append)")

    chain = tape_chain(transport, tape)
    archive_dir = archive_dir or getattr(transport, "archive_dir", None)

    if mode == "append":
        letter, slot = _next_free(transport, chain)
        moves, archived = 0, None
    else:
        moves, archived = _bump(transport, chain, archive_dir, stamp)
        letter, slot = chain[0]

    transport.write(letter, slot, data)

    back = transport.read(letter, slot)
    if back != data:
        raise InstallError(
            f"read-back mismatch on {letter}/{slot}: wrote {len(data)} bytes, "
            f"got {len(back) if back else 0}"
        )
    return Placement(
        transport=transport.name,
        tape=letter,
        slot=slot,
        shifted=moves,
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
