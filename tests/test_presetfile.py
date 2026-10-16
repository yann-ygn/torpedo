import json

import pytest

from torpedo import presetfile
from torpedo.device import MIC, MIC_2, SPEAKER, WAVES, Torpedo
from torpedo.fake import FakeHid
from torpedo.protocol import ARCADE, IR, MSG_PARAM, PRESET_MODE, Cab, find_param

CONTOUR = find_param("arcade_contour")


@pytest.fixture
def torpedo():
    return Torpedo(FakeHid())


def param_writes(t: Torpedo) -> list[bytes]:
    return [b[:3] for b in t.dev.sent if b[0] == MSG_PARAM]


def test_round_trip_into_another_slot(torpedo, tmp_path):
    preset = presetfile.export(torpedo, 1)
    assert torpedo.preset == 0, "back on the current preset"
    assert preset["cab"] == "LuxTweeDe" and preset["mics"] == ["Dyn 7b", "Cnd Gem II"]
    path = tmp_path / "a002.json"
    presetfile.save_file(preset, path)
    loaded = presetfile.load_file(path)
    assert presetfile.import_preset(torpedo, loaded, 79) == "BDELUXE 1 DYN421"
    assert torpedo.dev.presets[79] == torpedo.dev.presets[1]
    assert torpedo.dev.names[79] == "BDELUXE 1 DYN421" and torpedo.dev.stores == [79]


def test_import_as_current_sound_is_unsaved_and_named(torpedo):
    preset = presetfile.export(torpedo, 1)
    presetfile.import_preset(torpedo, preset)
    assert torpedo.edited and torpedo.dev.stores == []
    assert torpedo.dev.params == torpedo.dev.presets[1]
    assert torpedo.read_buffer_name() == "BDELUXE 1 DYN421"


def test_export_current_sound_includes_edits(torpedo):
    torpedo.set_param(CONTOUR, 33)
    preset = presetfile.export(torpedo)
    assert preset["params"]["ARCADE_CONTOUR"] == 33 and preset["name"] == "BDELUXE 1 RBN"
    with pytest.raises(ValueError, match="unsaved edits"):
        presetfile.export(torpedo, 1)
    with pytest.raises(ValueError, match="unsaved edits"):
        presetfile.import_preset(torpedo, preset)


def test_missing_cab_sends_nothing(torpedo):
    preset = presetfile.export(torpedo, 1)
    preset["cab"] = "Eddie"
    with pytest.raises(ValueError, match='no cab "Eddie"'):
        presetfile.import_preset(torpedo, preset, 79)
    assert param_writes(torpedo) == [] and torpedo.dev.stores == []


def test_refuses_to_overwrite(torpedo):
    preset = presetfile.export(torpedo, 1)
    with pytest.raises(ValueError, match="holds"):
        presetfile.import_preset(torpedo, preset, 2)
    assert param_writes(torpedo) == []


CABS = [Cab(0, "Silver77", "", ["Dyn 57", "Rbn 121"] + [""] * 6),
        Cab(1, "Brit VintC", "", ["Rbn 121", "Cnd 87", "Dyn 57"] + [""] * 5)]
IRS = [Cab(0, "ML-A", "", []), Cab(1, "ML-B", "", [])]


def preset_file(mode: int, cab="Brit VintC", mics=("Dyn 57", "Rbn 121"), irs=("ML-B", "ML-A")):
    params = {p: 0 for p in presetfile.STORED}
    params["PRESET_MODE"] = mode
    return {"cab": cab, "mics": list(mics), "irs": list(irs), "params": params}


def test_resolve_looks_names_up_in_this_units_banks():
    values = presetfile.resolve(preset_file(0), CABS, IRS)
    assert (values[SPEAKER], values[MIC], values[MIC_2]) == (1, 2, 0)
    assert (values[WAVES[0]], values[WAVES[1]]) == (1, 0)


def test_resolve_only_requires_what_the_mode_uses():
    with pytest.raises(ValueError, match='mic "Knightfall"'):
        presetfile.resolve(preset_file(0, mics=("Dyn 57", "Knightfall")), CABS, IRS)
    values = presetfile.resolve(preset_file(ARCADE, mics=("Dyn 57", "Knightfall")), CABS, IRS)
    assert values[MIC] == 2
    values = presetfile.resolve(preset_file(IR, cab="Eddie"), CABS, IRS)
    assert values[WAVES[0]] == 1
    with pytest.raises(ValueError, match='IR "ML-Z"'):
        presetfile.resolve(preset_file(IR, irs=("ML-A", "ML-Z")), CABS, IRS)
    presetfile.resolve(preset_file(0, irs=("ML-A", "ML-Z")), CABS, IRS)
    with pytest.raises(ValueError, match='cab "Eddie"'):
        presetfile.resolve(preset_file(ARCADE, cab="Eddie"), CABS, IRS)


def test_load_file_validates(torpedo, tmp_path):
    preset = presetfile.export(torpedo, 1)
    path = tmp_path / "p.json"
    for bad, match in [({"format": "torpedo-backup"}, "not a torpedo-preset"),
                       ({"version": 2}, "version 2"),
                       ({"name": "lower-case!"}, "preset name"),
                       ({"irs": ["ML-A"]}, "two IR names"),
                       ({"params": {**preset["params"], "SIMUAMP_GAIN": 31}}, "SIMUAMP_GAIN"),
                       ({"params": {**preset["params"], "SIMUHP_SPEAKER": 1}}, "parameters")]:
        path.write_text(json.dumps(preset | bad), encoding="utf-8")
        with pytest.raises(ValueError, match=match):
            presetfile.load_file(path)
