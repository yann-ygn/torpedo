"""Single presets as JSON files, to keep or share sounds (`torpedo export` /
`torpedo import`).

The cab, mics and IRs are stored by name: their bank positions can differ
between units and firmware versions. Import looks them up again and refuses a
file naming one this unit doesn't have (Torpedo Remote would fetch a "sample"
from the Two notes store instead, which we leave alone). Only what the
preset's mode uses has to be there: the cab and mic A (and mic B in
simulation mode), or the two IRs in IR mode; the rest keeps its default when
missing. Backups (backup.py) keep bank positions instead, since they are the
state of one unit.
"""

import datetime
import json

from .device import FOLDERS, MIC, MIC_2, SPEAKER, WAVES, Torpedo
from .protocol import (ARCADE, EMPTY_PARAMS, IR, PARAM_BY_NAME, PARAMS, PRESET_MODE, Cab,
                       check_name)

FORMAT, VERSION = "torpedo-preset", 1
PREVIEW = PARAM_BY_NAME["SIMUHP_PREVIEW"].index
BY_NAME = frozenset({SPEAKER, MIC, MIC_2, *WAVES, *FOLDERS, PREVIEW})  # not in "params"
STORED = [p.name for p in PARAMS if p.index not in BY_NAME]


def export(t: Torpedo, n: int | None = None) -> dict:
    """Preset `n`, or the current sound (unsaved edits included) by default.
    Another preset is read by selecting it, then the current one comes back."""
    if n is None:
        return _describe(t, t.read_buffer_name())
    if t.edited:
        raise ValueError("the current preset has unsaved edits, and exporting another preset "
                         "would select it: save or discard them first (or export the current "
                         "sound without naming a preset)")
    start = t.preset
    name = t.read_names()[n].name
    try:
        t.select_preset(n)
        return _describe(t, name)
    finally:
        t.select_preset(start)


def _describe(t: Torpedo, name: str) -> dict:
    values = t.read_params()
    cabs, irs = t.read_cabs(), t.read_irs()
    cab = cabs[values[SPEAKER]] if values[SPEAKER] < len(cabs) else None

    def mic(m: int) -> str:
        return cab.mics[values[m]] if cab and values[m] < len(cab.mics) else ""

    def ir(slot: int) -> str:
        i = values[WAVES[slot]]
        return irs[i].name if values[FOLDERS[slot]] == 0 and i < len(irs) else ""
    info = t.info()
    return {"format": FORMAT, "version": VERSION, "name": name,
            "date": datetime.datetime.now().isoformat(timespec="seconds"),
            "serial": info.serial, "firmware": info.firmware,
            "cab": cab.name if cab else "", "mics": [mic(MIC), mic(MIC_2)],
            "irs": [ir(0), ir(1)],
            "params": {p: values[PARAM_BY_NAME[p].index] for p in STORED}}


def save_file(preset: dict, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(preset, f, indent=1)
        f.write("\n")


def load_file(path) -> dict:
    with open(path, encoding="utf-8") as f:
        preset = json.load(f)
    if not isinstance(preset, dict) or preset.get("format") != FORMAT:
        raise ValueError(f"{path} is not a {FORMAT} file")
    if preset.get("version") != VERSION:
        raise ValueError(f"{path}: {FORMAT} version {preset.get('version')}, "
                         f"this program reads version {VERSION}")
    check_name(preset.get("name", ""))
    names = [preset.get("cab"), *preset.get("mics", []), *preset.get("irs", [])]
    if len(names) != 5 or not all(isinstance(s, str) for s in names):
        raise ValueError(f"{path}: expected a cab name and two mic and two IR names")
    if set(preset.get("params", {})) != set(STORED):
        raise ValueError(f"{path}: expected the {len(STORED)} parameters {', '.join(STORED)}")
    for name, v in preset["params"].items():
        p = PARAM_BY_NAME[name]
        if p.writable:
            p.check(v)
    return preset


def resolve(preset: dict, cabs: list[Cab], irs: list[Cab]) -> dict[int, int]:
    """Every parameter's value on this unit, the names looked up in its banks.
    Raises ValueError naming whatever the preset's mode needs and the unit lacks."""
    values = dict(enumerate(EMPTY_PARAMS))
    values |= {PARAM_BY_NAME[name].index: v for name, v in preset["params"].items()}
    mode = values[PRESET_MODE]
    needed_mics = () if mode == IR else (MIC,) if mode == ARCADE else (MIC, MIC_2)
    missing = []
    cab = next((c for c in cabs if c.name == preset["cab"]), None)
    if cab is not None:
        values[SPEAKER] = cab.index
        for m, name in zip((MIC, MIC_2), preset["mics"]):
            if name and name in cab.mics:
                values[m] = cab.mics.index(name)
            elif m in needed_mics:
                missing.append(f"mic \"{name}\" on cab \"{cab.name}\"")
    elif mode != IR:
        missing.append(f"cab \"{preset['cab']}\"")
    for slot, name in enumerate(preset["irs"]):
        ir = next((c for c in irs if c.name == name), None)
        if ir is not None:
            values[WAVES[slot]], values[FOLDERS[slot]] = ir.index, 0
        elif mode == IR:
            missing.append(f"IR \"{name}\"")
    if missing:
        raise ValueError(f"this unit has no {', '.join(missing)}")
    return values


def import_preset(t: Torpedo, preset: dict, n: int | None = None, overwrite: bool = False) -> str:
    """Make `preset` the current sound, named after it (unsaved), or store it
    into preset `n` (a flash write; refused over a non-empty slot unless
    `overwrite`). Nothing is sent if a cab, mic or IR is missing. Returns the name."""
    if t.edited:
        raise ValueError("the current preset has unsaved edits, and importing would replace "
                         "them: save or discard them first")
    name = check_name(preset["name"])
    if n is not None:
        target = t.read_names()[n]
        if not overwrite and not target.empty:
            raise ValueError(f"{target.label} holds \"{target.name}\": "
                             "pass overwrite=True to replace it")
    values = resolve(preset, t.read_cabs(), t.read_irs())
    t.write_params(values)
    if n is None:
        t.rename_buffer(name)
    else:
        t.save(n, name, overwrite=True)
    return name
