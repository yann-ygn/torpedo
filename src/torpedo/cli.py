import argparse
import sys

from .device import Torpedo
from .protocol import (ARCADE, ARCADE_EDITABLE, PARAMS, PRESET_MODE, PRESET_MODIFIED, PRESETS,
                       SETUP, find_param, preset_name)


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


def fmt_preset(p: int) -> str:
    return preset_name(p) + (" (modified)" if p & PRESET_MODIFIED else "")


def print_changes(before: dict[int, int], changes: dict[int, int]):
    for i, v in sorted(changes.items()):
        print(f"  {PARAMS[i].name:<20} {before.get(i, '?')} -> {v}")


def cmd_info(t: Torpedo, args):
    i = t.info()
    print(f"serial={i.serial} firmware={i.firmware}(?) preset={fmt_preset(t.preset)}")


def cmd_params(t: Torpedo, args):
    values = t.read_params()
    arcade = values[PRESET_MODE] == ARCADE
    print(f"mode: {'arcade' if arcade else 'simulation'} (PRESET_MODE={values[PRESET_MODE]})")
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
    print(f"preset {fmt_preset(t.preset)}: unsaved; `torpedo discard` reloads it")


def cmd_preset(t: Torpedo, args):
    if args.preset is not None:
        before = t.read_params()
        print_changes(before, t.select_preset(args.preset))
    print(f"preset {fmt_preset(t.preset)}")


def cmd_discard(t: Torpedo, args):
    before = t.read_params()
    print_changes(before, t.discard())
    print(f"preset {fmt_preset(t.preset)}")


def cmd_setup(t: Torpedo, args):
    values = t.read_setup()
    for i, name in enumerate(SETUP):
        v = values.get(i)
        print(f"{i:02x} {name:<24} {'?' if v is None else f'{v:#04x}'}")


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
    sub.add_parser("discard", help="drop unsaved edits (reload the current preset)").set_defaults(func=cmd_discard)
    sub.add_parser("setup", help="global setup values (read-only)").set_defaults(func=cmd_setup)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
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
