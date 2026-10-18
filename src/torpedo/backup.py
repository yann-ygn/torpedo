"""Backup and restore of the presets and global settings, as JSON.

The unit has no "send me preset n" query: each preset is read by selecting it
(0c 03 n) and reading its parameters (04 0a), then the original preset is
selected again. Restoring goes through the edit buffer
(`Torpedo.write_params`: every value, checked), then stores it (04 73 n, a
flash write).
Slots empty in the backup are emptied with 04 69 n instead, like Remote's
Delete. Only presets that differ from the unit are written. Cab and IR data can't be
read back, so they aren't part of a backup.
"""

import datetime
import json
from collections.abc import Callable
from dataclasses import dataclass, field

from .device import Torpedo
from .protocol import (EMPTY_PARAMS, EMPTY_PRESET, PARAM_BY_NAME, PARAMS, PRESETS, SETUP,
                       SETUP_OPTIONS, check_name)

FORMAT, VERSION = "torpedo-backup", 1
Progress = Callable[[int, int], None]


def _require_no_edits(t: Torpedo):
    if t.edited:
        raise ValueError("the current preset has unsaved edits, and reading or restoring "
                         "presets would discard them: `torpedo save` or `torpedo discard` first")


def read_state(t: Torpedo, progress: Progress | None = None) -> dict:
    """Everything a backup holds, read from the unit."""
    _require_no_edits(t)
    start = t.preset
    info = t.info()
    names = t.read_names()
    setup = t.read_setup()
    presets = []
    try:
        for n in range(PRESETS):
            t.select_preset(n)
            values = t.read_params()
            presets.append({"index": n, "label": names[n].label, "name": names[n].name,
                            "arcade": names[n].arcade,
                            "params": {p.name: values[p.index] for p in PARAMS}})
            if progress:
                progress(n + 1, PRESETS)
    finally:
        t.select_preset(start)
    return {"format": FORMAT, "version": VERSION,
            "date": datetime.datetime.now().isoformat(timespec="seconds"),
            "serial": info.serial, "firmware": info.firmware, "current_preset": start,
            "setup": {o.name: setup[o.index] for o in SETUP_OPTIONS.values()},
            "setup_raw": {name: setup.get(i) for i, name in enumerate(SETUP)},
            "presets": presets}


def save_file(state: dict, path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
        f.write("\n")


def load_file(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        state = json.load(f)
    if state.get("format") != FORMAT or state.get("version") != VERSION:
        raise ValueError(f"{path} is not a {FORMAT} v{VERSION} file")
    if [p["index"] for p in state["presets"]] != list(range(PRESETS)):
        raise ValueError(f"{path}: expected presets 0..{PRESETS - 1}")
    for p in state["presets"]:
        if set(p["params"]) != set(PARAM_BY_NAME):
            raise ValueError(f"{path}: {p['label']} doesn't have the {len(PARAMS)} parameters")
    for name, v in state["setup"].items():
        if name not in SETUP_OPTIONS or v not in SETUP_OPTIONS[name].wire_values:
            raise ValueError(f"{path}: bad setup value {name}={v}")
    return state


@dataclass
class Plan:
    setup: dict[str, int] = field(default_factory=dict)  # option -> wire value
    presets: list[dict] = field(default_factory=list)  # backup entries to store
    clears: list[dict] = field(default_factory=list)  # empty in the backup: clear the slot
    skipped: list[str] = field(default_factory=list)

    def __bool__(self):
        return bool(self.setup or self.presets or self.clears)


def _problem(entry: dict) -> str | None:
    """Why a backup entry can't be written, if it can't."""
    try:
        check_name(entry["name"])
    except ValueError as e:
        return str(e)
    for p in PARAMS:
        v = entry["params"][p.name]
        if p.writable:
            try:
                p.check(v)
            except ValueError as e:
                return str(e)
    return None


def plan_restore(backup: dict, current: dict, presets: set[int] | None = None,
                 setup: bool = True) -> Plan:
    """What restoring `backup` onto a unit in state `current` would change."""
    plan = Plan()
    if setup:
        plan.setup = {k: v for k, v in backup["setup"].items() if current["setup"][k] != v}
    for b, c in zip(backup["presets"], current["presets"]):
        if presets is not None and b["index"] not in presets:
            continue
        if (b["name"], b["params"]) == (c["name"], c["params"]):
            continue
        if b["name"] == EMPTY_PRESET and [b["params"][p.name] for p in PARAMS] == EMPTY_PARAMS:
            plan.clears.append(b)
            continue
        read_only = [p.name for p in PARAMS
                     if not p.writable and b["params"][p.name] != c["params"][p.name]]
        problem = _problem(b) or (f"read-only {', '.join(read_only)} differ" if read_only else None)
        if problem:
            plan.skipped.append(f"{b['label']}: {problem}")
        else:
            plan.presets.append(b)
    return plan


def apply_restore(t: Torpedo, plan: Plan, progress: Progress | None = None):
    """Write `plan` to the unit. Each preset is checked before it is stored."""
    _require_no_edits(t)
    start = t.preset
    for name, v in plan.setup.items():
        t.set_setup(SETUP_OPTIONS[name], v)
    for i, entry in enumerate(plan.clears):
        t.clear_preset(entry["index"])
        if progress:
            progress(i + 1, len(plan.clears) + len(plan.presets))
    try:
        for i, entry in enumerate(plan.presets):
            t.select_preset(entry["index"])
            try:
                t.write_params({p.index: entry["params"][p.name] for p in PARAMS})
            except RuntimeError as e:
                t.discard()
                raise RuntimeError(f"{entry['label']}: {e}, nothing stored for it") from None
            t.save(entry["index"], entry["name"], overwrite=True)
            if progress:
                progress(len(plan.clears) + i + 1, len(plan.clears) + len(plan.presets))
    finally:
        if t.edited:
            t.discard()
        t.select_preset(start)
