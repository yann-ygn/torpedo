"""Touch-ups to the built skin that the layout can't express: vst.json "skin_post", which gen_vst.py runs as
`python3 skin_post.py <skin folder>` once the skin is built (a non-zero exit fails the build).

mpc-vst-plugins' builder gives trigger buttons and list rows a "Mouse Down -> Toggle Switch" action on top of
their Button component, which toggles by itself when the tap is released. So one tap toggled them twice, and a
trigger fired twice, since the wrapper puts it back to 0 in between: every key typed two characters, an arrow
stepped two cabs, one CANCEL closed two levels (seen on the Key 37, 2026-10-18; on/off toggles have no such action
and toggle once). Without it they fire once, on release, like MPC's own switches.
"""

import json
import os
import sys

PREFIXES = ("shTrig_", "shRow_")  # the builder's definition keys for `button` lines and `list` rows


def single_tap(tui) -> int:
    """Drop Mouse Down -> Toggle Switch from the trigger and list row definitions in `tui`: how many."""
    fixed = 0

    def walk(o):
        nonlocal fixed
        if isinstance(o, dict):
            value = o.get("value")
            if str(o.get("key", "")).startswith(PREFIXES) and isinstance(value, dict) and "actions" in value:
                keep = [a for a in value["actions"]
                        if not (a.get("onAction") == "Mouse Down" and a.get("handler") == "Toggle Switch")]
                fixed += len(keep) != len(value["actions"])
                value["actions"] = keep
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(tui)
    return fixed


def main(skin: str) -> int:
    path = os.path.join(skin, "Plugin Skins", "TUI.json")
    with open(path, encoding="utf-8") as f:
        tui = json.load(f)
    fixed = single_tap(tui)
    if not fixed:
        print(f"skin_post: no trigger buttons or list rows in {path}: has mpc-vst-plugins' builder changed?",
              file=sys.stderr)
        return 1
    text = json.dumps(tui, separators=(",", ":"))   # as shadow_skin.py writes it
    with open(path, "w", encoding="utf-8") as f:
        f.write(text if len(text) > 8_000_000 else json.dumps(tui, indent=4))
    print(f"skin_post: {fixed} buttons and list rows fire once per tap")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
