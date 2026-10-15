import pytest

from torpedo.device import IgnoredWrite, Torpedo
from torpedo.fake import A001, FakeHid
from torpedo.protocol import MSG_POLL, PARAMS, PRESET_MODIFIED, SETUP, find_param

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
    assert torpedo.preset == PRESET_MODIFIED | 0
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


def test_select_preset(torpedo):
    changes = torpedo.select_preset(1)
    assert changes == {0x13: 30, 0x17: 30, 0x1A: 3, 0x20: 30}
    assert torpedo.preset == 1


@pytest.mark.parametrize("name, value", [
    ("arcade_contour", 41), ("arcade_contour", -1), ("simuhp_speaker", 3), ("eq_l_gain0", 128),
])
def test_validation_sends_nothing(torpedo, name, value):
    torpedo.read_params()
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
