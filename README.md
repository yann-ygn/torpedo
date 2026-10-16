# torpedo

Control for the Two notes Torpedo Live from Linux (and Windows), replacing
Torpedo Remote for day-to-day edits.

```sh
uv run torpedo info                          # serial, firmware, current preset
uv run torpedo params                        # every parameter of the current preset, with ranges
uv run torpedo get arcade_contour 0x1d       # by name (case-insensitive) or index
uv run torpedo set arcade_contour=22 reverb_room=3
uv run torpedo discard                       # leave unsaved edits (reloads the preset; the unit keeps them as A001*)
uv run torpedo preset 2                      # select A002 (or `preset A002`)
uv run torpedo presets                       # preset names (--all: also the empty USER slots)
uv run torpedo save --name "my crunch"       # store into the first empty USER slot
uv run torpedo save A080 --overwrite         # replace a preset (refused without --overwrite)
uv run torpedo rename "my crunch 2"          # rename the current preset (no unsaved edits)
uv run torpedo clear A080 --yes              # empty a preset like Remote's Delete (USER, default sound)
uv run torpedo ir-get v30-906-e out.wav       # download an IR (float WAV, like Remote's export)
uv run torpedo ir-put my-cab.wav --align min-phase     # upload a WAV (any rate) to the IR bank
uv run torpedo ir-delete MY-CAB              # exact name; refuses if later IRs would renumber
uv run torpedo export presets/crunch.json      # the current sound (unsaved edits included) to a preset file
uv run torpedo export presets/a002.json A002   # or a stored preset
uv run torpedo import presets/crunch.json      # load it as the current sound (unsaved; then `save`)
uv run torpedo import presets/crunch.json A080 # or store it into a slot (--overwrite if used)
uv run torpedo backup backup/today.json      # all presets + global settings (~20 s)
uv run torpedo restore backup/today.json     # dry run: what differs (--apply writes it)
uv run torpedo restore backup/today.json --presets A076,A080 --no-setup --apply
uv run torpedo setup                         # global settings (--raw: every setup value)
uv run torpedo setup midi_channel=1 output=wet/wet spdif_freq=96 level=85 mute=off
uv run torpedo cabs                          # the cabs, * = current
uv run torpedo irs                           # the IRs, with the IR mode slots using them
uv run torpedo cab                           # current cab and its mics
uv run torpedo cab darkprinz                 # select by index, name or unique part of it
uv run torpedo ir                            # the two IR slots (IR mode: preset_mode=2)
uv run torpedo ir 1 v30-906-e                # load an IR from the ML bank into slot 1
uv run torpedo --fake params                 # simulated device, nothing is sent
uv run torpedo tui                           # terminal interface (--fake tui: simulated)
```

`set` edits the current preset in the unit's memory only, until `save`
stores it. `save` writes into an empty "USER" slot unless told to
`--overwrite`; names are uppercased, A-Z, 0-9 and spaces, up to 25
characters (what the factory names use). Every value is range-checked before anything
is sent, and in arcade mode the parameters the Torpedo would ignore are
refused (see `src/torpedo/protocol.py`). `clear` empties a preset the way
Remote's Delete does: it becomes a "USER" slot with the default sound every
empty slot holds (Brit VintC, one Dyn 57, amp/EQ/reverb off); `--yes` is
needed unless it is already empty.

Presets have three modes (PRESET_MODE): 0 = simulation (one cab, two mics),
1 = arcade (simplified controls driving the simulation ones), 2 = IR (two IR
slots). IR slots load from the IR bank (the ML IRs and your uploads).

The Torpedo has one cab and two mics (A and B) picked from that cab's own 8
mics. `cab` keeps each mic on the same mic when the new cab has it, like
Torpedo Remote; otherwise the mic index is left as is. Mic B only follows in
simulation mode, since arcade mode ignores mic B.

## TUI

`torpedo tui` shows the presets on the left and the current preset on the
right, laid out like Torpedo Remote's rack for the preset's mode. ↑/↓ move
between rows, ←/→ change a value (shift: ×10), Enter types a number or opens
a filterable list (cab, mic, IR, amp, room), Space toggles. `s` saves, `r`
renames, `d` discards, Del clears the highlighted preset, `/` filters the presets. In arcade mode, the
simulation values arcade computes are listed under its controls, read-only.
Changes made on the unit's front panel show up (highlighted for a second),
and the TUI reconnects by itself after a replug.

## IRs

IRs live in bank 1 (`torpedo irs`), after the 86 ML IRs, and are
played by IR mode's two slots (`torpedo ir`). `ir-put` does what Torpedo
Remote does to a file: resampling to 48 kHz, then `--align` (`original`;
`min-latency` trims the silence before the onset; `min-phase` converts to
minimum phase with the peak at sample 4, like the ML IRs), cut or pad to 20
or 40 ms (`--length`), peak normalization, then it reads the upload back to
check it. Min phase gives Remote's samples to within its own rounding;
resampling is within -76 dB of Remote's (its filter isn't known). Plain
Python, no extra dependency: min phase on a 1 s file takes a few seconds. Deleting an IR moves the following
ones down a place, and presets using them would then play the next IR.

## Preset files

`export`/`import` keep single sounds as small JSON files, to keep or share
them. Unlike backups, they name the cab, the mics and the IRs instead of
using bank positions, which differ between units and firmware versions:
`import` looks the names up on this unit and refuses the file (sending
nothing) if the preset's mode needs one the unit doesn't have. Only the cab
and mics count in simulation and arcade mode, only the IRs in IR mode. This
isn't Torpedo Remote's `.top` format. The TUI's "Preset files" tab does the
same from a folder (`presets/` by default).

## Backup

`backup` selects each preset in turn to read it (the unit has no other way
to send a stored preset), then goes back to the current one. `restore`
compares the file with the unit and only stores the presets that differ, so
a restore that changes one preset writes one; a slot that is empty in the
backup gets cleared like `clear` does. Cab and IR data aren't included, and
presets point at cabs and IRs by bank position, so a backup is meant for the
unit it came from. Both refuse to run while the current preset has unsaved edits.

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
