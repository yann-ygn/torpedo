"""In-memory stand-in for the Torpedo Live's HID device, for tests and `--fake`.

Emulates the observed firmware at the report level: one IN report per OUT
report from a queue (`05` when empty), no reply to no-op writes, linked
parameters, the preset-modified flag. Commands that were never observed in a
capture raise, so tests also check that nothing unknown gets sent.
"""

from collections import deque

from . import ir
from .protocol import (ARCADE, ARCADE_EDITABLE, BANK_END, EDIT_BUFFER, IR_ACK, IR_BANK, IR_STATUS,
                       MIC_SLOT, MSG_CAB, MSG_EMPTY, MSG_INFO, MSG_IR_DATA, MSG_IR_END,
                       MSG_IR_HEADER, MSG_NAME, MSG_PARAM, MSG_POLL, MSG_QUERY, MSG_SETUP,
                       NAMES_ALL, NAMES_EDIT_BUFFER, PRESET_MODE, PRESETS, QUERY_BANK, QUERY_INFO,
                       QUERY_IR, QUERY_IR_COMMIT, QUERY_IR_DELETE, QUERY_NAMES, QUERY_PARAMS,
                       QUERY_SETUP, QUERY_STORE, REPORT_LEN, SETUP_OPTIONS, SETUP_PRESET,
                       SETUP_PRESET_SOURCE)

# Read from the real unit (preset A001 and setup, 2026-10-15)
A001 = [0, 5, 0, 127, 127, 1, 1, 0, 16, 0, 0, 0, 0, 5, 0, 17, 1, 1, 0, 21, 20, 20, 20, 21, 87,
        1, 0, 1, 0, 0, 0, 38, 21, 87, 1, 95, 0, 0, 0, 0, 0, 1, 95, 0, 1, 0, 57, 57, 57, 57, 57]
SETUP_VALUES = [0x55, 0, 1, 0, 0, 1, 1, 3, 2, 7, 1, 1, 1, 0x10, 3, 0xFF, 0xFF, 0xFF]
INFO = bytes([MSG_INFO, 2, 2, 1, 4, 0x17, 3]) + b"032C4B94"
CONTOUR, EQ_GAIN0, EQ_GAIN4 = 0x20, 0x13, 0x17
STALE = bytes(range(0xC0, 0xC0 + REPORT_LEN))  # the device reuses its buffer


TWO_NOTES_MICS = ["Knightfall", "Cnd 87", "Rbn 121", "Rbn 160", "Dyn 421", "Dyn 57",
                  "Dyn Bass20", "Dyn Bass52"]
# Bank 0 as read from the unit, except the unnamed fillers
CABS = {0: ("Blonde 63", "Two Notes Audio Engineering", TWO_NOTES_MICS),
        1: ("Brit 65C", "Two Notes Audio Engineering", TWO_NOTES_MICS),
        2: ("Brit VintC", "Two Notes Audio Engineering", TWO_NOTES_MICS),
        0x10: ("LuxTweeDe", "Hop Pole Studios", ["Cnd Gem II", "Cnd S5E", "Rbn 1R", "Dyn 421",
                                                 "Dyn 57", "Dyn 7b", "Dyn Bass112", "Dyn i5"]),
        0x12: ("DarkPrinz", "Black Bear Studio", ["Cnd 840", "Cnd BLX414", "Rbn 160", "Dyn 421",
                                                  "Dyn 57", "Dyn 906", "Dyn Bass20", "Dyn TG201"])}
SETUP_WRITABLE = {o.index for o in SETUP_OPTIONS.values()}
PRESET_NAMES = (["BDELUXE 1 RBN", "BDELUXE 1 DYN421"] + [f"FACTORY {i}" for i in range(2, 75)]
                + ["USER"] * 53)  # A076..A128 are empty user slots, as on the unit
BANK0_SIZE = 32
ML_IRS = [f"ML-FRMN-{i:02d}" for i in range(85)] + ["ML-FRMN-V30-906-E"]  # bank 1, 86 IRs


def fake_ir(seed: int) -> list[int]:
    """A deterministic decaying IR, full scale like the unit stores them."""
    values = [(-1) ** k * 0.97 ** k * (1 + (k * seed) % 7) / 7 for k in range(ir.LENGTHS[20])]
    peak = max(map(abs, values))
    return [round(v / peak * ir.FULL_SCALE) for v in values]


def report(*payload: int) -> list[int]:
    return list(bytes(payload) + STALE[len(payload):])


def cab_reports(bank: int, idx: int, name: str, maker: str, mics: list[str]) -> list[list[int]]:
    def pad(s: str, n: int) -> bytes:
        return s.encode("latin-1")[:n].ljust(n, bytes(1))
    slots = b"".join(pad(m, MIC_SLOT) for m in mics)
    return [report(MSG_CAB, bank, idx, 0, 0x01, 0x08, 0xD8, *pad(name, REPORT_LEN - 7)),
            report(MSG_CAB, bank, idx, 1, *bytes(24), *slots[:3 * MIC_SLOT]),
            report(MSG_CAB, bank, idx, 2, *slots[3 * MIC_SLOT:]),
            report(MSG_CAB, bank, idx, 3, *pad(maker, 32))]


class FakeHid:
    def __init__(self, lag: int = 0):
        self.lag = lag  # empty replies after a command before its messages show up
        self.held = 0
        self.presets = [list(A001) for _ in range(PRESETS)]
        self.presets[1][CONTOUR] = self.presets[1][EQ_GAIN0] = self.presets[1][EQ_GAIN4] = 30
        self.presets[1][0x1A] = 3  # reverb room
        self.params = list(self.presets[0])
        self.setup = list(SETUP_VALUES)
        self.names = list(PRESET_NAMES)
        self.buffer_name = self.names[0]
        self.stores: list[int] = []  # presets written to "flash"
        self.irs = [(name, fake_ir(i)) for i, name in enumerate(ML_IRS)]  # bank 1
        self.upload: list[bytes] = []
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
        elif cmd == MSG_QUERY and idx == QUERY_BANK and val <= 4:
            if val == 0:
                for i in range(BANK0_SIZE):
                    name, maker, mics = CABS.get(i, (f"Cab {i}", "Two Notes Audio Engineering",
                                                     TWO_NOTES_MICS))
                    self.queue.extend(cab_reports(0, i, name, maker, mics))
            elif val == IR_BANK:  # IRs: only parts 0-1
                for i, (name, _) in enumerate(self.irs):
                    self.queue.extend(cab_reports(1, i, name, "", [""] * 8)[:2])
            self.queue.append(report(MSG_QUERY, BANK_END, val, 3, *b"Noise Factory"))
        elif cmd == MSG_QUERY and idx == QUERY_IR and val == IR_BANK and b[3] < len(self.irs):
            name, samples = self.irs[b[3]]
            stream = ir.pack_samples(samples)
            self.queue.extend(report(*h) for h in ir.headers(b[3], name, stream, len(samples), upload=False))
            self.queue.extend(report(*c) for c in ir.chunks(stream))
            self.queue.append(report(MSG_IR_END))
        elif cmd == MSG_IR_HEADER:
            self.upload = self.upload + [b] if len(self.upload) == 1 else [b]
            if len(self.upload) == 2:
                self.queue.append(report(MSG_QUERY, IR_ACK))
        elif cmd == MSG_IR_DATA and len(self.upload) >= 2:
            self.upload.append(b)
            self.queue.append(report(MSG_QUERY, IR_ACK, 5, len(self.upload) // 3, 0x10))
        elif cmd == MSG_IR_END and len(self.upload) >= 2:
            h0, h1, *data = self.upload
            assert h0[8] == len(self.irs) + 1, "upload slot = bank size + 1"
            self.irs.append(ir.parse_transfer(h0, h1, data, upload=True))  # checks checksums
            self.upload = []
            self.queue.append(report(MSG_QUERY, IR_STATUS, 5, 0x10, 0x10))
        elif cmd == MSG_QUERY and idx == QUERY_IR_DELETE and b[3] == IR_BANK and val < len(self.irs):
            del self.irs[val]
            self.queue.append(report(MSG_QUERY, IR_STATUS, 0))
        elif cmd == MSG_QUERY and idx == QUERY_IR_COMMIT:
            self.queue.append(report(MSG_QUERY, IR_STATUS, 0))
        elif cmd == MSG_PARAM and idx < len(self.params):
            self._set_param(idx, val)
        elif cmd == MSG_SETUP and idx == SETUP_PRESET and val < PRESETS:
            self._select(val)
        elif cmd == MSG_SETUP and idx in SETUP_WRITABLE:
            if self.setup[idx] != val:
                self.setup[idx] = val
                self.queue.append(report(MSG_SETUP, idx, val))
        elif cmd == MSG_QUERY and idx == QUERY_NAMES and val in (NAMES_ALL, NAMES_EDIT_BUFFER):
            for i in (range(PRESETS) if val == NAMES_ALL else ()):
                self.queue.append(self._name_report(i))
            self.queue.append(self._name_report(EDIT_BUFFER))
        elif cmd == MSG_NAME and idx == EDIT_BUFFER:
            self.buffer_name = b[2:].split(bytes(1))[0].decode()
            self.queue.append(self._name_report(EDIT_BUFFER))
        elif cmd == MSG_QUERY and idx == QUERY_STORE and val < PRESETS:
            self._store(val)
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
        if self.setup[SETUP_PRESET] != EDIT_BUFFER:
            self.setup[SETUP_PRESET_SOURCE] = self.setup[SETUP_PRESET]
            self.setup[SETUP_PRESET] = EDIT_BUFFER
            self.queue.append(report(MSG_SETUP, SETUP_PRESET, EDIT_BUFFER))
            self.queue.append(report(MSG_SETUP, SETUP_PRESET_SOURCE, self.setup[SETUP_PRESET_SOURCE]))

    def _select(self, n: int):
        if self.setup[SETUP_PRESET] == n:
            return
        new = self.presets[n]
        self.queue.extend(report(MSG_PARAM, i, v) for i, v in enumerate(new) if v != self.params[i])
        self.params = list(new)
        self.buffer_name = self.names[n]
        self.setup[SETUP_PRESET] = self.setup[SETUP_PRESET_SOURCE] = n
        self.queue.append(report(MSG_SETUP, SETUP_PRESET, n))

    def _name_report(self, idx: int) -> list[int]:
        name = self.buffer_name if idx == EDIT_BUFFER else self.names[idx]
        arcade = (self.params if idx == EDIT_BUFFER else self.presets[idx])[PRESET_MODE] == ARCADE
        return report(MSG_NAME, idx, int(arcade), *name.encode(), 0)

    def _store(self, n: int):
        self.presets[n] = list(self.params)
        self.names[n] = self.buffer_name
        self.stores.append(n)
        self.setup[SETUP_PRESET] = self.setup[SETUP_PRESET_SOURCE] = n
        self.queue.extend([self._name_report(n), report(MSG_SETUP, SETUP_PRESET, n),
                           report(MSG_SETUP, SETUP_PRESET_SOURCE, n), report(MSG_SETUP, 5, 0)])
