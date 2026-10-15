"""Which parameters does the device accept in arcade vs simulation mode?

For each mode and each writable parameter: reload the current preset, set the
mode, write the value one step away, record accepted/ignored and the linked
parameters the device recomputed, then reload the preset again.

Usage: uv run python tools/modeprobe.py
"""
from torpedo.device import IgnoredWrite, Torpedo
from torpedo.protocol import PARAMS, PRESET_MODE, PRESET_MODIFIED

MODES = {"arcade": 1, "sim": 0, "ir": 2}
mode_param = PARAMS[PRESET_MODE]

with Torpedo() as t:
    t.timeout = 0.6
    if t.preset & PRESET_MODIFIED:
        raise SystemExit("the current preset has unsaved edits: probing would discard them")
    base = t.preset
    results = {}
    for mode, mv in MODES.items():
        for p in PARAMS:
            if not p.writable or p.index == PRESET_MODE or p.lo == p.hi:
                continue
            t.discard()
            t.set_param(mode_param, mv)
            cur = t.params[p.index]
            new = cur + 1 if cur < p.hi else cur - 1
            try:
                changes = t.set_param(p, new)
                linked = sorted(PARAMS[i].name for i in changes if i != p.index)
                results[mode, p.index] = "ok" + (f" -> {', '.join(linked)}" if linked else "")
            except IgnoredWrite:
                results[mode, p.index] = "IGNORED"
            print(f"{mode:<6} {p.name:<20} {cur}->{new}: {results[mode, p.index]}", flush=True)
    t.select_preset(base)
    t.discard()
    print("\nback on preset", t.preset, "\n")
    for p in PARAMS:
        if (r := results.get(("arcade", p.index))) is not None:
            print(f"{p.index:02x} {p.name:<20} arcade: {r:<36} sim: {results['sim', p.index]:<8}"
                  f" ir: {results['ir', p.index]}")
