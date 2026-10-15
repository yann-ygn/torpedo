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
| `04 08 <bank>` | N × `06 bank idx part …`, then `04 10 bank …` | cabinet bank contents: 4 parts per cabinet (name, data, mic names, maker). Bank 0 = 128 Two notes cabs, bank 1 = 172 entries ("ML-FRMN-FREEDOM"…), banks 2–4 empty user IR slots ("Generic wave file") |
| `04 09 81` | 129 × `03 idx flag name` | legacy speaker names (v3 era: "BDELUXE 1 RBN", "USER", "RECORDING …", "… 2MICS"); count = arg & 0x7F + 1 *(?)*. `04 09 80` returned a single entry |
| `04 33`, `04 34` | `04 0f slot name` + 3 × `04 31 slot n` | names of the IRs loaded in mic slot 0 / 1 ("Lone Star 23 - WTE", "Brit VintC") |
| `04 15 01` | `6e …` ×2, `6f …` ×~45, `70 …` | waveform of the loaded IR (for the display); `6f` = data chunks, `70` = end |

## Writes

| Sent | Effect | Device reply |
|---|---|---|
| `01 idx val` | set parameter | linked parameters it recomputed first (e.g. ARCADE_CONTOUR also moves EQ_L_GAIN0/4 and sometimes PRESET_LEVEL), then the echo `01 idx val`, then `0c 03 (0x80 \| n)` after the first edit: bit 7 of SETUP_PRESET = current preset modified (`0c 04 00` also appeared in the Remote capture) |
| `0c 03 n` | select preset n (0 = A001) | `01 idx val` for every parameter that differs, then `0c 03 n`; discards an unsaved edit (verified: contour 21 → 22 → reselect → 21, all 51 params identical) |

Not captured yet: preset names, save, rename, store-preset, user IR upload,
footswitch/MIDI setup changes.

## Do not send

From the RTTI: `RebootableCommand<0x03>`, `<0x04>`, `<0x27>`, `UpdateFirmwareCommand`,
`SecureEraseCommand`, `ClearPresetsCommand`, `ClearBankCommand`, factory reset.
Their wire IDs are not mapped yet, so never send a command type that has not
been observed in a capture. Writes seen so far: `01`, `0c 03`, `7e`.

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
| 02 | SIMUAMP_GAIN | 0 | |
| 03 | SIMUAMP_CONTOUR | 127 | presence in the .top files *(?)* |
| 04 | SIMUAMP_DEPTH | 127 | |
| 05 | SIMUAMP_CHARACTER | 1 | |
| 06 | SIMUHP | 1 | cab sim on/off |
| 07 | SIMUHP_PREVIEW | 0 | |
| 08 | SIMUHP_SPEAKER | 16 | |
| 09 | SIMUHP_WAVE0 | 0 | |
| 0A | SIMUHP_WAVE1 | 0 | |
| 0B | SIMUHP_FOLDER_IR_0 | 0 | |
| 0C | SIMUHP_FOLDER_IR_1 | 0 | |
| 0D | SIMUHP_MIC | 5 | mic A type; 7 = "Dyn 112" in the capture |
| 0E | SIMUHP_DISTANCE | 0 | |
| 0F | SIMUHP_CENTER | 17 | |
| 10 | SIMUHP_POSITION | 1 | front/back |
| 11 | EQ | 1 | |
| 12 | EQ_MODE | 0 | |
| 13–17 | EQ_L_GAIN0..4 | 21 20 20 20 21 | 20 = flat *(?)* |
| 18 | PRESET_LEVEL | 87 | |
| 19 | REVERB | 1 | |
| 1A | REVERB_ROOM | 0 | |
| 1B | REVERB_L_DRYWET | 1 | |
| 1C | ARCADE_GUITARBASS | 0 | |
| 1D | ARCADE_POWERAMP | 0 | 2 after "power amp on" (also set SIMUAMP=1, GAIN=2) |
| 1E | ARCADE_DISTANCE | 0 | |
| 1F | ARCADE_TONE | 38 | |
| 20 | ARCADE_CONTOUR | 21 | 0..40, UI % = val × 2.5 (21 = 52%, 22 = 55%) |
| 21 | ARCADE_LEVEL | 87 | |
| 22 | PRESET_MODE | 1 | arcade / simulation *(?)* |
| 23 | SIMUHP_LEVEL | 95 | |
| 24 | SIMUHP_PHASE | 0 | |
| 25 | SIMUHP_MUTE | 0 | |
| 26–2C | SIMUHP_MIC_2, DISTANCE_2, CENTER_2, POSITION_2, LEVEL_2, PHASE_2, MUTE_2 | 0 0 0 1 95 0 1 | mic B |
| 2D | EQ_L_FREQ_LOW | 0 | low cut |
| 2E–32 | EQ_L_FREQ_F0..F4 | 57 ×5 | |

Ranges are unknown except contour; the factory `.top` presets in
`C:\Program Files\Two notes Audio Engineering\Torpedo Remote v5\Presets\Live`
are plain XML with the same parameters (`TOP_*` names) and help bound them.

## Setup (`0c idx val`)

Order is `LiveCABSetupIndexDictionary`. Values are from the first read.

| idx | name | value |
|---|---|---|
| 00 | SETUP_LEVEL | 0x55 |
| 01 | SETUP_MUTE | 0 |
| 02 | SETUP_ON_OFF | 1 |
| 03 | SETUP_PRESET | 0x7E (current preset; select with `0c 03 n`) |
| 04 | SETUP_PRESET_SOURCE | 0x70 |
| 05 | SETUP_FLAG_PRESET_MODIF | 1 |
| 06–09 | SETUP_FOOTSWITCH_A, HOLD_A, B, HOLD_B | 1 3 2 7 |
| 0A | SETUP_SPDIF_FREQ | 1 |
| 0B–0D | SETUP_MIDI_CC, MIDI_PC, MIDI_CHANNEL | 1 1 0x10 |
| 0E | SETUP_SPDIF_MODE | 3 |
| 0F–11 | DUMMY_CCIR_PRESENCE, DUMMY_AUX_LEVEL, DUMMY_AUX_MUTE | 0xFF |
