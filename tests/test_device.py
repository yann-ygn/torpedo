import math

import pytest

from torpedo.device import IgnoredWrite, Torpedo
from torpedo.fake import A001, FakeHid
from torpedo.protocol import (CLIP_IN, EDIT_BUFFER, EMPTY_PARAMS, IR, MSG_POLL, PARAMS, PRESET_MODE, SETUP,
                              SETUP_OPTIONS, Meters, find_param, meter_db, preset_index)

CONTOUR = find_param("arcade_contour")
PARAMS_ALL = frozenset(range(len(PARAMS)))


@pytest.fixture(params=[0, 2], ids=["prompt", "lagging"])
def torpedo(request):
    """The real device may answer a command with `05` before its reply is queued."""
    return Torpedo(FakeHid(lag=request.param))


def commands(t: Torpedo) -> list[bytes]:
    """OUT reports except polls, trimmed to the 3 meaningful bytes."""
    return [b[:3] for b in t.dev.sent if b[0] != MSG_POLL]


def test_reads(torpedo):
    info = torpedo.info()
    assert (info.serial, info.firmware) == ("032C4B94", "4.23")
    assert torpedo.read_params() == dict(enumerate(A001))
    assert len(torpedo.read_setup()) == len(SETUP)
    assert torpedo.preset == 0


def test_set_param_reports_linked_changes_and_modified_flag(torpedo):
    assert torpedo.set_param(CONTOUR, 22) == {0x13: 22, 0x17: 22, 0x20: 22}
    assert torpedo.params[0x20] == 22
    assert torpedo.preset == EDIT_BUFFER and torpedo.source == 0
    assert torpedo.dev.queue == type(torpedo.dev.queue)(), "exchange drained the queue"


def test_discard_restores_preset(torpedo):
    torpedo.set_param(CONTOUR, 30)
    torpedo.set_param(find_param("reverb_room"), 5)
    torpedo.discard()
    assert torpedo.read_params() == dict(enumerate(A001))
    assert torpedo.preset == 0


def test_noop_writes_send_nothing(torpedo):
    torpedo.read_params()
    torpedo.read_setup()
    before = len(commands(torpedo))
    assert torpedo.set_param(CONTOUR, A001[0x20]) == {}
    assert torpedo.select_preset(0) == {}
    assert len(commands(torpedo)) == before


def test_simulation_params_refused_in_arcade_mode(torpedo):
    gain = find_param("simuamp_gain")
    torpedo.read_params()
    before = len(commands(torpedo))
    with pytest.raises(IgnoredWrite, match="arcade mode"):
        torpedo.set_param(gain, 25)
    assert len(commands(torpedo)) == before, "refused before sending"
    torpedo.set_param(find_param("reverb_room"), 3)  # shared by both modes
    torpedo.set_param(find_param("preset_mode"), 0)
    assert torpedo.set_param(gain, 25) == {0x02: 25}


def test_unexpectedly_ignored_write_is_reported(torpedo, monkeypatch):
    """If the arcade table is incomplete, the missing echo still gives a clear error."""
    monkeypatch.setattr("torpedo.device.ARCADE_EDITABLE", PARAMS_ALL)
    torpedo.timeout = 0.05
    with pytest.raises(IgnoredWrite, match="ignored SIMUAMP_GAIN=25"):
        torpedo.set_param(find_param("simuamp_gain"), 25)


def test_read_cabs(torpedo):
    cabs = torpedo.read_cabs()
    assert len(cabs) == 32 and [c.index for c in cabs] == list(range(32))
    lux = cabs[0x10]
    assert (lux.name, lux.maker) == ("LuxTweeDe", "Hop Pole Studios")
    assert lux.mics[5] == "Dyn 7b" and len(lux.mics) == 8
    assert torpedo.read_cabs(2) == []


def test_select_cab_keeps_mic_like_remote(torpedo):
    """Captured from Torpedo Remote: DarkPrinz with mic 3 (Dyn 421) -> Brit 65C sets mic 4."""
    cabs = torpedo.read_cabs()
    torpedo.select_cab(0x12, cabs)  # A001 has Dyn 7b (mic 5), which DarkPrinz lacks
    assert torpedo.params[0x0D] == 5
    torpedo.set_param(find_param("simuhp_mic"), 3)
    assert torpedo.select_cab(1, cabs) == {0x08: 1, 0x0D: 4}
    assert torpedo.select_cab(2, cabs) == {0x08: 2}  # same mic list: mic untouched
    with pytest.raises(ValueError):
        torpedo.select_cab(32, cabs)


def test_select_cab_moves_mic_b_in_simulation_mode(torpedo):
    """1 cab, 2 mics: mic B indexes the same mic list. Arcade ignores mic B writes."""
    cabs = torpedo.read_cabs()
    torpedo.select_cab(1, cabs)  # A001 arcade, mic B = 0 "Cnd Gem II" (absent anyway)
    torpedo.set_param(find_param("preset_mode"), 0)
    torpedo.set_param(find_param("simuhp_mic_2"), 3)  # Rbn 160 on Brit 65C
    torpedo.set_param(find_param("simuhp_mic"), 4)  # Dyn 421
    assert torpedo.select_cab(0x12, cabs) == {0x08: 0x12, 0x0D: 3, 0x26: 2}


def test_select_ir(torpedo):
    irs = torpedo.read_irs()
    assert len(irs) == 86 and irs[85].name == "ML-FRMN-V30-906-E"
    with pytest.raises(IgnoredWrite):  # A001 is in arcade mode
        torpedo.select_ir(0, 85, irs)
    torpedo.set_param(find_param("preset_mode"), 2)
    assert torpedo.select_ir(0, 85, irs) == {0x09: 85}  # folder already 0
    assert torpedo.select_ir(1, 15, irs) == {0x0A: 15}
    for bad in ((2, 0), (0, 86)):
        with pytest.raises(ValueError):
            torpedo.select_ir(*bad, irs)


def test_discard_returns_to_the_edited_preset(torpedo):
    """SETUP_PRESET becomes 0x80 once edited; the source preset is in SETUP_PRESET_SOURCE."""
    torpedo.select_preset(1)
    torpedo.set_param(CONTOUR, 10)
    assert (torpedo.preset, torpedo.source, torpedo.edited) == (EDIT_BUFFER, 1, True)
    torpedo.discard()
    assert torpedo.preset == 1 and torpedo.params[0x20] == 30
    assert torpedo.discard() == {}, "nothing to discard"


def test_front_panel_changes(torpedo):
    """A discarded edit stays on the unit ("A001*"), and the panel can load it again."""
    torpedo.set_param(CONTOUR, 10)
    torpedo.discard()
    torpedo.dev.panel_select(1)
    torpedo.poll()
    assert (torpedo.preset, torpedo.params[0x20]) == (1, 30)
    torpedo.dev.panel_select_kept()
    torpedo.poll()
    assert (torpedo.preset, torpedo.source, torpedo.params[0x20]) == (EDIT_BUFFER, 0, 10)
    torpedo.dev.panel_select(0)
    torpedo.dev.panel_param(0x1D, 1)  # power amp, on an unedited preset
    torpedo.dev.panel_setup(SETUP.index("SETUP_MIDI_CC"), 0)
    torpedo.poll()
    assert (torpedo.preset, torpedo.source, torpedo.params[0x1D]) == (EDIT_BUFFER, 0, 1)
    assert torpedo.setup[SETUP.index("SETUP_MIDI_CC")] == 0
    assert commands(torpedo)[-1] == bytes([0x04, 0x0B, 0]), "only the setup re-read was sent"


def test_meters(torpedo):
    assert torpedo.meters == Meters()
    torpedo.dev.panel_signal(168, 40, CLIP_IN)
    torpedo.poll()
    assert (torpedo.meters.input, torpedo.meters.output) == (168, 40)
    assert torpedo.meters.clip_in and not torpedo.meters.clip_out
    torpedo.dev.panel_signal(0, 0)
    torpedo.read_params()  # every exchange ends on an `05`
    assert torpedo.meters == Meters()
    assert round(meter_db(168)) == -4 and meter_db(0) == -math.inf


def test_out_level_encoder_mutes_at_zero(torpedo):
    level, mute = SETUP_OPTIONS["level"].index, SETUP_OPTIONS["mute"].index
    torpedo.read_setup()
    for v in (1, 0):
        torpedo.dev.panel_level(v)
    torpedo.poll()
    assert (torpedo.setup[level], torpedo.setup[mute]) == (0, 1)
    torpedo.dev.panel_level(1)
    torpedo.poll()
    assert (torpedo.setup[level], torpedo.setup[mute]) == (1, 0)


def test_read_names(torpedo):
    names = torpedo.read_names()
    assert len(names) == 129 and names[-1].index == EDIT_BUFFER
    assert names[0].name == "BDELUXE 1 RBN" and names[0].arcade
    assert names[75].empty and not names[74].empty


def test_names_carry_the_mode_the_panel_shows(torpedo):
    torpedo.dev.presets[5][PRESET_MODE] = IR
    names = torpedo.read_names()
    assert [names[n].label for n in (0, 5, 75)] == ["A001", "I006", "S076"]
    assert names[5].mode == IR and not names[5].arcade
    assert [preset_index(s) for s in ("S021", "a021", "I021", "21")] == [20] * 4
    for bad in ("X021", "0", "S129", ""):
        with pytest.raises(ValueError):
            preset_index(bad)


def test_save_into_empty_slot(torpedo):
    torpedo.set_param(CONTOUR, 22)
    saved = torpedo.save(75, "testcap")
    assert (saved.index, saved.name) == (75, "TESTCAP")
    assert torpedo.dev.stores == [75] and torpedo.preset == 75 and not torpedo.edited
    assert torpedo.dev.presets[75][0x20] == 22 and torpedo.read_names()[75].name == "TESTCAP"


def test_save_keeping_the_name_skips_the_rename(torpedo):
    torpedo.set_param(CONTOUR, 22)
    saved = torpedo.save(75, "bdeluxe 1 rbn", overwrite=True)  # the edit buffer's name already
    assert saved.name == "BDELUXE 1 RBN" and torpedo.dev.stores == [75]
    assert not [c for c in commands(torpedo) if c[:2] == bytes([0x03, EDIT_BUFFER])]


def test_save_refuses_to_overwrite(torpedo):
    with pytest.raises(ValueError, match="holds"):
        torpedo.save(3)
    for bad in ("", "x" * 26, "lower-case!"):
        with pytest.raises(ValueError):
            torpedo.save(75, bad)
    with pytest.raises(ValueError):
        torpedo.save(128)
    assert torpedo.dev.stores == [], "nothing written"
    torpedo.save(3, overwrite=True)
    assert torpedo.dev.stores == [3] and torpedo.read_names()[3].name == "BDELUXE 1 RBN"


def test_clear_another_preset(torpedo):
    torpedo.read_params()
    names = torpedo.clear_preset(3)
    assert names[3].name == "USER" and names[3].empty and not names[3].arcade
    assert torpedo.dev.clears == [3] and torpedo.dev.presets[3] == EMPTY_PARAMS
    assert commands(torpedo)[-3:] == [bytes([4, 0x69, 3]), bytes([4, 0x0E, 0]), bytes([4, 9, 0x81])]
    assert torpedo.preset == 0 and torpedo.params[0x20] == A001[0x20]


def test_clear_current_preset_rereads_its_parameters(torpedo):
    torpedo.read_params()
    names = torpedo.clear_preset(0)
    assert names[0].name == names[-1].name == "USER"
    assert torpedo.params == dict(enumerate(EMPTY_PARAMS)) and not torpedo.edited


def test_clear_refuses_a_preset_with_unsaved_edits(torpedo):
    torpedo.set_param(CONTOUR, 22)
    with pytest.raises(ValueError, match="unsaved edits"):
        torpedo.clear_preset(0)
    with pytest.raises(ValueError):
        torpedo.clear_preset(128)
    assert torpedo.dev.clears == []
    torpedo.clear_preset(3)  # edits to another preset don't matter
    assert torpedo.dev.clears == [3] and torpedo.edited


def test_setup_options(torpedo):
    opts = SETUP_OPTIONS
    assert opts["midi_channel"].encode("16") == 0x0F and opts["midi_channel"].decode(0) == "1"
    assert opts["midi_channel"].encode("ALL") == 0x10 and opts["midi_channel"].decode(0x10) == "all"
    assert opts["midi_channel"].decode(0x11).startswith("?")
    assert opts["output"].encode("Wet/Wet") == 3 and opts["spdif_freq"].decode(1) == "96"
    for name, bad in (("midi_channel", "0"), ("midi_channel", "17"), ("output", "wet"),
                      ("level", "108"), ("mute", "yes")):
        with pytest.raises(ValueError):
            opts[name].encode(bad)
    assert torpedo.set_setup(opts["output"], 1) and torpedo.read_setup()[0x0E] == 1
    assert not torpedo.set_setup(opts["output"], 1), "no-op"
    with pytest.raises(ValueError):
        torpedo.set_setup(opts["midi_channel"], 0x11)


def test_select_preset(torpedo):
    changes = torpedo.select_preset(1)
    assert changes == {0x13: 30, 0x17: 30, 0x1A: 3, 0x20: 30}
    assert torpedo.preset == 1


@pytest.mark.parametrize("name, value", [
    ("arcade_contour", 41), ("arcade_contour", -1), ("simuhp_folder_ir_0", 1), ("simuhp_speaker", 32), ("eq_l_gain0", 128),
])
def test_validation_sends_nothing(torpedo, name, value):
    torpedo.read_params()
    torpedo.read_cabs()  # the cab bank's size bounds SIMUHP_SPEAKER (read on demand otherwise)
    before = len(commands(torpedo))
    with pytest.raises(ValueError):
        torpedo.set_param(find_param(name), value)
    with pytest.raises(ValueError):
        torpedo.select_preset(128)
    assert len(commands(torpedo)) == before


def test_find_param():
    assert find_param("ARCADE-CONTOUR") is find_param("0x20") is find_param("32") is CONTOUR
    with pytest.raises(KeyError):
        find_param("nope")
    with pytest.raises(KeyError):
        find_param(str(len(PARAMS)))


def test_param_table():
    assert len(PARAMS) == 51 and [p.index for p in PARAMS] == list(range(51))
    for p, v in zip(PARAMS, A001):
        assert p.lo <= v <= p.hi, f"{p.name}: A001 value {v} outside {p.lo}..{p.hi}"
