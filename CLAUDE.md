# torpedo

Goal: control a Two notes **Torpedo Live** (owner's own unit) from Linux, the
way `../klim` replaced the KLIM Dash TKL keyboard app: a Python CLI plus a
Textual TUI over hidapi. Torpedo Remote 5 (Windows/macOS only) is the
reference implementation.

## Status (2026-10-15)

- Protocol mostly decoded for reading and setting parameters: see `PROTOCOL.md`.
- Read-only access from Python is verified (`tools/readtest.py`: info + all 51 params).
- Write + discard verified (`tools/writetest.py --write [--poll]`): `01 20 16`,
  then `0c 03 0` restores the preset. Clients must poll with `7e` until `05`
  (see PROTOCOL.md "Polling").
- Package `src/torpedo/` (2026-10-15): `protocol.py` (tables, ranges,
  arcade/simulation rules), `device.py` (transport + cache), `fake.py` (report-level emulator,
  raises on unobserved commands), `cli.py` (info/params/get/set/preset/discard/setup).
  Verified on the unit.
- TUI (2026-10-16, `torpedo tui`, `src/torpedo/tui/`, merged): preset list +
  editor in Remote's rack order, overlay pickers, arcade shows the computed
  simulation values dimmed; IRs/Setup/Backup tabs. The editor is driven by
  `src/torpedo/layout.py` (no Textual, meant for the MPC plugin too). Used on
  the unit; CHARACTER (triode/pentode) and SIDE (back/front) labels checked there.
- Ranges come from a min/max sweep capture (`captures/2026-10-15-ranges.log`,
  `tools/ranges.py`). Cab selection from bank 0 works (`cabs`/`cab`, capture
  `2026-10-15-cabs.log`); IR mode (PRESET_MODE=2) slots load from the ML bank
  (`ir`, capture `2026-10-15-irs.log`); user IR banks 2–4 are empty here and
  Remote's Live UI never shows them (other products?), so FOLDER_IR stays pinned to 0. The ranges capture also
  showed setup writes (`0c 00` level, `0c 01` mute) and a cab preview upload
  (`6e`/`6f`/`70`), not implemented yet.
- Arcade mode only accepts writes to `ARCADE_EDITABLE` (protocol.py, probed
  with `tools/modeprobe.py`); the client refuses the rest up front.
- One-off scripts in `tools/` run with `uv run python tools/<x>.py`.
- Preset names, save and rename are decoded (`presets`/`save`/`rename`,
  capture `2026-10-15-presets.log`); `save` (`04 73`) writes flash and refuses
  non-empty slots without `--overwrite`. A076 is the user's scratch slot ("TESTCAP", emptied
  by the 2026-10-16 delete capture).
- Global settings (level, mute, MIDI CC/PC/channel, S/PDIF output and rate)
  are decoded (`setup NAME=VALUE`, capture `2026-10-15-setup.log`).
- `clear` empties a preset like Remote's Delete (`04 69 n` + `04 0e`, capture
  `2026-10-16-delete.log`): renamed "USER" with the default sound
  (`EMPTY_PARAMS`). TUI: Del on the highlighted preset. Tested on the unit.
- `backup`/`restore` (src/torpedo/backup.py): JSON of all 128 presets +
  global settings, read by selecting each preset; restore stores only the
  presets that differ, and clears the slots that are empty in the backup.
  Backups go in `backup/` and are committed (like klim).
- Preset files (2026-10-16, `export`/`import`, src/torpedo/presetfile.py, TUI
  "Preset files" tab): our own JSON, one sound per file, cab/mics/IRs by name
  (backups keep bank positions: they are one unit's state). Import refuses a
  file whose mode needs a cab/mic/IR the unit lacks. Remote's `.top` format
  (XML, `TOP_*` tags) was looked at and deliberately not supported: Remote
  pulls missing cabs from the Two notes store, which we don't touch.
- IR download/upload/delete on bank 1 (`ir-get`/`ir-put`/`ir-delete`,
  src/torpedo/ir.py, capture `2026-10-15-ir-upload.log`), tested on the unit
  (bank 1 = the 86 ML IRs again). Resampling and min phase (2026-10-16, pure
  Python) are checked against Remote's uploads in `captures/irtest/`.
- Footswitch settings (setup 06–09) aren't exposed by Remote.
- Front-panel changes arrive unsolicited (capture `2026-10-16-frontpanel.log`,
  `tools/listen.py`): `Torpedo.poll()` picks them up, `FakeHid.panel_*` emulates
  them. The OUT LEVEL encoder sends SETUP_LEVEL (0 also mutes); IN LEVEL sends
  nothing.
- Meters (2026-10-16-meters, -meters2 captures): `05 in out clip 00` in every
  empty-queue reply, linear 0..255, clip bit 0 in / bit 1 out
  (`Torpedo.meters`, `FakeHid.panel_signal`, TUI meter bar under the editor,
  polled every 0.1 s). LED thresholds -30..-6 dBFS are inferred.
- After a USB replug or power cycle the unit sends nothing, so re-read
  everything (capture `2026-10-16-unplug.log`). `discard` doesn't clear the
  unit's kept edit ("A001*" on its panel, survives reboots); see PROTOCOL.md.

## Safety

- Never send a command type that has not been observed in a capture (firmware
  update, factory reset, secure erase and clear-bank commands exist and their
  IDs are unknown).
- Never save over the user's presets without asking; edits are fine as long as
  the preset is reselected afterwards.

## Capturing Torpedo Remote's traffic (Windows)

Frida hooks `ReadFile`/`WriteFile`/`GetOverlappedResult` in the running
`TorpedoRemote.exe`; it only observes and does not send anything.

```sh
uv venv fridaenv && VIRTUAL_ENV=fridaenv uv pip install frida
fridaenv/Scripts/python tools/hidspy.py captures/<name>.log   # stop: create tools/STOP
python tools/summarize.py captures/<name>.log 24              # collapse dumps, drop report ID
```

The `.log` skips the idle `7e`/`05`/`04 02` traffic; `<name>.log.raw` keeps it.
Ask the user to perform one action per ~5 s so they separate in the log.

To see what the unit sends on its own (front panel, meters in `05`), close
Remote and run `uv run python tools/listen.py captures/<name>.log`: it only
polls with `7e`, logs in the same format, and takes typed notes as labels.

## Conventions (same as klim)

- Python project managed with `uv`; deps `hidapi`, `textual`.
- Commit only when the user asks. Git identity: `yann-ygn <yann@ygn-effects.com>`.
- Validate every value before sending: the firmware may accept out-of-range values.
- Windows + Git Bash: don't write Python with `\n` escapes through heredocs (they
  get mangled); use the editor tools. Open files with `encoding="utf-8"`.
