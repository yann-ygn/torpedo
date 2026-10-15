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
  Verified on the unit. No TUI yet.
- Ranges come from a min/max sweep capture (`captures/2026-10-15-ranges.log`,
  `tools/ranges.py`). Cab/IR selection params are read-only. That capture also
  showed setup writes (`0c 00` level, `0c 01` mute) and a cab preview upload
  (`6e`/`6f`/`70`), not implemented yet.
- Arcade mode only accepts writes to `ARCADE_EDITABLE` (protocol.py, probed
  with `tools/modeprobe.py`); the client refuses the rest up front.
- One-off scripts in `tools/` run with `uv run python tools/<x>.py`.
- Still to capture: preset names, save/rename preset, user IR upload,
  setup changes (footswitch/MIDI/S/PDIF).

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

## Conventions (same as klim)

- Python project managed with `uv`; deps `hidapi`, `textual`.
- Commit only when the user asks. Git identity: `yann-ygn <yann@ygn-effects.com>`.
- Validate every value before sending: the firmware may accept out-of-range values.
- Windows + Git Bash: don't write Python with `\n` escapes through heredocs (they
  get mangled); use the editor tools. Open files with `encoding="utf-8"`.
