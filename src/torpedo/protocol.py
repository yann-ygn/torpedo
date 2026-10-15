"""Message types and parameter tables for the Torpedo Live (see PROTOCOL.md).

Every message is one 64-byte report `[type] [index] [value] ...`. The device
reuses its buffer, so bytes past the payload are stale: parse by type.
"""

from dataclasses import dataclass

REPORT_LEN = 64

# Message types. Only send what has been observed in a capture of Torpedo Remote.
MSG_PARAM = 0x01  # 01 idx val: set (host) / value or change (device)
MSG_QUERY = 0x04  # 04 what [arg]: query (host); 04 02 = heartbeat (device)
MSG_EMPTY = 0x05  # device: queue is empty (probably carries the meters)
MSG_INFO = 0x0B  # device: reply to 04 01
MSG_SETUP = 0x0C  # 0c idx val: select preset with idx 3 (host) / setup value (device)
MSG_POLL = 0x7E  # host: send me the next queued message

QUERY_INFO = 0x01
QUERY_PARAMS = 0x0A
QUERY_SETUP = 0x0B
HEARTBEAT = bytes([MSG_QUERY, 0x02])

SETUP_PRESET = 0x03
PRESET_MODIFIED = 0x80  # bit 7 of SETUP_PRESET: current preset has unsaved edits
PRESETS = 128  # (?) 7-bit index, bit 7 is the modified flag


@dataclass(frozen=True)
class Param:
    index: int
    name: str
    lo: int = 0
    hi: int = 1
    writable: bool = True  # False until we know what the value means

    def check(self, value: int):
        if not self.writable:
            raise ValueError(f"{self.name} is read-only for now")
        if not self.lo <= value <= self.hi:
            raise ValueError(f"{self.name} must be {self.lo}..{self.hi}, got {value}")


RO = (0, 127, False)
KNOB = (0, 127)

# Order = LiveCABParameterIndexDictionary in the exe. Ranges are provisional:
# only ARCADE_CONTOUR (0..40) was checked against the UI. The others are the
# widest of the factory .top presets (Live first, other products when the
# meaning is the same) and the values seen on the unit; widen them after a
# min/max sweep capture of each knob in Torpedo Remote.
PARAMS = [Param(i, *row) for i, row in enumerate([
    ("SIMUAMP", 0, 1),
    ("SIMUAMP_MODELE", 0, 7),
    ("SIMUAMP_GAIN", 0, 15),  # Live presets stop at 15, other products go to 127
    ("SIMUAMP_CONTOUR", *KNOB),  # presence
    ("SIMUAMP_DEPTH", *KNOB),
    ("SIMUAMP_CHARACTER", 0, 1),
    ("SIMUHP", 0, 1),
    ("SIMUHP_PREVIEW", *RO),
    ("SIMUHP_SPEAKER", *RO),  # cab selection: index space not mapped yet
    ("SIMUHP_WAVE0", *RO),
    ("SIMUHP_WAVE1", *RO),
    ("SIMUHP_FOLDER_IR_0", *RO),
    ("SIMUHP_FOLDER_IR_1", *RO),
    ("SIMUHP_MIC", 0, 7),
    ("SIMUHP_DISTANCE", *KNOB),
    ("SIMUHP_CENTER", *KNOB),
    ("SIMUHP_POSITION", 0, 1),  # front/back
    ("EQ", 0, 1),
    ("EQ_MODE", 0, 2),
    ("EQ_L_GAIN0", 0, 40),  # 20 = flat
    ("EQ_L_GAIN1", 0, 40),
    ("EQ_L_GAIN2", 0, 40),
    ("EQ_L_GAIN3", 0, 40),
    ("EQ_L_GAIN4", 0, 40),
    ("PRESET_LEVEL", 0, 95),  # levels: capped at the highest value seen, dB mapping unknown
    ("REVERB", 0, 1),
    ("REVERB_ROOM", 0, 7),
    ("REVERB_L_DRYWET", *KNOB),
    ("ARCADE_GUITARBASS", 0, 1),
    ("ARCADE_POWERAMP", 0, 2),  # 2 = "power amp on" in the capture
    ("ARCADE_DISTANCE", 0, 44),
    ("ARCADE_TONE", 0, 44),
    ("ARCADE_CONTOUR", 0, 40),  # UI % = value * 2.5
    ("ARCADE_LEVEL", 0, 97),
    ("PRESET_MODE", 0, 2),
    ("SIMUHP_LEVEL", 0, 107),
    ("SIMUHP_PHASE", 0, 1),
    ("SIMUHP_MUTE", 0, 1),
    ("SIMUHP_MIC_2", 0, 7),
    ("SIMUHP_DISTANCE_2", *KNOB),
    ("SIMUHP_CENTER_2", *KNOB),
    ("SIMUHP_POSITION_2", 0, 1),
    ("SIMUHP_LEVEL_2", 0, 107),
    ("SIMUHP_PHASE_2", 0, 1),
    ("SIMUHP_MUTE_2", 0, 1),
    ("EQ_L_FREQ_LOW", 0, 117),  # low cut
    ("EQ_L_FREQ_F0", *KNOB),
    ("EQ_L_FREQ_F1", *KNOB),
    ("EQ_L_FREQ_F2", *KNOB),
    ("EQ_L_FREQ_F3", *KNOB),
    ("EQ_L_FREQ_F4", *KNOB),
])]
PARAM_BY_NAME = {p.name: p for p in PARAMS}

# Order = LiveCABSetupIndexDictionary. Read-only: only preset selection
# (0c 03 n) has been observed as a write.
SETUP = """SETUP_LEVEL SETUP_MUTE SETUP_ON_OFF SETUP_PRESET SETUP_PRESET_SOURCE
SETUP_FLAG_PRESET_MODIF SETUP_FOOTSWITCH_A SETUP_FOOTSWITCH_HOLD_A SETUP_FOOTSWITCH_B
SETUP_FOOTSWITCH_HOLD_B SETUP_SPDIF_FREQ SETUP_MIDI_CC SETUP_MIDI_PC SETUP_MIDI_CHANNEL
SETUP_SPDIF_MODE DUMMY_CCIR_PRESENCE DUMMY_AUX_LEVEL DUMMY_AUX_MUTE""".split()


def find_param(key: str) -> Param:
    """Look a parameter up by name (case-insensitive, `-` for `_` ok) or index (`32`, `0x20`)."""
    name = key.upper().replace("-", "_")
    if name in PARAM_BY_NAME:
        return PARAM_BY_NAME[name]
    try:
        return PARAMS[int(key, 0)]
    except (ValueError, IndexError):
        raise KeyError(f"unknown parameter '{key}'") from None


def preset_name(n: int) -> str:
    """Preset index -> label as Torpedo Remote shows it (0 = A001). (?) past A001."""
    return f"A{(n & 0x7F) + 1:03d}"
