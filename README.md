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
uv run torpedo --fake params                 # simulated device, nothing is sent
```

`set` edits the current preset in the unit's memory only; nothing is saved
(saving isn't implemented yet). Every value is range-checked before anything
is sent. Ranges are provisional except ARCADE_CONTOUR (see
`src/torpedo/protocol.py`), and parameters whose meaning isn't mapped yet
(cab/IR selection) are read-only.

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
