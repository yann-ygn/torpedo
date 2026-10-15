import json

import pytest

from torpedo import backup
from torpedo.device import Torpedo
from torpedo.fake import FakeHid
from torpedo.protocol import find_param


@pytest.fixture
def torpedo():
    t = Torpedo(FakeHid())
    t.select_preset(2)
    return t


def strip_date(state):
    return {k: v for k, v in state.items() if k != "date"}


def test_read_state_returns_to_the_current_preset(torpedo):
    state = backup.read_state(torpedo)
    assert len(state["presets"]) == 128 and state["current_preset"] == 2
    assert state["presets"][1]["params"]["ARCADE_CONTOUR"] == 30
    assert state["presets"][0]["name"] == "BDELUXE 1 RBN" and state["presets"][0]["arcade"]
    assert state["setup"]["midi_channel"] == 0x10 and state["serial"] == "032C4B94"
    assert torpedo.preset == 2 and torpedo.dev.stores == []


def test_refuses_with_unsaved_edits(torpedo):
    torpedo.set_param(find_param("arcade_contour"), 5)
    with pytest.raises(ValueError, match="unsaved edits"):
        backup.read_state(torpedo)


def test_restore_round_trip(torpedo):
    saved = backup.read_state(torpedo)
    fake = torpedo.dev
    fake.presets[5][0x20] = 40  # contour
    fake.presets[5][0x22] = 0  # simulation mode
    fake.presets[5][0x02] = 25  # simuamp gain
    fake.names[6] = "RENAMED"
    fake.setup[0x0E] = 0  # output dry/dry
    current = backup.read_state(torpedo)
    plan = backup.plan_restore(saved, current)
    assert plan.setup == {"output": 3} and [p["index"] for p in plan.presets] == [5, 6]
    backup.apply_restore(torpedo, plan)
    assert fake.stores == [5, 6] and torpedo.preset == 2
    assert strip_date(backup.read_state(torpedo)) == strip_date(saved)
    assert not backup.plan_restore(saved, backup.read_state(torpedo))


def test_restore_subset_and_empty_slots(torpedo):
    saved = backup.read_state(torpedo)
    torpedo.save(75, "NEW ONE")  # was an empty USER slot in the backup
    torpedo.select_preset(2)
    torpedo.dev.names[6] = "RENAMED"
    current = backup.read_state(torpedo)
    plan = backup.plan_restore(saved, current, presets={75}, setup=False)
    assert not plan.presets and "no command to empty a slot" in plan.skipped[0]
    plan = backup.plan_restore(saved, current, presets={6})
    assert [p["index"] for p in plan.presets] == [6]


def test_load_file_validates(torpedo, tmp_path):
    path = tmp_path / "b.json"
    state = backup.read_state(torpedo)
    backup.save_file(state, path)
    assert backup.load_file(path) == state
    state["presets"][3]["params"].pop("EQ")
    path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="A004"):
        backup.load_file(path)
    path.write_text(json.dumps({"format": "something else"}), encoding="utf-8")
    with pytest.raises(ValueError, match="not a torpedo-backup"):
        backup.load_file(path)
