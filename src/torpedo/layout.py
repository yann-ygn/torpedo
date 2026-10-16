"""What an editor shows for each preset mode, in Torpedo Remote's rack order.

Plain data with no UI toolkit, so other front ends (the MPC plugin) can use
it too: a mode is a list of sections, a section a list of rows, a row a label
and one control per column (two columns for the mic A/B and IR A/B tables).

Value labels marked (?) are guesses from Remote's screenshots, not checked
against a capture.
"""

from collections.abc import Callable
from dataclasses import dataclass

from .protocol import ARCADE, IR, PARAM_BY_NAME, SIMULATION

Values = dict[int, int]  # parameter index -> value

SLIDER, CHOICE, CAB, MIC, IR_SLOT = "slider", "choice", "cab", "mic", "ir"

# Labels in wire-value order, as Torpedo Remote lists them. A001 has
# SIMUAMP_MODELE=5 / REVERB_ROOM=0 and Remote shows "PP EL34" / "Studio A".
AMPS = ("SE 6L6", "SE EL34", "SE EL84", "SE KT88", "PP 6L6", "PP EL34", "PP EL84", "PP KT88")
ROOMS = ("Studio A", "Studio B", "Basement", "Loft", "Hall A", "Hall B", "Crypt", "Cathedral")
EQ_MODES = ("guitar", "bass", "custom")  # 0 = guitar on A001; ARCADE_GUITARBASS -> 0/1 (?)
EQ_CUSTOM = 2
EQ_BANDS = {0: ("120 Hz", "360 Hz", "800 Hz", "2 kHz", "6 kHz"),
            1: ("50 Hz", "120 Hz", "360 Hz", "800 Hz", "4 kHz")}
MODES = ("simulation", "arcade", "ir")  # PRESET_MODE
OFF_ON = ("off", "on")
CHARACTER = ("triode", "pentode")  # (?) A001 = 1, Remote's switch looked up (pentode)
SIDE = ("front", "back")  # (?)
PHASE = ("normal", "inverted")
GUITAR_BASS = ("guitar", "bass")


def index(name: str) -> int:
    return PARAM_BY_NAME[name].index


EQ_MODE = index("EQ_MODE")


def eq_custom(v: Values) -> bool:
    return v.get(EQ_MODE) == EQ_CUSTOM


def band_freq(n: int) -> Callable[[Values], str]:
    """Text for band n's frequency when the EQ isn't in custom mode (fixed bands)."""
    return lambda v: EQ_BANDS.get(v.get(EQ_MODE), ("",) * 5)[n]


def eq_gain(v: int) -> str:
    return f"{v - 20:+d}"  # 20 = flat; the step in dB is unknown


def percent_40(v: int) -> str:
    return f"{v * 2.5:g}%"  # ARCADE_CONTOUR, as Remote shows it


@dataclass(frozen=True)
class Control:
    param: str
    kind: str = SLIDER
    choices: tuple[str, ...] = ()  # CHOICE: label per wire value
    fmt: Callable[[int], str] = str  # SLIDER: value label
    ends: tuple[str, str] = ("", "")  # SLIDER: words at either end (cold ... hot)
    slot: int = 0  # IR_SLOT: 0 or 1
    when: Callable[[Values], bool] | None = None  # shown only if true...
    otherwise: Callable[[Values], str] | None = None  # ...else this text instead

    @property
    def index(self) -> int:
        return index(self.param)


@dataclass(frozen=True)
class Row:
    label: str
    cells: tuple[Control, ...]
    when: Callable[[Values], bool] | None = None


@dataclass(frozen=True)
class Section:
    title: str
    switch: Control | None = None  # the section's on/off
    head: Control | None = None  # main selector, next to the title (cab, amp, room)
    columns: tuple[str, ...] = ()  # headings of a two-column table
    rows: tuple[Row, ...] = ()


def on_off(name: str) -> Control:
    return Control(name, CHOICE, OFF_ON)


def single(label: str, name: str, **kw) -> Row:
    return Row(label, (Control(name, **kw),))


def pair(label: str, a: str, b: str, **kw) -> Row:
    return Row(label, (Control(a, **kw), Control(b, **kw)))


POWER_AMP = Section("POWER AMP", on_off("SIMUAMP"), Control("SIMUAMP_MODELE", CHOICE, AMPS), rows=(
    single("Volume", "SIMUAMP_GAIN"),
    single("Depth", "SIMUAMP_DEPTH"),
    single("Presence", "SIMUAMP_CONTOUR"),
    single("Character", "SIMUAMP_CHARACTER", kind=CHOICE, choices=CHARACTER),
))

EQ = Section("EQ", on_off("EQ"), Control("EQ_MODE", CHOICE, EQ_MODES), ("Freq", "Gain"), rows=(
    Row("Low cut", (Control("EQ_L_FREQ_LOW"),), when=eq_custom),
    *(Row(f"Band {n + 1}", (Control(f"EQ_L_FREQ_F{n}", when=eq_custom, otherwise=band_freq(n)),
                            Control(f"EQ_L_GAIN{n}", fmt=eq_gain)))
      for n in range(5)),
))

REVERB = Section("REVERB", on_off("REVERB"), Control("REVERB_ROOM", CHOICE, ROOMS), rows=(
    single("Dry/wet", "REVERB_L_DRYWET"),
))

PRESET_LEVEL = Section("PRESET", rows=(single("Level", "PRESET_LEVEL"),))

SIMULATION_LAYOUT = (
    Section("CABINET", on_off("SIMUHP"), Control("SIMUHP_SPEAKER", CAB)),
    POWER_AMP,
    EQ,
    REVERB,
    Section("MICS", columns=("Mic A", "Mic B"), rows=(
        pair("Mic", "SIMUHP_MIC", "SIMUHP_MIC_2", kind=MIC),
        pair("Side", "SIMUHP_POSITION", "SIMUHP_POSITION_2", kind=CHOICE, choices=SIDE),
        pair("Distance", "SIMUHP_DISTANCE", "SIMUHP_DISTANCE_2"),
        pair("Axis", "SIMUHP_CENTER", "SIMUHP_CENTER_2"),
        pair("Level", "SIMUHP_LEVEL", "SIMUHP_LEVEL_2"),
        pair("Phase", "SIMUHP_PHASE", "SIMUHP_PHASE_2", kind=CHOICE, choices=PHASE),
        pair("Mute", "SIMUHP_MUTE", "SIMUHP_MUTE_2", kind=CHOICE, choices=OFF_ON),
    )),
    PRESET_LEVEL,
)

IR_LAYOUT = (
    Section("IR LOADER", on_off("SIMUHP"), columns=("IR A", "IR B"), rows=(
        Row("IR", (Control("SIMUHP_WAVE0", IR_SLOT, slot=0), Control("SIMUHP_WAVE1", IR_SLOT, slot=1))),
    )),
    POWER_AMP,
    EQ,
    REVERB,
    Section("IR MIX", columns=("IR A", "IR B"), rows=(
        pair("Level", "SIMUHP_LEVEL", "SIMUHP_LEVEL_2"),
        pair("Phase", "SIMUHP_PHASE", "SIMUHP_PHASE_2", kind=CHOICE, choices=PHASE),
        pair("Mute", "SIMUHP_MUTE", "SIMUHP_MUTE_2", kind=CHOICE, choices=OFF_ON),
    )),
    PRESET_LEVEL,
)

# Only parameters in protocol.ARCADE_EDITABLE: the unit ignores the rest in
# arcade mode. The simulation values arcade computes are shown under these,
# read-only (SIMULATION_LAYOUT).
ARCADE_LAYOUT = (
    Section("CABINET", head=Control("SIMUHP_SPEAKER", CAB), rows=(
        single("Mic", "SIMUHP_MIC", kind=MIC),
        single("Instrument", "ARCADE_GUITARBASS", kind=CHOICE, choices=GUITAR_BASS),
    )),
    Section("ARCADE", rows=(
        single("Power amp", "ARCADE_POWERAMP", ends=("cold", "hot")),
        single("Tone", "ARCADE_TONE", ends=("dark", "bright")),
        single("Distance", "ARCADE_DISTANCE", ends=("close", "far")),
        single("Contour", "ARCADE_CONTOUR", fmt=percent_40, ends=("vintage", "modern")),
    )),
    Section("ROOM", head=Control("REVERB_ROOM", CHOICE, ROOMS)),
    Section("PRESET", rows=(single("Level", "ARCADE_LEVEL"),)),
)

LAYOUTS = {SIMULATION: SIMULATION_LAYOUT, ARCADE: ARCADE_LAYOUT, IR: IR_LAYOUT}
MODE_CONTROL = Control("PRESET_MODE", CHOICE, MODES)


def controls(layout: tuple[Section, ...]):
    """Every control of a layout, in display order."""
    for s in layout:
        yield from (c for c in (s.switch, s.head) if c)
        for r in s.rows:
            yield from r.cells
