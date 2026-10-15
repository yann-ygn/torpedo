"""In-memory stand-in for the Torpedo Live's HID device, for tests and `--fake`.

Emulates the observed firmware at the report level: one IN report per OUT
report from a queue (`05` when empty), no reply to no-op writes, linked
parameters, the preset-modified flag. Commands that were never observed in a
capture raise, so tests also check that nothing unknown gets sent.
"""

from collections import deque

from .protocol import (ARCADE, ARCADE_EDITABLE, MSG_EMPTY, MSG_INFO, MSG_PARAM, MSG_POLL,
                       MSG_QUERY, MSG_SETUP, PRESET_MODE, PRESET_MODIFIED, PRESETS, QUERY_INFO,
                       QUERY_PARAMS, QUERY_SETUP, REPORT_LEN, SETUP_PRESET)

# Read from the real unit (preset A001 and setup, 2026-10-15)
A001 = [0, 5, 0, 127, 127, 1, 1, 0, 16, 0, 0, 0, 0, 5, 0, 17, 1, 1, 0, 21, 20, 20, 20, 21, 87,
        1, 0, 1, 0, 0, 0, 38, 21, 87, 1, 95, 0, 0, 0, 0, 0, 1, 95, 0, 1, 0, 57, 57, 57, 57, 57]
SETUP_VALUES = [0x55, 0, 1, 0, 0, 1, 1, 3, 2, 7, 1, 1, 1, 0x10, 3, 0xFF, 0xFF, 0xFF]
INFO = bytes([MSG_INFO, 2, 2, 1, 4, 0x17, 3]) + b"032C4B94"
CONTOUR, EQ_GAIN0, EQ_GAIN4 = 0x20, 0x13, 0x17
STALE = bytes(range(0xC0, 0xC0 + REPORT_LEN))  # the device reuses its buffer


def report(*payload: int) -> list[int]:
    return list(bytes(payload) + STALE[len(payload):])


class FakeHid:
    def __init__(self, lag: int = 0):
        self.lag = lag  # empty replies after a command before its messages show up
        self.held = 0
        self.presets = [list(A001) for _ in range(PRESETS)]
        self.presets[1][CONTOUR] = self.presets[1][EQ_GAIN0] = self.presets[1][EQ_GAIN4] = 30
        self.presets[1][0x1A] = 3  # reverb room
        self.params = list(self.presets[0])
        self.setup = list(SETUP_VALUES)
        self.queue: deque[list[int]] = deque()
        self.replies = 0  # IN reports owed: one per OUT report
        self.sent: list[bytes] = []  # every OUT report, without the report ID

    def close(self):
        pass

    def write(self, data: list[int]) -> int:
        assert len(data) == REPORT_LEN + 1 and data[0] == 0, "report ID 0 + 64 bytes"
        b = bytes(data[1:])
        self.sent.append(b)
        self.replies += 1
        self._handle(b)
        return len(data)

    def read(self, size: int, timeout_ms: int = 0) -> list[int]:
        if not self.replies:
            return []
        self.replies -= 1
        if self.held:
            self.held -= 1
            return report(MSG_EMPTY)
        return self.queue.popleft() if self.queue else report(MSG_EMPTY)

    def _handle(self, b: bytes):
        cmd, idx, val = b[0], b[1], b[2]
        if cmd == MSG_POLL:
            return
        self.held = self.lag
        if cmd == MSG_QUERY and idx == QUERY_INFO:
            self.queue.append(report(*INFO))
        elif cmd == MSG_QUERY and idx == QUERY_PARAMS:
            self.queue.extend(report(MSG_PARAM, i, v) for i, v in enumerate(self.params))
        elif cmd == MSG_QUERY and idx == QUERY_SETUP:
            self.queue.extend(report(MSG_SETUP, i, v) for i, v in enumerate(self.setup))
        elif cmd == MSG_PARAM and idx < len(self.params):
            self._set_param(idx, val)
        elif cmd == MSG_SETUP and idx == SETUP_PRESET and val < PRESETS:
            self._select(val)
        else:
            raise AssertionError(f"fake: command never observed on the real device: {b[:4].hex(' ')}")

    def _set_param(self, idx: int, val: int):
        if self.params[idx] == val or (self.params[PRESET_MODE] == ARCADE and idx not in ARCADE_EDITABLE):
            return
        changes = {EQ_GAIN0: val, EQ_GAIN4: val} if idx == CONTOUR else {}
        for i, v in changes.items():
            self.params[i] = v
            self.queue.append(report(MSG_PARAM, i, v))
        self.params[idx] = val
        self.queue.append(report(MSG_PARAM, idx, val))
        if not self.setup[SETUP_PRESET] & PRESET_MODIFIED:
            self.setup[SETUP_PRESET] |= PRESET_MODIFIED
            self.queue.append(report(MSG_SETUP, SETUP_PRESET, self.setup[SETUP_PRESET]))

    def _select(self, n: int):
        if self.setup[SETUP_PRESET] == n:
            return
        new = self.presets[n]
        self.queue.extend(report(MSG_PARAM, i, v) for i, v in enumerate(new) if v != self.params[i])
        self.params = list(new)
        self.setup[SETUP_PRESET] = n
        self.queue.append(report(MSG_SETUP, SETUP_PRESET, n))
