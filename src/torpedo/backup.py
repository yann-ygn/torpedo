"""Backup and restore of the presets and global settings, as JSON.

The unit has no "send me preset n" query: each preset is read by selecting it
(0c 03 n) and reading its parameters (04 0a), then the original preset is
selected again. Restoring goes through the edit buffer: switch it to
simulation mode (which accepts every parameter), write the values, restore
the mode, check every parameter, then store it (04 73 n, a flash write).
Only presets that differ from the unit are written. Cab and IR data can't be
read back, so they aren't part of a backup.
"""

import datetime
import json
from collections.abc import Callable
from dataclasses import dataclass, field

from .device import Torpedo
from .protocol import (EMPTY_PRESET, PARAM_BY_NAME, PARAMS, PRESET_MODE, PRESETS, SETUP,
                       SETUP_OPTIONS, SIMULATION, check_name, preset_name)

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
            presets.append({"index": n, "label": preset_name(n), "name": names[n].name,
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
    skipped: list[str] = field(default_factory=list)

    def __bool__(self):
        return bool(self.setup or self.presets)


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
        if b["name"] == EMPTY_PRESET:
            plan.skipped.append(f"{b['label']}: empty in the backup, but there's no command "
                                f"to empty a slot (holds \"{c['name']}\")")
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
    mode = PARAMS[PRESET_MODE]
    try:
        for i, entry in enumerate(plan.presets):
            target = entry["params"]
            t.select_preset(entry["index"])
            t.set_param(mode, SIMULATION)  # accepts every parameter, recomputes nothing
            for p in PARAMS:
                if p.writable and p.index != PRESET_MODE:
                    t.set_param(p, target[p.name])
            t.set_param(mode, target["PRESET_MODE"])
            values = t.read_params()
            wrong = [p.name for p in PARAMS if values[p.index] != target[p.name]]
            if wrong:
                t.discard()
                raise RuntimeError(f"{entry['label']}: {', '.join(wrong)} didn't take the "
                                   "backup's value, nothing stored for it")
            t.save(entry["index"], entry["name"], overwrite=True)
            if progress:
                progress(i + 1, len(plan.presets))
    finally:
        if t.edited:
            t.discard()
        t.select_preset(start)
