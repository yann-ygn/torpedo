# torpedo

Control for the Two notes Torpedo Live from Linux (and Windows), replacing
Torpedo Remote for day-to-day edits.

```sh
uv run torpedo info                          # serial, firmware, current preset
uv run torpedo params                        # every parameter of the current preset, with ranges
uv run torpedo get arcade_contour 0x1d       # by name (case-insensitive) or index
uv run torpedo set arcade_contour=22 reverb_room=3
uv run torpedo discard                       # drop unsaved edits (reloads the preset)
uv run torpedo preset 2                      # select A002 (or `preset A002`)
uv run torpedo setup                         # global setup values (read-only)
uv run torpedo cabs                          # cab bank 0, * = current (--bank 1..4: IR banks)
uv run torpedo cab                           # current cab and its mics
uv run torpedo cab darkprinz                 # select by index, name or unique part of it
uv run torpedo ir                            # the two IR slots (IR mode: preset_mode=2)
uv run torpedo ir 1 v30-906-e                # load an IR from the ML bank into slot 1
uv run torpedo --fake params                 # simulated device, nothing is sent
```

`set` edits the current preset in the unit's memory only; nothing is saved
(saving isn't implemented yet). Every value is range-checked before anything
is sent, and in arcade mode the parameters the Torpedo would ignore are
refused (see `src/torpedo/protocol.py`).

Presets have three modes (PRESET_MODE): 0 = simulation (one cab, two mics),
1 = arcade (simplified controls driving the simulation ones), 2 = IR (two IR
slots). IR slots can load from the ML bank; the user IR banks are empty on
our unit, so they aren't supported yet.

The Torpedo has one cab and two mics (A and B) picked from that cab's own 8
mics. `cab` keeps each mic on the same mic when the new cab has it, like
Torpedo Remote; otherwise the mic index is left as is. Mic B only follows in
simulation mode, since arcade mode ignores mic B.

## Linux

hidapi talks to the kernel's `hidraw` devices, which are root-only by default.
Install the udev rule once so your user can open the Torpedo:

```sh
sudo cp linux/70-torpedo-live.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger   # then replug the Torpedo
```

## Development

```sh
uv run pytest                   # device layer against an emulated Torpedo (src/torpedo/fake.py)
```

The protocol notes are in `PROTOCOL.md`; `tools/` has the Frida capture
scripts used to observe Torpedo Remote (see `CLAUDE.md`).
