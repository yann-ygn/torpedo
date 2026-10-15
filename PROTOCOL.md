# Torpedo Live USB protocol (notes)

Recovered on 2026-10-15 from Torpedo Remote 5.4.32 (Windows) by hooking its HID
I/O with Frida (`tools/hidspy.*`) and from the RTTI/strings in `TorpedoRemote.exe`
(`captures/torpedoremote-5.4.32-strings.txt`). Device: Torpedo Live, serial
`032C4B94`. Anything marked *(?)* is a guess.

## Transport

- USB HID, VID:PID `0483:A334` (STMicroelectronics VID), manufacturer
  "Two Notes Audio Engineering", product "Torpedo Live", usage page `0xFF00`.
  (`0483:5710` also appears in the exe's driver-repair code: probably another
  model or bootloader.)
- Report descriptor: no report IDs, one 64-byte input and one 64-byte output
  report. With hidapi: `write([0] + 64 bytes)`, `read(64)`.
- Firmware v3 used a libusb0 driver; v4+ is plain HID, so it works through
  hidraw on Linux (needs a udev rule; match on the manufacturer string too,
  since `0483` is ST's shared VID).
- Torpedo Remote embeds hidapi 0.10.1 and is a JUCE app.

## Framing

Every message is one 64-byte report: `[type] [index] [value] ...`, zero-padded
on the host side. The device reuses its buffer, so bytes past the meaningful
payload are stale garbage: parse by type, never by "trailing zeros".

## Polling (`7e`) and the reply queue

Verified 2026-10-15 with our own client. The device keeps an outgoing queue
(query replies, write echoes, notifications) and sends **one IN report per
OUT report**: the next queued message, or `05 …` when the queue is empty.
On its own, it only flushes the whole queue about once per second, right
before the unsolicited `04 02` heartbeat.

| Host → | ← Device | |
|---|---|---|
| `7e` | next queued message, or `05 00 00 …` if none | poll. Torpedo Remote sends it continuously (~47/s). `05` probably carries the meters (`LiveCABInfoIndexDictionary`: DSP volume in/out, binary values, DSP error, input/output clip, silence) *(?)* |
| — | `04 02` | about once per second, unsolicited, after the queue flush: heartbeat *(?)* |

So a client should send a command, then send `7e` until it gets `05` back:
`04 0a` + polling returns all 51 params in ~0.1 s, versus ~1 s without
polling. Without polling, the reply to a write may also be a bare `05`
(the echo isn't queued yet) and the echo then arrives with the next flush.

## Queries (`04 <what> [arg]`)

| Sent | Reply | Content |
|---|---|---|
| `04 01` | `0b …` | info: `0b 02 02 01 04 17 03 "032C4B94"` → bytes 1..6 unknown (firmware 4.23?), then the serial as ASCII |
| `04 0a` | 51 × `01 idx val` | every parameter of the current preset (see table) |
| `04 0b` | 18 × `0c idx val` | setup values (see table) |
| `04 08 <bank>` | N × `06 bank idx part …`, then `04 10 bank …` | cabinet bank contents (layout below). Bank 0 = 32 cabs × 4 parts (Two notes + partners: Black Bear, Mirador, Suhr/Celestion…), bank 1 = 86 IRs × 2 parts ("ML-FRMN-FREEDOM"…), banks 2–4 = user IR banks, no records on this unit (only the `04 10` end marker) |
| `04 09 81` | 129 × `03 idx arcade name` | preset names: presets 0–127 (A001–A128), then the edit buffer (idx 0x80). `arcade` = 1 when the preset is in arcade mode: it follows PRESET_MODE (the edit buffer's flips as soon as the mode changes) and is stored with the preset, which is how Remote marks presets as arcade or simulation. Empty user slots are named "USER" (A076–A109 and A116–A118 here). The factory simulation presets are A110–A115 ("RECORDING …") and A119–A128 ("… 2MICS"). `04 09 80` returns the edit buffer's entry only |
| `04 33`, `04 34` | `04 0f slot name` + 3 × `04 31 slot n` | names of the IRs loaded in mic slot 0 / 1. Slot 0 follows SIMUHP_SPEAKER (A001: "LuxTweeDe" = bank 0 #16); slot 1 said "Brit VintC" with mic B muted *(?)* |
| `04 15 <bank> <idx>` | `6e …` ×2, `6f …` ×~45, `70 …` | waveform of an IR, for the display (Remote sends `04 15 01 55` after loading ML IR 85 into a slot); `6e` = header with the IR name, `6f` = data chunks, `70` = end |

### Cab records (`06 bank idx part …`)

| part | content |
|---|---|
| 0 | `01 08 d8`, then the cab name from byte 7 |
| 1 | 24 unknown bytes, then mic names 0–2 (12-byte slots from byte 28, not always NUL-terminated) |
| 2 | mic names 3–7 (12-byte slots from byte 4) |
| 3 | maker name in bytes 4–35, then unknown (checksum?) |

One cab, two mics (A and B, each with its own mute): SIMUHP_MIC and
SIMUHP_MIC_2 are both indexes into the selected cab's 8-mic
list ("Knightfall", mic 0 of the Two notes cabs, is a real mic name, as in Remote's UI).

### Cab selection (2026-10-15-cabs capture)

SIMUHP_SPEAKER is an index into bank 0 (`01 08 n`); arcade mode accepts it
too. When selecting a cab, Torpedo Remote also writes SIMUHP_MIC so the same
mic name stays selected (DarkPrinz mic 3 "Dyn 421" → Brit 65C mic 4), and
writes nothing when the index is unchanged. When the new cab lacks the mic it
picked something else (LuxTweeDe mic 5 "Dyn 7b" → DarkPrinz mic 3), rule
unknown. Mic B was never rewritten (it was muted, and arcade mode ignores
SIMUHP_MIC_2 anyway); `torpedo cab` keeps its name too in simulation mode.

### IR mode (2026-10-15-irs capture)

PRESET_MODE = 2 replaces the cab + 2 mics with two IR slots. SIMUHP (`01 06`)
is the global cab-sim toggle. Loading an IR into slot n: Remote sends
SIMUHP_PREVIEW = 0, SIMUHP_FOLDER_IR_n = 0, then SIMUHP_WAVEn = index (`01 09
55` = ML IR 85 "ML-FRMN-V30-906-E", `01 0a 0f` = ML IR 15), then `04 15 01
idx` for the waveform. Folder 0 = bank 1 (ML); other folders are presumably
the user banks 2–4, empty here. Per-slot controls are the mic A/B ones:
SIMUHP_LEVEL/PHASE/MUTE for slot 1, `_2` for slot 2. The amp, EQ and reverb
use the same parameters as simulation mode.

## Writes

| Sent | Effect | Device reply |
|---|---|---|
| `01 idx val` | set parameter | linked parameters it recomputed first (e.g. ARCADE_CONTOUR also moves EQ_L_GAIN0/4 and sometimes PRESET_LEVEL), then the echo `01 idx val`, then after the first edit `0c 03 80` (SETUP_PRESET = 0x80: the edit buffer) and `0c 04 n` (SETUP_PRESET_SOURCE: the preset it came from) |
| `0c 03 n` | select preset n (0 = A001) | `01 idx val` for every parameter that differs, then `0c 03 n`; discards an unsaved edit (verified: contour 21 → 22 → reselect → 21, all 51 params identical) |
| `0c 00 v` | SETUP_LEVEL, global output level, 0..107 (0x6b) | echo `0c 00 v` |
| `0c 01 v` | SETUP_MUTE 0/1 | echo `0c 01 v` |
| `6e`, `6f` ×N, `70` | upload a cab into the preview slot (see below) | `04 70` per report, `04 0d` after `70` |

No reply at all to a no-op: writing a parameter's current value, or
reselecting the current unmodified preset. Also no reply, and no change, when
the parameter can't be edited in the preset's mode (next section).

### Arcade vs simulation mode

The modes are front ends over the same 51 parameters; PRESET_MODE (0x22)
picks one (0 = simulation, 1 = arcade, 2 = IR). Probed parameter by parameter on the
unit (`tools/modeprobe.py`, each write one step from A001, then reselect):

- Arcade mode accepts only the 6 `ARCADE_*` macros, SIMUHP_SPEAKER, SIMUHP_MIC,
  REVERB_ROOM, EQ_L_FREQ_F0..F4 (and PRESET_MODE). Every other write is
  silently dropped.
- Each macro recomputes simulation parameters, reported before its echo:

  | macro | recomputes |
  |---|---|
  | ARCADE_GUITARBASS | EQ_MODE, SIMUAMP_MODELE |
  | ARCADE_POWERAMP | SIMUAMP, SIMUAMP_GAIN, PRESET_LEVEL |
  | ARCADE_DISTANCE | SIMUHP_DISTANCE, REVERB_L_DRYWET |
  | ARCADE_TONE | SIMUHP_CENTER |
  | ARCADE_CONTOUR | EQ_L_GAIN0, EQ_L_GAIN4 |
  | ARCADE_LEVEL | PRESET_LEVEL |

- Simulation and IR modes accept every parameter; `ARCADE_*` writes are then only
  stored (nothing recomputed). Switching mode recomputes nothing either, so
  simulation mode starts from the values arcade mode produced.

### Cab preview upload (`6e` / `6f` / `70`)

Clicking a cab in Torpedo Remote's library (2026-10-15-ranges capture,
"Mars Asto 112 Cream Live" by "Noise Factory") sent 4 × `6e 00 05 …` (header:
`2f 98 07 00` *(?)*, cab name, mic list `44 79 6e …` = "Dyn 421", "Dyn
Bass20", …, maker name), 200 × `6f` (63 bytes of data each, apparently 24-bit
little-endian samples), then `70`. The device acks each report with `04 70`
and the end with `04 0d`. Remote then sent `01 07 02` (SIMUHP_PREVIEW = 2,
play the preview cab) and `01 07 00` to leave preview. Whether the slot is
RAM-only is not known: don't send these yet.

### Saving a preset (2026-10-15-presets capture)

Torpedo Remote has a single "save" (renaming = saving under a new name):

1. `03 80 <name>` (name from byte 2, NUL-padded): names the edit buffer; the
   device echoes `03 80 <arcade> <name>`.
2. `04 73 n`: stores the edit buffer into preset n. This writes flash: about
   2 s later the device sends `03 n <arcade> <name>`, `0c 03 n`, `0c 04 n`,
   `0c 05 00`.

Seen: "TESTCAP2" then "TESTCAP" stored into A076 (an empty "USER" slot).
Factory names use A–Z, 0–9 and spaces, up to 25 characters; the real limits
aren't known, so `torpedo` sticks to that.

SETUP_PRESET is n while preset n is loaded unmodified and 0x80 once it is
edited, with the source in SETUP_PRESET_SOURCE (editing A067 gave `0c 03 80`,
`0c 04 42`). It is not a bit 7 flag on n. SETUP_FLAG_PRESET_MODIF went to 0
after a store and 1 after the next edit, but also read 1 for an unedited
A001, so it isn't used.

Not captured yet: user IR upload (to a bank, as opposed to the preview slot),
footswitch/MIDI setup changes.

## Do not send

From the RTTI: `RebootableCommand<0x03>`, `<0x04>`, `<0x27>`, `UpdateFirmwareCommand`,
`SecureEraseCommand`, `ClearPresetsCommand`, `ClearBankCommand`, factory reset.
Their wire IDs are not mapped yet, so never send a command type that has not
been observed in a capture. Writes seen so far: `01`, `03 80`, `04 73` (flash), `0c 00`, `0c 01`, `0c 03`, `6e`, `6f`, `70`, `7e`.

Command IDs present in the exe (MSVC template args, decoded): `Command<>` 0x01,
0x03, 0x04, 0x08, 0x09, 0x0A, 0x0B, 0x0E, 0x0F, 0x16, 0x27, 0x31, 0x38, 0x3A,
0x3B, 0x3D, 0x3F, 0x41, 0x42, 0x45, 0x49, 0x4B, 0x4C, 0x65, 0x68, 0x69, 0x6C,
0x6D, 0x73, 0x74, 0x75. `QueryCommand<query, reply type>`: (0x01,0x0B) info,
(0x09,0x03) legacy names, (0x0A,0x01) params, (0x0B,0x0C) setup, (0x16,0x04),
(0x3A,0x0D), (0x3F,0x0E), (0x45,0x10), (0x4B,0x10), (0x73,0x03).
`ModeOutgoingMessage<>` 0x01, 0x03, 0x0C. `MoveItemCommand<>` 0x68, 0x6D.

## Parameters (`01 idx val`)

Order is the `LiveCABParameterIndexDictionary` string list in the exe; checked
against the capture for 0x0D, 0x1D, 0x20 and the 51-entry count. Values are
those of preset A001.

| idx | name | A001 | notes |
|---|---|---|---|
| 00 | SIMUAMP | 0 | amp sim on/off |
| 01 | SIMUAMP_MODELE | 5 | |
| 02 | SIMUAMP_GAIN | 0 | 0..30 |
| 03 | SIMUAMP_CONTOUR | 127 | presence in the .top files *(?)* |
| 04 | SIMUAMP_DEPTH | 127 | |
| 05 | SIMUAMP_CHARACTER | 1 | |
| 06 | SIMUHP | 1 | cab sim on/off |
| 07 | SIMUHP_PREVIEW | 0 | 2 = play the uploaded preview cab |
| 08 | SIMUHP_SPEAKER | 16 | cab = bank 0 index (16 = LuxTweeDe) |
| 09 | SIMUHP_WAVE0 | 0 | IR slot 1: index in the folder's bank |
| 0A | SIMUHP_WAVE1 | 0 | IR slot 2 |
| 0B | SIMUHP_FOLDER_IR_0 | 0 | IR slot 1 folder (0 = ML bank) |
| 0C | SIMUHP_FOLDER_IR_1 | 0 | IR slot 2 folder |
| 0D | SIMUHP_MIC | 5 | mic A: index into the cab's mic list (5 = "Dyn 7b" on LuxTweeDe) |
| 0E | SIMUHP_DISTANCE | 0 | |
| 0F | SIMUHP_CENTER | 17 | |
| 10 | SIMUHP_POSITION | 1 | front/back |
| 11 | EQ | 1 | |
| 12 | EQ_MODE | 0 | |
| 13–17 | EQ_L_GAIN0..4 | 21 20 20 20 21 | 20 = flat *(?)* |
| 18 | PRESET_LEVEL | 87 | 0..107 |
| 19 | REVERB | 1 | |
| 1A | REVERB_ROOM | 0 | |
| 1B | REVERB_L_DRYWET | 1 | |
| 1C | ARCADE_GUITARBASS | 0 | |
| 1D | ARCADE_POWERAMP | 0 | knob 0..30; 2 after "power amp on" (also set SIMUAMP=1, GAIN=2) |
| 1E | ARCADE_DISTANCE | 0 | |
| 1F | ARCADE_TONE | 38 | |
| 20 | ARCADE_CONTOUR | 21 | 0..40, UI % = val × 2.5 (21 = 52%, 22 = 55%) |
| 21 | ARCADE_LEVEL | 87 | |
| 22 | PRESET_MODE | 1 | 0 = simulation, 1 = arcade, 2 = IR |
| 23 | SIMUHP_LEVEL | 95 | |
| 24 | SIMUHP_PHASE | 0 | |
| 25 | SIMUHP_MUTE | 0 | |
| 26–2C | SIMUHP_MIC_2, DISTANCE_2, CENTER_2, POSITION_2, LEVEL_2, PHASE_2, MUTE_2 | 0 0 0 1 95 0 1 | mic B |
| 2D | EQ_L_FREQ_LOW | 0 | low cut, 0..127 |
| 2E–32 | EQ_L_FREQ_F0..F4 | 57 ×5 | |

Ranges come from sweeping every control to min/max in Torpedo Remote
(`captures/2026-10-15-ranges.log`, summarized by `tools/ranges.py`); the
device echoed every extreme unchanged. The full table is in
`src/torpedo/protocol.py`. The factory `.top` presets in
`C:\Program Files\Two notes Audio Engineering\Torpedo Remote v5\Presets\Live`
are plain XML with the same parameters (`TOP_*` names).

## Setup (`0c idx val`)

Order is `LiveCABSetupIndexDictionary`. Values are from the first read.

| idx | name | value |
|---|---|---|
| 00 | SETUP_LEVEL | 0x55 |
| 01 | SETUP_MUTE | 0 |
| 02 | SETUP_ON_OFF | 1 |
| 03 | SETUP_PRESET | 0x7E (current preset, 0x80 = edit buffer; select with `0c 03 n`) |
| 04 | SETUP_PRESET_SOURCE | 0x70 (preset the edit buffer came from) |
| 05 | SETUP_FLAG_PRESET_MODIF | 1 |
| 06–09 | SETUP_FOOTSWITCH_A, HOLD_A, B, HOLD_B | 1 3 2 7 |
| 0A | SETUP_SPDIF_FREQ | 1 |
| 0B–0D | SETUP_MIDI_CC, MIDI_PC, MIDI_CHANNEL | 1 1 0x10 |
| 0E | SETUP_SPDIF_MODE | 3 |
| 0F–11 | DUMMY_CCIR_PRESENCE, DUMMY_AUX_LEVEL, DUMMY_AUX_MUTE | 0xFF |
