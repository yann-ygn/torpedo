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
QUERY_BANK = 0x08  # 04 08 bank: cab records (MSG_CAB), then 04 10 bank <name>
BANK_END = 0x10
HEARTBEAT = bytes([MSG_QUERY, 0x02])

# Cab bank listing: one record = up to 4 `06 bank idx part ...` reports.
#   part 0: 01 08 d8 <name> from byte 7
#   part 1: 24 bytes (unknown), then mic names 0..2   (12-byte slots from byte 28)
#   part 2: mic names 3..7                            (12-byte slots from byte 4)
#   part 3: <maker> in bytes 4..35, then unknown
# Bank 0 holds the cabs SIMUHP_SPEAKER selects; SIMUHP_MIC indexes the cab's mic list.
MSG_CAB = 0x06
MIC_SLOT = 12

SETUP_PRESET = 0x03  # current preset, or EDIT_BUFFER once it has unsaved edits
SETUP_PRESET_SOURCE = 0x04  # preset the edit buffer came from
EDIT_BUFFER = 0x80
PRESETS = 128  # A001..A128

# Preset names: `04 09 81` -> 129 x `03 idx arcade name`: presets 0..127, then
# the edit buffer (0x80); `04 09 80` -> the edit buffer only. `arcade` is 1 when
# the preset's PRESET_MODE is arcade (stored with it; Remote's arcade/simulation
# label). Empty user slots are named "USER".
MSG_NAME = 0x03  # host: 03 80 <name> renames the edit buffer (echoed)
QUERY_NAMES = 0x09
NAMES_ALL, NAMES_EDIT_BUFFER = 0x81, 0x80
# 04 73 n: store the edit buffer into preset n (a flash write). The device
# answers ~2 s later: 03 n <name>, 0c 03 n, 0c 04 n, 0c 05 00.
QUERY_STORE = 0x73
EMPTY_PRESET = "USER"
NAME_MAX = 25  # longest factory name; the real limit is unknown
NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ")  # seen in factory names


def check_name(name: str) -> str:
    """Uppercase and validate a preset name (only characters seen in factory names)."""
    name = name.upper()
    if not 1 <= len(name) <= NAME_MAX or not set(name) <= NAME_CHARS:
        raise ValueError(f"preset name must be 1..{NAME_MAX} characters among A-Z, 0-9 and space")
    return name


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

# Order = LiveCABParameterIndexDictionary in the exe. Ranges come from a
# min/max sweep of every control in Torpedo Remote
# (captures/2026-10-15-ranges.log, check with tools/ranges.py); the device
# echoed every extreme unchanged. Not swept: ARCADE_GUITARBASS, PRESET_MODE
# (values seen on the unit) and SIMUHP_MIC_2 (assumed same list as mic A).
PARAMS = [Param(i, *row) for i, row in enumerate([
    ("SIMUAMP", 0, 1),
    ("SIMUAMP_MODELE", 0, 7),
    ("SIMUAMP_GAIN", 0, 30),
    ("SIMUAMP_CONTOUR", *KNOB),  # presence
    ("SIMUAMP_DEPTH", *KNOB),
    ("SIMUAMP_CHARACTER", 0, 1),
    ("SIMUHP", 0, 1),
    ("SIMUHP_PREVIEW", *RO),  # 2 = play a cab uploaded with 6e/6f/70 (Remote library preview)
    ("SIMUHP_SPEAKER", 0, 31),  # index into cab bank 0 (32 cabs on this unit)
    ("SIMUHP_WAVE0", 0, 85),  # IR mode, slot 1: index in the folder's bank (ML bank: 86 IRs)
    ("SIMUHP_WAVE1", 0, 85),  # IR mode, slot 2
    ("SIMUHP_FOLDER_IR_0", 0, 0),  # 0 = bank 1 (ML); user banks 2-4 are empty on this unit
    ("SIMUHP_FOLDER_IR_1", 0, 0),
    ("SIMUHP_MIC", 0, 7),  # index into the cab's mic list
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
    ("PRESET_LEVEL", 0, 107),  # dB mapping unknown
    ("REVERB", 0, 1),
    ("REVERB_ROOM", 0, 7),
    ("REVERB_L_DRYWET", *KNOB),
    ("ARCADE_GUITARBASS", 0, 1),
    ("ARCADE_POWERAMP", 0, 30),  # a knob, not a selector
    ("ARCADE_DISTANCE", 0, 44),
    ("ARCADE_TONE", 0, 44),
    ("ARCADE_CONTOUR", 0, 40),  # UI % = value * 2.5
    ("ARCADE_LEVEL", 0, 107),
    ("PRESET_MODE", 0, 2),  # SIMULATION / ARCADE / IR below
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
    ("EQ_L_FREQ_LOW", *KNOB),  # low cut
    ("EQ_L_FREQ_F0", *KNOB),
    ("EQ_L_FREQ_F1", *KNOB),
    ("EQ_L_FREQ_F2", *KNOB),
    ("EQ_L_FREQ_F3", *KNOB),
    ("EQ_L_FREQ_F4", *KNOB),
])]
PARAM_BY_NAME = {p.name: p for p in PARAMS}
PRESET_MODE = PARAM_BY_NAME["PRESET_MODE"].index
SIMULATION, ARCADE, IR = 0, 1, 2  # PRESET_MODE values; IR = two IR slots instead of a cab
MODE_NAMES = {SIMULATION: "simulation", ARCADE: "arcade", IR: "ir"}
# IR slot n = SIMUHP_FOLDER_IR_n (folder) + SIMUHP_WAVEn (index in that folder's
# bank). Folder 0 = bank 1 (ML IRs); folder f = bank f + 1 is a guess.
FOLDER_BANK_OFFSET = 1

# Arcade mode is a front end over the same parameters: its 6 ARCADE_* macros
# recompute simulation parameters (tools/modeprobe.py, 2026-10-15):
#   GUITARBASS -> EQ_MODE, SIMUAMP_MODELE      DISTANCE -> SIMUHP_DISTANCE, REVERB_L_DRYWET
#   POWERAMP -> SIMUAMP, SIMUAMP_GAIN, PRESET_LEVEL   TONE -> SIMUHP_CENTER
#   CONTOUR -> EQ_L_GAIN0, EQ_L_GAIN4          LEVEL -> PRESET_LEVEL
# In arcade mode the device silently ignores writes to anything else except
# the parameters below. Simulation mode accepts everything (ARCADE_* are then
# just stored, nothing is recomputed).
ARCADE_EDITABLE = frozenset(PARAM_BY_NAME[n].index for n in """
    ARCADE_GUITARBASS ARCADE_POWERAMP ARCADE_DISTANCE ARCADE_TONE ARCADE_CONTOUR ARCADE_LEVEL
    SIMUHP_SPEAKER SIMUHP_MIC REVERB_ROOM EQ_L_FREQ_F0 EQ_L_FREQ_F1 EQ_L_FREQ_F2 EQ_L_FREQ_F3 EQ_L_FREQ_F4
    PRESET_MODE""".split())

# Order = LiveCABSetupIndexDictionary. Read-only: only preset selection
# (0c 03 n) has been observed as a write.
SETUP = """SETUP_LEVEL SETUP_MUTE SETUP_ON_OFF SETUP_PRESET SETUP_PRESET_SOURCE
SETUP_FLAG_PRESET_MODIF SETUP_FOOTSWITCH_A SETUP_FOOTSWITCH_HOLD_A SETUP_FOOTSWITCH_B
SETUP_FOOTSWITCH_HOLD_B SETUP_SPDIF_FREQ SETUP_MIDI_CC SETUP_MIDI_PC SETUP_MIDI_CHANNEL
SETUP_SPDIF_MODE DUMMY_CCIR_PRESENCE DUMMY_AUX_LEVEL DUMMY_AUX_MUTE""".split()


@dataclass
class Cab:
    index: int
    name: str
    maker: str
    mics: list[str]  # SIMUHP_MIC indexes this list; "" = empty slot


def _str(b: bytes) -> str:
    return b.split(bytes(1))[0].decode("latin-1")


def parse_cab(index: int, parts: dict[int, bytes]) -> Cab:
    """Build a Cab from its `06 bank idx part` reports (ML/user IRs only have parts 0-1)."""
    p0, p1, p2, p3 = (parts.get(i, bytes(REPORT_LEN)) for i in range(4))
    slots = p1[28:28 + 3 * MIC_SLOT] + p2[4:4 + 5 * MIC_SLOT]
    mics = [_str(slots[i:i + MIC_SLOT]) for i in range(0, len(slots), MIC_SLOT)]
    return Cab(index, _str(p0[7:]), _str(p3[4:36]), mics)


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
    """Preset index -> label as Torpedo Remote shows it (0 = A001)."""
    return f"A{n + 1:03d}"


def parse_name(p: bytes) -> str:
    """Name in a `03 idx flag name` report."""
    return _str(p[3:])


@dataclass
class PresetName:
    index: int  # 0..127, or EDIT_BUFFER
    name: str
    arcade: bool

    @property
    def empty(self) -> bool:
        return self.name == EMPTY_PRESET
