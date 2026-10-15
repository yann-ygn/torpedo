"""Transport for the Two notes Torpedo Live (HID 0483:A334, see PROTOCOL.md).

The device answers each OUT report with exactly one IN report: the next
message in its queue, or an `05` report when the queue is empty (on its own
it only flushes the queue about once a second). So after a command we poll
with `7e` until the expected reply has arrived and the queue is empty.
"""

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from .protocol import (HEARTBEAT, MSG_EMPTY, MSG_INFO, MSG_PARAM, MSG_POLL, MSG_QUERY,
                       MSG_SETUP, PARAMS, PRESET_MODIFIED, PRESETS, QUERY_INFO, QUERY_PARAMS,
                       QUERY_SETUP, REPORT_LEN, SETUP, SETUP_PRESET, Param)

VID, PID = 0x0483, 0xA334  # ST's shared VID: also check the manufacturer
MANUFACTURER = "Two Notes Audio Engineering"
READ_TIMEOUT_MS = 100
TIMEOUT = 2.0


@dataclass
class Info:
    serial: str
    firmware: str  # (?) bytes 4-5 of the info reply
    raw: bytes


def open_hid():
    import hid
    info = next((d for d in hid.enumerate(VID, PID) if d["manufacturer_string"] == MANUFACTURER), None)
    if info is None:
        raise RuntimeError(f"Torpedo Live ({VID:04X}:{PID:04X}) not found")
    dev = hid.device()
    try:
        dev.open_path(info["path"])
    except OSError as e:
        path = info["path"].decode(errors="replace")
        hint = (" Add the udev rule from linux/70-torpedo-live.rules (see README) and replug."
                if sys.platform.startswith("linux") else "")
        raise RuntimeError(f"found the Torpedo but could not open {path} ({e}).{hint}") from e
    return dev


class Torpedo:
    """Keeps a cache of the current preset's parameters and setup values,
    updated from every message the device sends."""

    def __init__(self, dev=None):
        self.dev = dev if dev is not None else open_hid()
        self.params: dict[int, int] = {}
        self.setup: dict[int, int] = {}
        self._exchange(MSG_POLL)  # flush whatever is queued

    def close(self):
        self.dev.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- transport -------------------------------------------------------

    def _send(self, *payload: int):
        self.dev.write([0, *payload] + [0] * (REPORT_LEN - len(payload)))

    def _exchange(self, *cmd: int, until: Callable[[bytes], bool] | None = None,
                  timeout: float = TIMEOUT) -> list[bytes]:
        """Send `cmd`, then poll until `until` matched a message (if given) and the queue is empty.

        The reply to a write may lag behind it, so an empty queue only ends the
        exchange once `until` has matched.
        """
        self._send(*cmd)
        msgs, done = [], until is None
        deadline = time.monotonic() + timeout
        while True:
            p = bytes(self.dev.read(REPORT_LEN, READ_TIMEOUT_MS))
            if not p or p[0] == MSG_EMPTY:
                if done:
                    return msgs
            elif p[:2] != HEARTBEAT:
                self._absorb(p)
                msgs.append(p)
                done = done or until(p)
            if time.monotonic() > deadline:
                raise TimeoutError(f"no reply to {bytes(cmd).hex(' ')}")
            self._send(MSG_POLL)

    def _absorb(self, p: bytes):
        if p[0] == MSG_PARAM and p[1] < len(PARAMS):
            self.params[p[1]] = p[2]
        elif p[0] == MSG_SETUP and p[1] < len(SETUP):
            self.setup[p[1]] = p[2]

    def _read_all(self, query: int, reply: int, count: int):
        seen = set()

        def complete(p: bytes) -> bool:
            if p[0] == reply:
                seen.add(p[1])
            return len(seen) == count
        self._exchange(MSG_QUERY, query, until=complete)

    # --- queries -----------------------------------------------------------

    def info(self) -> Info:
        msgs = self._exchange(MSG_QUERY, QUERY_INFO, until=lambda p: p[0] == MSG_INFO)
        p = next(m for m in msgs if m[0] == MSG_INFO)
        return Info(serial=p[7:15].decode("ascii", "replace"), firmware=f"{p[4]}.{p[5]}", raw=p[:15])

    def read_params(self) -> dict[int, int]:
        self._read_all(QUERY_PARAMS, MSG_PARAM, len(PARAMS))
        return dict(self.params)

    def read_setup(self) -> dict[int, int]:
        self._read_all(QUERY_SETUP, MSG_SETUP, len(SETUP))
        return dict(self.setup)

    # --- writes --------------------------------------------------------------

    def set_param(self, param: Param, value: int) -> dict[int, int]:
        """Edit the current preset (not saved). Returns every parameter the device
        reported as changed, including linked ones it recomputed."""
        param.check(value)
        if param.index not in self.params:
            self.read_params()
        if self.params[param.index] == value:
            return {}  # the device would not answer
        echo = bytes([MSG_PARAM, param.index, value])
        msgs = self._exchange(MSG_PARAM, param.index, value, until=lambda p: p[:3] == echo)
        return {p[1]: p[2] for p in msgs if p[0] == MSG_PARAM}

    @property
    def preset(self) -> int:
        """Current preset index; bit 7 (PRESET_MODIFIED) is set if it has unsaved edits."""
        if SETUP_PRESET not in self.setup:
            self.read_setup()
        return self.setup[SETUP_PRESET]

    def select_preset(self, n: int) -> dict[int, int]:
        """Load a stored preset, discarding unsaved edits. Returns the changed parameters."""
        if not 0 <= n < PRESETS:
            raise ValueError(f"preset must be 0..{PRESETS - 1}")
        if self.preset == n:
            return {}  # already there and unmodified: the device would not answer
        if not self.params:
            self.read_params()
        reply = bytes([MSG_SETUP, SETUP_PRESET, n])
        msgs = self._exchange(MSG_SETUP, SETUP_PRESET, n, until=lambda p: p[:3] == reply)
        return {p[1]: p[2] for p in msgs if p[0] == MSG_PARAM}

    def discard(self) -> dict[int, int]:
        """Drop unsaved edits by reloading the current preset."""
        return self.select_preset(self.preset & ~PRESET_MODIFIED)
