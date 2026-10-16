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

from . import ir
from .protocol import (ARCADE, ARCADE_EDITABLE, BANK_END, EDIT_BUFFER, EMPTY_PRESET,
                       FOLDER_BANK_OFFSET, HEARTBEAT, IR_ACK, IR_BANK, MSG_CAB, MSG_EMPTY,
                       MSG_INFO, MSG_IR_DATA, MSG_IR_END, MSG_IR_HEADER, MSG_NAME, MSG_PARAM,
                       MSG_POLL, MSG_QUERY, MSG_SETUP, NAMES_ALL, NAMES_EDIT_BUFFER, PARAMS,
                       PARAM_BY_NAME, PRESETS, PRESET_MODE, QUERY_BANK, QUERY_CLEAR, QUERY_COMMIT,
                       QUERY_INFO, QUERY_IR, QUERY_IR_DELETE, QUERY_NAMES, QUERY_PARAMS,
                       QUERY_SETUP, QUERY_STORE, REPORT_LEN, SETUP, SETUP_OPTIONS, SETUP_PRESET,
                       SETUP_PRESET_SOURCE, STATUS, Cab, Param, PresetName, SetupOption,
                       check_name, parse_cab, parse_name, preset_name)

VID, PID = 0x0483, 0xA334  # ST's shared VID: also check the manufacturer
MANUFACTURER = "Two Notes Audio Engineering"
SPEAKER, MIC, MIC_2, MUTE, MUTE_2 = (PARAM_BY_NAME[n].index for n in (
    "SIMUHP_SPEAKER", "SIMUHP_MIC", "SIMUHP_MIC_2", "SIMUHP_MUTE", "SIMUHP_MUTE_2"))
WAVES = (PARAM_BY_NAME["SIMUHP_WAVE0"].index, PARAM_BY_NAME["SIMUHP_WAVE1"].index)
FOLDERS = (PARAM_BY_NAME["SIMUHP_FOLDER_IR_0"].index, PARAM_BY_NAME["SIMUHP_FOLDER_IR_1"].index)
READ_TIMEOUT_MS = 100
TIMEOUT = 2.0
STORE_TIMEOUT = 6.0  # storing a preset was answered after ~2.1 s


class IgnoredWrite(ValueError):
    """The device drops some writes without a reply: simulation parameters
    while the preset is in arcade mode."""


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
        self.timeout = TIMEOUT
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
                  timeout: float | None = None, drain: bool = True) -> list[bytes]:
        """Send `cmd`, then poll until `until` matched a message (if given) and the queue is empty.

        The reply to a write may lag behind it, so an empty queue only ends the
        exchange once `until` has matched. With drain=False it ends right at the
        match (IR chunks: Remote sends the next one as soon as `04 70` arrives).
        """
        self._send(*cmd)
        msgs, done = [], until is None
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            p = bytes(self.dev.read(REPORT_LEN, READ_TIMEOUT_MS))
            if not p or p[0] == MSG_EMPTY:
                if done:
                    return msgs
            elif p[:2] != HEARTBEAT:
                self._absorb(p)
                msgs.append(p)
                done = done or until(p)
                if done and not drain:
                    return msgs
            if time.monotonic() > deadline:
                raise TimeoutError(f"no reply to {bytes(cmd).hex(' ')}")
            self._send(MSG_POLL)

    def poll(self) -> list[bytes]:
        """Fetch what the device sent on its own (front panel changes) into the cache.

        Coming back on the panel to a preset left with edits restores them with
        `0c 03 80` but no SETUP_PRESET_SOURCE, so the source is then read back.
        """
        msgs = self._exchange(MSG_POLL)
        setup = {p[1]: p[2] for p in msgs if p[0] == MSG_SETUP}
        if setup.get(SETUP_PRESET) == EDIT_BUFFER and SETUP_PRESET_SOURCE not in setup:
            self.read_setup()
        return msgs

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

    def read_cabs(self, bank: int = 0) -> list[Cab]:
        """Cab bank listing. Bank 0 holds the cabs SIMUHP_SPEAKER selects."""
        end = bytes([MSG_QUERY, BANK_END, bank])
        msgs = self._exchange(MSG_QUERY, QUERY_BANK, bank, until=lambda p: p[:3] == end)
        records: dict[int, dict[int, bytes]] = {}
        for m in msgs:
            if m[0] == MSG_CAB and m[1] == bank:
                records.setdefault(m[2], {})[m[3]] = m
        return [parse_cab(i, records[i]) for i in sorted(records)]

    # --- writes --------------------------------------------------------------

    def set_param(self, param: Param, value: int) -> dict[int, int]:
        """Edit the current preset (not saved). Returns every parameter the device
        reported as changed, including linked ones it recomputed."""
        param.check(value)
        if param.index not in self.params:
            self.read_params()
        if self.params[param.index] == value:
            return {}  # the device would not answer
        if self.params[PRESET_MODE] == ARCADE and param.index not in ARCADE_EDITABLE:
            raise IgnoredWrite(f"{param.name} can't be edited in arcade mode (set "
                               "preset_mode=0 for simulation or preset_mode=2 for ir first)")
        echo = bytes([MSG_PARAM, param.index, value])
        try:
            msgs = self._exchange(MSG_PARAM, param.index, value, until=lambda p: p[:3] == echo)
        except TimeoutError:
            if self.read_params()[param.index] == value:
                raise
            mode = self.params.get(PRESET_MODE)
            raise IgnoredWrite(f"the Torpedo ignored {param.name}={value} "
                               f"(PRESET_MODE={mode})") from None
        return {p[1]: p[2] for p in msgs if p[0] == MSG_PARAM}

    def select_cab(self, index: int, cabs: list[Cab] | None = None) -> dict[int, int]:
        """Select a cab from bank 0. Both mics (A and B) use the cab's mic list:
        like Torpedo Remote, move each one so the same mic stays selected when
        the new cab has it. Mic B only in simulation mode (arcade ignores it)."""
        cabs = cabs if cabs is not None else self.read_cabs()
        if not 0 <= index < len(cabs):
            raise ValueError(f"cab must be 0..{len(cabs) - 1}")
        if not self.params:
            self.read_params()
        old = self.params[SPEAKER]
        mics = [m for m in (MIC, MIC_2)
                if self.params[PRESET_MODE] != ARCADE or m in ARCADE_EDITABLE]
        names = {m: cabs[old].mics[self.params[m]] if old < len(cabs) else "" for m in mics}
        changes = self.set_param(PARAMS[SPEAKER], index)
        for m, name in names.items():
            if name and name in cabs[index].mics:
                changes |= self.set_param(PARAMS[m], cabs[index].mics.index(name))
        return changes

    def read_irs(self, folder: int = 0) -> list[Cab]:
        """IRs an IR slot can pick from in `folder` (0 = the ML bank)."""
        return self.read_cabs(folder + FOLDER_BANK_OFFSET)

    def select_ir(self, slot: int, index: int, irs: list[Cab] | None = None) -> dict[int, int]:
        """Load IR `index` of folder 0 into IR slot 0 or 1 (heard in IR mode)."""
        if slot not in (0, 1):
            raise ValueError("IR slot must be 1 or 2")
        irs = irs if irs is not None else self.read_irs()
        if not 0 <= index < len(irs):
            raise ValueError(f"IR must be 0..{len(irs) - 1}")
        changes = self.set_param(PARAMS[FOLDERS[slot]], 0)
        return changes | self.set_param(PARAMS[WAVES[slot]], index)

    @property
    def preset(self) -> int:
        """Current preset index, or EDIT_BUFFER if it has unsaved edits (see `source`)."""
        if SETUP_PRESET not in self.setup:
            self.read_setup()
        return self.setup[SETUP_PRESET]

    @property
    def source(self) -> int:
        """The preset the current sound comes from, edited or not."""
        p = self.preset
        return self.setup[SETUP_PRESET_SOURCE] if p == EDIT_BUFFER else p

    @property
    def edited(self) -> bool:
        return self.preset == EDIT_BUFFER

    def read_names(self) -> list[PresetName]:
        """Names of the 128 presets, then the edit buffer's (index EDIT_BUFFER)."""
        seen: dict[int, PresetName] = {}

        def complete(p: bytes) -> bool:
            if p[0] == MSG_NAME:
                seen[p[1]] = PresetName(p[1], parse_name(p), bool(p[2]))
            return len(seen) == PRESETS + 1
        self._exchange(MSG_QUERY, QUERY_NAMES, NAMES_ALL, until=complete)
        return [seen[i] for i in sorted(seen)]

    def read_buffer_name(self) -> str:
        """The edit buffer's name (`04 09 80`)."""
        reply = bytes([MSG_NAME, EDIT_BUFFER])
        msgs = self._exchange(MSG_QUERY, QUERY_NAMES, NAMES_EDIT_BUFFER,
                              until=lambda p: p[:2] == reply)
        return next(parse_name(p) for p in msgs if p[:2] == reply)

    def save(self, n: int, name: str | None = None, overwrite: bool = False,
             names: list[PresetName] | None = None) -> PresetName:
        """Store the current sound (edit buffer) into preset `n`, optionally renamed.

        Writes the unit's flash. Refuses to replace a preset that isn't an
        empty "USER" slot unless `overwrite` (saving over the source preset
        itself counts as overwriting too)."""
        if not 0 <= n < PRESETS:
            raise ValueError(f"preset must be 0..{PRESETS - 1}")
        if names is None and (not overwrite or name is None):
            names = self.read_names()
        if not overwrite and not names[n].empty:
            raise ValueError(f"{preset_name(n)} holds \"{names[n].name}\": "
                             "pass overwrite=True to replace it")
        current = names[-1].name if names is not None else self.read_buffer_name()
        name = check_name(name) if name is not None else current
        if name != current:  # renaming to the same name gets no reply (PROTOCOL.md)
            encoded = name.encode("ascii")
            echo = bytes([MSG_NAME, EDIT_BUFFER])
            self._exchange(MSG_NAME, EDIT_BUFFER, *encoded,
                           until=lambda p: p[:2] == echo and parse_name(p) == name)
        # The reply starts with `03 n <name>`; `0c 03 n` follows in the same burst,
        # but may not come if preset n was already selected unedited.
        stored = (bytes([MSG_NAME, n]), bytes([MSG_SETUP, SETUP_PRESET, n]))
        msgs = self._exchange(MSG_QUERY, QUERY_STORE, n,
                              until=lambda p: p[:2] == stored[0] or p[:3] == stored[1],
                              timeout=STORE_TIMEOUT)
        return next((PresetName(n, parse_name(p), bool(p[2])) for p in msgs
                     if p[0] == MSG_NAME and p[1] == n), PresetName(n, name, False))

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
        """Leave unsaved edits by reloading the preset they came from. The unit
        still keeps them (as e.g. "A001*" on its panel) until the next edit."""
        return self.select_preset(self.source) if self.edited else {}

    def clear_preset(self, n: int) -> list[PresetName]:
        """Empty preset `n` like Remote's Delete (a flash write): it becomes a
        "USER" slot holding EMPTY_PARAMS. Returns the names read back.

        Refused while `n` has unsaved edits (not tried on the unit)."""
        if not 0 <= n < PRESETS:
            raise ValueError(f"preset must be 0..{PRESETS - 1}")
        if self.edited and self.source == n:
            raise ValueError(f"{preset_name(n)} has unsaved edits: save or discard them first")
        current = self.preset == n
        status = bytes([MSG_QUERY, STATUS, 0])
        self._exchange(MSG_QUERY, QUERY_CLEAR, n, until=lambda p: p[:3] == status,
                       timeout=STORE_TIMEOUT)
        self._commit()
        names = self.read_names()
        if names[n].name != EMPTY_PRESET:
            raise RuntimeError(f"{preset_name(n)} is still named \"{names[n].name}\" after clearing it")
        if current:
            self.read_params()  # the unit renamed the edit buffer but sent no parameters
        return names

    # --- IRs (bank 1) ---------------------------------------------------------

    def download_ir(self, index: int, bank: int = IR_BANK) -> tuple[str, list[int]]:
        """(name, samples) of an IR; samples are 24-bit ints at 48 kHz."""
        msgs = self._exchange(MSG_QUERY, QUERY_IR, bank, index,
                              until=lambda p: p[0] == MSG_IR_END)
        headers = [m for m in msgs if m[0] == MSG_IR_HEADER]
        data = [m for m in msgs if m[0] == MSG_IR_DATA]
        if len(headers) != 2:
            raise RuntimeError(f"unexpected IR download ({len(headers)} headers)")
        return ir.parse_transfer(headers[0], headers[1], data)

    def _commit(self):
        done = bytes([MSG_QUERY, STATUS, 0])
        self._exchange(MSG_QUERY, QUERY_COMMIT, until=lambda p: p[:3] == done,
                       timeout=STORE_TIMEOUT)

    def upload_ir(self, name: str, samples: list[int]) -> int:
        """Append an IR to bank 1 (a flash write). Returns its index, after
        reading it back to check it."""
        irs = self.read_cabs(IR_BANK)
        if any(c.name == name for c in irs):
            raise ValueError(f"bank {IR_BANK} already has an IR named {name}")
        stream = ir.pack_samples(samples)
        h0, h1 = ir.headers(len(irs) + 1, name, stream, len(samples), upload=True)
        ack = bytes([MSG_QUERY, IR_ACK])
        self._send(*h0)
        self._exchange(*h1, until=lambda p: p[:2] == ack, timeout=STORE_TIMEOUT)
        for chunk in ir.chunks(stream):
            self._exchange(*chunk, until=lambda p: p[:2] == ack, drain=False)
        self._exchange(MSG_IR_END, until=lambda p: p[:2] == bytes([MSG_QUERY, STATUS]),
                       timeout=STORE_TIMEOUT)
        self._commit()
        after = self.read_cabs(IR_BANK)
        index = len(after) - 1
        if len(after) != len(irs) + 1 or after[index].name != name:
            raise RuntimeError("the upload didn't show up at the end of the bank")
        if self.download_ir(index)[1] != samples:
            raise RuntimeError(f"IR {index} reads back different from what was sent")
        return index

    def delete_ir(self, index: int, name: str):
        """Delete IR `index` of bank 1 (a flash write), checking it is `name`.
        The IRs after it move down one place."""
        irs = self.read_cabs(IR_BANK)
        if not 0 <= index < len(irs) or irs[index].name != name:
            raise ValueError(f"IR {index} of bank {IR_BANK} isn't \"{name}\"")
        status = bytes([MSG_QUERY, STATUS, 0])
        self._exchange(MSG_QUERY, QUERY_IR_DELETE, index, IR_BANK,
                       until=lambda p: p[:3] == status, timeout=STORE_TIMEOUT)
        self._commit()
        expected = [c.name for c in irs[:index] + irs[index + 1:]]
        if [c.name for c in self.read_cabs(IR_BANK)] != expected:
            raise RuntimeError("the IR bank doesn't look as expected after the delete")

    def set_setup(self, option: SetupOption, value: int) -> bool:
        """Change a global setting (stored by the unit, not part of a preset).
        Returns False if it already had that value."""
        if option.name not in SETUP_OPTIONS or SETUP_OPTIONS[option.name] is not option:
            raise ValueError(f"unknown setup option {option.name}")
        if value not in option.wire_values:
            raise ValueError(f"{option.name}: bad wire value {value}")
        if option.index not in self.setup:
            self.read_setup()
        if self.setup[option.index] == value:
            return False
        echo = bytes([MSG_SETUP, option.index, value])
        self._exchange(MSG_SETUP, option.index, value, until=lambda p: p[:3] == echo)
        return True
