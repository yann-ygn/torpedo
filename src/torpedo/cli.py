import argparse
import os
import sys

from . import backup, ir, presetfile
from .device import FOLDERS, MIC, MIC_2, MUTE, MUTE_2, SPEAKER, WAVES, Torpedo
from .protocol import (ARCADE, ARCADE_EDITABLE, EMPTY_PRESET, FOLDER_BANK_OFFSET, IR, MODE_NAMES,
                       PARAM_BY_NAME, PARAMS, PRESET_MODE, PRESETS, SETUP, SETUP_OPTIONS, Cab,
                       find_param, preset_name)


def parse_assignment(s: str):
    name, sep, value = s.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError(f"expected NAME=VALUE, got '{s}'")
    param = parse_param(name)
    try:
        v = int(value, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad value '{value}' for {param.name}") from None
    try:
        param.check(v)  # validate everything before anything is sent
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None
    return param, v


def parse_param(s: str):
    try:
        return find_param(s)
    except KeyError as e:
        raise argparse.ArgumentTypeError(e.args[0]) from None


def parse_preset(s: str) -> int:
    """`A001` or `1` -> index 0."""
    t = s.upper().removeprefix("A")
    if t.isdigit() and 1 <= int(t) <= PRESETS:
        return int(t) - 1
    raise argparse.ArgumentTypeError(f"preset must be 1..{PRESETS} or A001..A{PRESETS:03d}")


def fmt_preset(t: Torpedo) -> str:
    return preset_name(t.source) + (" (modified)" if t.edited else "")


def print_changes(before: dict[int, int], changes: dict[int, int]):
    for i, v in sorted(changes.items()):
        print(f"  {PARAMS[i].name:<20} {before.get(i, '?')} -> {v}")


def cmd_info(t: Torpedo, args):
    i = t.info()
    print(f"serial={i.serial} firmware={i.firmware}(?) preset={fmt_preset(t)}")


def cmd_params(t: Torpedo, args):
    values = t.read_params()
    arcade = values[PRESET_MODE] == ARCADE
    print(f"mode: {MODE_NAMES.get(values[PRESET_MODE], '?')} (PRESET_MODE={values[PRESET_MODE]})")
    for p in PARAMS:
        rng = f"{p.lo}..{p.hi}" if p.writable else "read-only"
        if p.writable and arcade and p.index not in ARCADE_EDITABLE:
            rng += "  simulation mode only"
        print(f"{p.index:02x} {p.name:<20} {values.get(p.index, '?'):>3}  {rng}")


def cmd_get(t: Torpedo, args):
    values = t.read_params()
    for p in args.params:
        print(f"{p.name}={values[p.index]}")


def cmd_set(t: Torpedo, args):
    before = t.read_params()
    for param, value in args.assignments:
        print_changes(before, t.set_param(param, value))
        before = dict(t.params)
    print(f"preset {fmt_preset(t)}: unsaved; `torpedo discard` reloads it, `torpedo save` keeps it")


def cmd_preset(t: Torpedo, args):
    if args.preset is not None:
        before = t.read_params()
        print_changes(before, t.select_preset(args.preset))
    print(f"preset {fmt_preset(t)}")


def cmd_discard(t: Torpedo, args):
    before = t.read_params()
    print_changes(before, t.discard())
    print(f"preset {fmt_preset(t)}")


def cmd_presets(t: Torpedo, args):
    names = t.read_names()
    current = t.source
    for p in names[:PRESETS]:
        if p.empty and not args.all:
            continue
        mark = "*" if p.index == current else " "
        kind = "" if p.empty else "arcade" if p.arcade else "simulation"  # as Remote labels them
        print(f"{mark} {preset_name(p.index)} {p.name:<26} {kind}")
    hidden = sum(p.empty for p in names[:PRESETS])
    if hidden and not args.all:
        print(f"  ({hidden} empty USER slots hidden, first {preset_name(first_empty(names))}; --all shows them)")
    print(f"current: {fmt_preset(t)} \"{names[-1].name}\"")


def first_empty(names) -> int:
    return next(p.index for p in names[:PRESETS] if p.empty)


def cmd_save(t: Torpedo, args):
    names = t.read_names()
    slot = args.preset if args.preset is not None else first_empty(names)
    if not names[slot].empty and not args.overwrite:
        sys.exit(f"error: {preset_name(slot)} holds \"{names[slot].name}\"; "
                 "add --overwrite to replace it")
    saved = t.save(slot, args.name, overwrite=args.overwrite, names=names)
    print(f"saved {preset_name(saved.index)} \"{saved.name}\"")


def cmd_rename(t: Torpedo, args):
    if t.edited:
        sys.exit("error: the current preset has unsaved edits, and renaming saves them too: "
                 "`torpedo save` or `torpedo discard` first")
    saved = t.save(t.source, args.name, overwrite=True)
    print(f"renamed {preset_name(saved.index)} to \"{saved.name}\"")


def cmd_clear(t: Torpedo, args):
    names = t.read_names()
    n = args.preset
    if not names[n].empty and not args.yes:
        sys.exit(f"error: {preset_name(n)} holds \"{names[n].name}\"; add --yes to empty it "
                 "(flash write)")
    t.clear_preset(n)
    print(f"cleared {preset_name(n)} (was \"{names[n].name}\"): now an empty USER slot")


def cmd_export(t: Torpedo, args):
    preset = presetfile.export(t, args.preset)
    presetfile.save_file(preset, args.file)
    what = "the current sound" if args.preset is None else preset_name(args.preset)
    print(f"exported {what} \"{preset['name']}\" to {args.file}")


def cmd_import(t: Torpedo, args):
    preset = presetfile.load_file(args.file)
    if args.preset is not None:
        names = t.read_names()
        if not names[args.preset].empty and not args.overwrite:
            sys.exit(f"error: {preset_name(args.preset)} holds \"{names[args.preset].name}\"; "
                     "add --overwrite to replace it")
    name = presetfile.import_preset(t, preset, args.preset, overwrite=args.overwrite)
    if args.preset is None:
        print(f"loaded \"{name}\" as the current sound (unsaved: `torpedo save` stores it)")
    else:
        print(f"imported \"{name}\" into {preset_name(args.preset)}")


def parse_setup(s: str):
    name, sep, value = s.partition("=")
    option = SETUP_OPTIONS.get(name.lower().replace("-", "_"))
    if not sep or option is None:
        raise argparse.ArgumentTypeError(f"expected NAME=VALUE with NAME one of: {', '.join(SETUP_OPTIONS)}")
    try:
        return option, option.encode(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def cmd_setup(t: Torpedo, args):
    values = t.read_setup()
    for option, v in args.assignments:
        if t.set_setup(option, v):
            print(f"  {option.name:<13} {option.decode(values[option.index])} -> {option.decode(v)}")
    if args.assignments:
        return
    for option in SETUP_OPTIONS.values():
        print(f"{option.name:<13} {option.decode(values[option.index])}")
    if args.raw:
        for i, name in enumerate(SETUP):
            v = values.get(i)
            print(f"  {i:02x} {name:<24} {'?' if v is None else f'{v:#04x}'}")


def find_cab(cabs: list[Cab], key: str, listing: str = "cabs") -> Cab:
    """By index (`18`, `0x12`), name (case-insensitive) or a unique part of the name.
    A number that isn't a valid index is matched against the names (`906`)."""
    try:
        i = int(key, 0)
    except ValueError:
        i = -1
    if 0 <= i < len(cabs):
        return cabs[i]
    k = key.lower()
    found = ([c for c in cabs if c.name.lower() == k]
             or [c for c in cabs if k in c.name.lower()])
    if len(found) == 1:
        return found[0]
    if not found:
        raise ValueError(f"'{key}' not found (see `torpedo {listing}`)")
    names = ", ".join(c.name for c in found[:8]) + (f", ... ({len(found)} matches)" if len(found) > 8 else "")
    raise ValueError(f"'{key}' is ambiguous: {names}")


SLOT_PARAMS = [("SIMUHP_LEVEL", "SIMUHP_PHASE", "SIMUHP_MUTE"),
               ("SIMUHP_LEVEL_2", "SIMUHP_PHASE_2", "SIMUHP_MUTE_2")]


def cmd_ir(t: Torpedo, args):
    irs = t.read_irs()
    before = t.read_params()
    if args.ir is not None:
        print_changes(before, t.select_ir(args.slot - 1, find_cab(irs, args.ir, "irs").index, irs))
    mode = t.params[PRESET_MODE]
    for n, (wave, names) in enumerate(zip(WAVES, SLOT_PARAMS)):
        level, phase, mute = (t.params[PARAM_BY_NAME[p].index] for p in names)
        i = t.params[wave]
        name = irs[i].name if i < len(irs) else "?"
        print(f"slot {n + 1}: {i} {name}  level={level}" + ("  phase inverted" if phase else "")
              + ("  muted" if mute else ""))
    if mode != IR:
        print(f"(preset is in {MODE_NAMES.get(mode, '?')} mode: the IR slots play in ir mode, "
              "`torpedo set preset_mode=2`)")


def cmd_cabs(t: Torpedo, args):
    current = t.read_params()[SPEAKER]
    for c in t.read_cabs():
        mark = "*" if c.index == current else " "
        print(f"{mark} {c.index:3d} {c.name:<28} {c.maker}")


def cmd_irs(t: Torpedo, args):
    """The IR bank, with the IR slots that use each IR. Banks 2-4 (user IR
    folders 1-3) are listed too when they hold anything."""
    params = t.read_params()
    used = {}  # (folder, index) -> slots
    for slot, (folder, wave) in enumerate(zip(FOLDERS, WAVES), 1):
        used.setdefault((params[folder], params[wave]), []).append(str(slot))
    for folder in range(4):
        irs = t.read_irs(folder)
        if not irs and folder:
            continue
        if folder:
            print(f"folder {folder} (bank {folder + FOLDER_BANK_OFFSET}):")
        for c in irs:
            slots = used.get((folder, c.index), [])
            mark = ("slot " + "+".join(slots)) if slots else ""
            print(f"  {c.index:3d} {c.name:<28} {mark}")
    note = "" if params[PRESET_MODE] == IR else f" (the preset is in {MODE_NAMES.get(params[PRESET_MODE], '?')} mode)"
    print(f"IR mode's slots: `torpedo ir`{note}")


def cmd_cab(t: Torpedo, args):
    cabs = t.read_cabs()
    before = t.read_params()
    if args.cab is not None:
        print_changes(before, t.select_cab(find_cab(cabs, args.cab).index, cabs))
    cab = cabs[t.params[SPEAKER]]
    muted = {MIC: t.params[MUTE], MIC_2: t.params[MUTE_2]}
    print(f"cab {cab.index}: {cab.name} ({cab.maker})")
    for label, m in (("A", MIC), ("B", MIC_2)):
        print(f"  mic {label}: {cab.mics[t.params[m]] or '-'} ({t.params[m]})"
              + ("  muted" if muted[m] else ""))
    print("  mics: " + ", ".join(f"{i} {m}" for i, m in enumerate(cab.mics) if m))


def cmd_ir_get(t: Torpedo, args):
    irs = t.read_irs()
    entry = find_cab(irs, args.ir, "irs")
    name, samples = t.download_ir(entry.index)
    ir.write_wav(args.file, samples)
    print(f"IR {entry.index} \"{name}\" ({len(samples)} samples, 48 kHz) -> {args.file}")


def cmd_ir_put(t: Torpedo, args):
    rate, values = ir.read_wav(args.file)
    name = args.name or os.path.splitext(os.path.basename(args.file))[0]
    if not 1 <= len(name) <= ir.NAME_MAX or not name.isascii() or not name.isprintable():
        sys.exit(f"error: IR name must be 1..{ir.NAME_MAX} printable ASCII characters (--name)")
    samples = ir.prepare(values, rate, args.length, args.align)
    index = t.upload_ir(name, samples)
    print(f"uploaded \"{name}\" as IR {index} ({args.length} ms, {args.align}); checked by reading it back")


def cmd_ir_delete(t: Torpedo, args):
    irs = t.read_irs()
    matches = [c for c in irs if c.name == args.name]
    if len(matches) != 1:
        sys.exit(f"error: no IR named exactly \"{args.name}\" (see `torpedo irs`)")
    index = matches[0].index
    if index < len(irs) - 1 and not args.yes:
        sys.exit(f"error: IRs {index + 1}..{len(irs) - 1} would move down one place, and presets "
                 "using them would then point at the wrong IR; add --yes to delete anyway")
    t.delete_ir(index, args.name)
    print(f"deleted IR {index} \"{args.name}\"")


def show_progress(what: str):
    def progress(done: int, total: int):
        print(f"\r{what} {done}/{total}", end="" if done < total else "\n", flush=True)
    return progress


def parse_preset_list(s: str) -> set[int]:
    """`A001,A076` or `1,76` -> {0, 75}."""
    return {parse_preset(x) for x in s.split(",") if x}


def cmd_backup(t: Torpedo, args):
    state = backup.read_state(t, show_progress("reading presets"))
    backup.save_file(state, args.file)
    used = sum(p["name"] != EMPTY_PRESET for p in state["presets"])
    print(f"saved {used} presets (+{len(state['presets']) - used} empty slots) and the "
          f"global settings to {args.file}")


def cmd_restore(t: Torpedo, args):
    saved = backup.load_file(args.file)
    if saved["serial"] != t.info().serial:
        print(f"note: backup from unit {saved['serial']}: cab/IR numbers may not match this one")
    current = backup.read_state(t, show_progress("reading the unit"))
    plan = backup.plan_restore(saved, current, args.presets, setup=not args.no_setup)
    for name, v in plan.setup.items():
        option = SETUP_OPTIONS[name]
        print(f"  setup {name}: {option.decode(current['setup'][name])} -> {option.decode(v)}")
    by_index = {p["index"]: p for p in current["presets"]}
    for entry in plan.clears:
        print(f"  {entry['label']}: \"{by_index[entry['index']]['name']}\" -> cleared ({EMPTY_PRESET})")
    for entry in plan.presets:
        now = by_index[entry["index"]]
        changed = sum(entry["params"][k] != v for k, v in now["params"].items())
        print(f"  {entry['label']}: \"{now['name']}\" -> \"{entry['name']}\""
              f" ({changed} parameters differ)")
    for line in plan.skipped:
        print(f"  skipped {line}")
    if not plan:
        print("nothing to restore: the unit matches the backup")
    elif not args.apply:
        print(f"dry run: {len(plan.presets) + len(plan.clears)} presets would be stored or cleared "
              "(flash writes, ~3 s each); add --apply to do it")
    else:
        backup.apply_restore(t, plan, show_progress("storing presets"))
        print("restored")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="torpedo", description="Control a Two notes Torpedo Live over USB")
    ap.add_argument("--fake", action="store_true", help="simulated device, nothing is sent to hardware")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="serial, firmware, current preset").set_defaults(func=cmd_info)
    sub.add_parser("params", help="all parameters of the current preset").set_defaults(func=cmd_params)
    p = sub.add_parser("get", help="read parameters by name or index")
    p.add_argument("params", nargs="+", type=parse_param, metavar="NAME")
    p.set_defaults(func=cmd_get)
    p = sub.add_parser("set", help="edit the current preset (not saved): NAME=VALUE ...")
    p.add_argument("assignments", nargs="+", type=parse_assignment, metavar="NAME=VALUE")
    p.set_defaults(func=cmd_set)
    p = sub.add_parser("preset", help="show or select the current preset (1 = A001)")
    p.add_argument("preset", nargs="?", type=parse_preset)
    p.set_defaults(func=cmd_preset)
    sub.add_parser("discard", help="leave unsaved edits (reload the stored preset; the unit keeps them as A001* etc.)").set_defaults(func=cmd_discard)
    p = sub.add_parser("setup", help="show or change global settings: NAME=VALUE ...")
    p.add_argument("assignments", nargs="*", type=parse_setup, metavar="NAME=VALUE")
    p.add_argument("--raw", action="store_true", help="also dump every setup value")
    p.set_defaults(func=cmd_setup)
    sub.add_parser("cabs", help="list the cabs (* = current)").set_defaults(func=cmd_cabs)
    sub.add_parser("irs", help="list the IRs, with the IR mode slots using them").set_defaults(func=cmd_irs)
    p = sub.add_parser("cab", help="show or select the cab (keeps the same mic when it can)")
    p.add_argument("cab", nargs="?", metavar="CAB", help="index, name or part of the name")
    p.set_defaults(func=cmd_cab)
    p = sub.add_parser("ir", help="show the IR slots, or load an IR from the ML bank into one")
    p.add_argument("slot", nargs="?", type=int, choices=(1, 2))
    p.add_argument("ir", nargs="?", metavar="IR", help="index, name or part of the name")
    p.set_defaults(func=cmd_ir)
    p = sub.add_parser("presets", help="list the stored presets")
    p.add_argument("--all", action="store_true", help="also list the empty USER slots")
    p.set_defaults(func=cmd_presets)
    p = sub.add_parser("save", help="store the current sound into a preset (default: first empty USER slot)")
    p.add_argument("preset", nargs="?", type=parse_preset)
    p.add_argument("--name", help="preset name (A-Z, 0-9, space; default: keep the current one)")
    p.add_argument("--overwrite", action="store_true", help="allow replacing a non-empty preset")
    p.set_defaults(func=cmd_save)
    p = sub.add_parser("backup", help="save all presets and global settings to a JSON file (~30 s)")
    p.add_argument("file")
    p.set_defaults(func=cmd_backup)
    p = sub.add_parser("restore", help="show (or --apply) what restoring a backup would change")
    p.add_argument("file")
    p.add_argument("--presets", type=parse_preset_list, metavar="A001,A076",
                   help="only these presets (default: all)")
    p.add_argument("--no-setup", action="store_true", help="leave the global settings alone")
    p.add_argument("--apply", action="store_true", help="really write (stores presets to flash)")
    p.set_defaults(func=cmd_restore)
    p = sub.add_parser("ir-get", help="download an IR from the IR bank to a WAV file")
    p.add_argument("ir", metavar="IR", help="index, name or part of the name")
    p.add_argument("file")
    p.set_defaults(func=cmd_ir_get)
    p = sub.add_parser("ir-put", help="upload a WAV to the end of the IR bank (flash write)")
    p.add_argument("file")
    p.add_argument("--name", help="IR name (default: the file name)")
    p.add_argument("--length", type=int, choices=(20, 40), default=20, help="ms (default 20)")
    p.add_argument("--align", choices=ir.ALIGNS, default="original",
                   help="min-latency drops the silence before the onset; min-phase converts "
                        "to minimum phase (peak at sample 4)")
    p.set_defaults(func=cmd_ir_put)
    p = sub.add_parser("ir-delete", help="delete an IR from the IR bank (flash write)")
    p.add_argument("name", help="the IR's exact name")
    p.add_argument("--yes", action="store_true", help="also when IRs after it get renumbered")
    p.set_defaults(func=cmd_ir_delete)
    p = sub.add_parser("rename", help="rename the current preset (must have no unsaved edits)")
    p.add_argument("name")
    p.set_defaults(func=cmd_rename)
    p = sub.add_parser("clear", help="empty a preset like Remote's Delete: USER, default sound (flash write)")
    p.add_argument("preset", type=parse_preset)
    p.add_argument("--yes", action="store_true", help="needed unless the slot is already empty")
    p.set_defaults(func=cmd_clear)
    p = sub.add_parser("export", help="save the current sound (or a preset) to a preset file")
    p.add_argument("file")
    p.add_argument("preset", nargs="?", type=parse_preset)
    p.set_defaults(func=cmd_export)
    p = sub.add_parser("import", help="load a preset file as the current sound, or store it into PRESET")
    p.add_argument("file")
    p.add_argument("preset", nargs="?", type=parse_preset)
    p.add_argument("--overwrite", action="store_true", help="allow replacing a non-empty preset")
    p.set_defaults(func=cmd_import)
    sub.add_parser("tui", help="interactive terminal interface")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd == "tui":
        from .tui import run
        return run(fake=args.fake)
    try:
        if args.fake:
            from .fake import FakeHid
            t = Torpedo(FakeHid())
        else:
            t = Torpedo()
        with t:
            args.func(t, args)
    except (RuntimeError, TimeoutError, ValueError) as e:
        sys.exit(f"error: {e}")
