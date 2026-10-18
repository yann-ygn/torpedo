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

- MPC plugin (2026-10-17, branch `mpc-probe`): the unit plugged into the MPC Key 37
  (Gen1, RK3288, armv7, MPC runs as root) shows up as `/dev/hidraw0`, and a shell
  round trip (`04 01` → `0b` info) works. `mpc/`: C core (`core/tp_hid.c` hidraw or
  `unix:` socket, `core/tp_dev.c` exchange/poll/cache like device.py), tables
  generated from protocol.py by `mpc/gen_tables.py` (flash-writing commands left
  out, except the preset store and clear the PRESET page needs), `probe/probe.c` (read-only
  check, `--listen`). `make -C mpc host` (ASan,
  used by tests/test_mpc.py against tests/fakebridge.py), `make -C mpc arm` (static
  musl via Zig from PyPI: no Docker here). The plugin builds on github.com/sd88me/mpc-vst-plugins
  (VST2 wrapper, int16 effect path: so a silent instrument used as a control surface, not
  a passthrough effect). `core/tp_core.c` (branch `mpc-core`): worker
  thread owning the device; ordered queue with one entry per parameter (a rewrite
  replaces it and moves to the end, but not past a queued store or clear); visible state = device + queued values, so
  late echoes don't move knobs; reports only device-made changes; reconnects and
  re-reads everything on unplug; reads the edit buffer's name (`04 09 80`) after every load, as the unit
  renames it silently. `ctl/ctl.c` drives it from stdin
  (tests/test_mpc_core.py, also usable on the MPC); `make -C mpc tsan` for threads.
- MPC plugin proper (branch `mpc-plugin`): `mpc/plugin/` on mpc-vst-plugins' VST2 wrapper
  (submodule `mpc/mpc-vst-plugins`, pinned; `git submodule update --init`). Scope: the
  unit's parameters and presets only (no IR upload, files, preset import/export) and no
  project recall (empty state chunk). `gen_plugin.py` derives params.json and value texts
  (plugin_tables.c) from layout.py and checks `layout.conf` against them; param 0 is a virtual
  read-only `view` (offline/simulation/arcade/ir) the skin's `when=view:...` lines hang on.
  `engine.c`: silent instrument, one tp_core shared by all instances, `display_rev` = core
  revision, presets in MPC's PRESET menu.
  **Skin (2026-10-18 rework):** `layout.conf` is hand-owned and edited in the skin studio
  (`tools/studio.py serve plugin/layout.conf --params plugin/params.json [--host <ip>]`); an MPC
  page is not the TUI's rack. Dracula palette. Each panel is one Q-Link group (every 4 keys of the
  tab's single `qlinks` line; `qlink_bounds=column`): MPC outlines the active group, touching a
  control picks it, the Q-Link button steps through them (confirmed on the Key 37).
  **Modes (2026-10-18):** a tab's Q-Links and outlines are fixed at build time (`when=` only hides
  controls, one condition per line), so a mode with other controls gets its own tab: PRESET |
  ARCADE | AMP | CAB | IR | EQ. AMP serves simulation and IR (lines repeated with
  `when=view:ir`), EQ too (through `eq_view`, below); in arcade AMP/CAB/EQ show the values arcade sets as read-only readouts (what it
  takes itself, cab, mic A, room, output, stays live); other tabs show a placeholder. The engine
  drops writes to what the preset's mode doesn't show (`modes` in plugin_tables, from layout.py's
  LAYOUTS), so a Q-Link left on a hidden control edits nothing. Switching mode recomputes nothing
  (PROTOCOL.md), and Remote only writes PRESET_MODE too (`01 22 0x` in the -irs and -presets
  captures, then `04 09 80` for the name's mode byte): so does the plugin.
  **EQ (2026-10-18):** Remote's custom EQ: low cut, then each band's gain over its frequency
  (raw 0..127: the Hz mapping is unknown). The engine's virtual `eq_view` (hidden offline and in
  arcade / fixed / custom) drives it, since a line takes one condition; guitar/bass show the fixed
  band frequencies as labels. Q-Links: two neighbouring bands per group (low cut + band 1, 2-3,
  4-5: a tab has 4 groups); outside custom only the gains turn, the engine drops the rest
  (`eq_custom` in plugin_tables). Choosers are readout + arrow buttons drawn from `images/*.svg`
  (the browser renderer has no sized plain buttons). The skin is drawn by the browser
  renderer (`"art": "html"`) on the system Chromium through `plugin/html_art_local` (no Docker).
  `plugin/skin_post.py` (vst.json `skin_post`) drops the builder's Mouse Down → Toggle Switch from
  trigger buttons: their Button component toggles by itself on release, so every tap fired twice
  (keys typed two characters, arrows stepped two; seen on the Key 37). List rows keep it: their
  name label covers the Button, so it is all a tap reaches (dropping it left the tiles dead).
  `make -C mpc plugin` builds the device folder (Zig, glibc 2.17 symbols; skin via
  `uv run --with pillow`), `make -C mpc plugin-host` the ASan stand-in host
  (tests/test_mpc_plugin.py). Previews: `studio.py preview` (see Makefile). The folder
  carries `plugin-meta.xml`, which upstream's `mpc-store.sh -t <Synths> sync` needs to
  register it. On the Key 37 the plugin lives in `/storage/Synths/` (a skin change needs an
  `acvs` restart: ask first).
  **PRESET tab (2026-10-18, first tab, every mode):** 16 tiles show a bank (tap loads; lit = the
  current preset), SAVE / SAVE AS / DISCARD. Loading over edits, discarding and saving are each a
  CONFIRM/CANCEL question (`engine.c` state machine; the `pui` param drives the buttons'
  `when=`), dropped after 20 s or when the unit changes under it. SAVE AS lights the empty
  ("USER") slots. A save is `tp_core_store` (`04 73 n`, after the queued writes, names re-read
  to check it). Checked on the Key 37. CLEAR PRESET (2026-10-18) is the TUI's Delete: pick a lit
  tile (not empty, not the one with edits), confirm; `tp_core_clear` (`04 69 n`, `04 0e`, names
  and the current preset's parameters re-read; shares the store's one-at-a-time slot), not sent
  if an edit queued before it hit the same preset. The two are the only flash writes the C side
  exports. Presets read like
  the unit's panel, mode letter then number (S021), here and in the CLI/TUI (`preset_name(n, mode)`,
  `preset_index` takes them with or without the letter): a slot's from the name list's mode byte
  (its PRESET_MODE, 2 = IR seen on the unit), the current sound's from its own PRESET_MODE. Mute toggles are red when on.
  **Names (2026-10-18):** MPC has no text entry a VST2 skin can call (only "menu overlay", empty
  for VST2), so the PRESET tab has its own keyboard (A–Z, 0–9, space, DEL, CLEAR: `k_*`
  triggers; `pkb` swaps it for the tiles). A save question has NAME... (OK goes back to the
  question), RENAME... (no unsaved edits) starts with it. `tp_core_store(c, n, name)` renames
  the edit buffer first (`03 80`, skipped when the name is unchanged: no reply) and only stores
  once that is echoed; names are protocol.py's NAME_CHARS / NAME_MAX (exported by gen_tables).
  Tested against the emulator (tests/test_mpc_core.py, test_mpc_plugin.py).

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
