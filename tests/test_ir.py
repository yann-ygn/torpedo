import pytest

from torpedo import ir
from torpedo.device import Torpedo
from torpedo.fake import FakeHid


@pytest.fixture(params=[0, 2], ids=["prompt", "lagging"])
def torpedo(request):
    return Torpedo(FakeHid(lag=request.param))


def test_download(torpedo):
    name, samples = torpedo.download_ir(85)
    assert name == "ML-FRMN-V30-906-E" and len(samples) == 896
    assert max(map(abs, samples)) == ir.FULL_SCALE


def test_upload_and_delete(torpedo, tmp_path):
    rate, values = ir.read_wav("captures/irtest/IRTEST-48K.wav")
    samples = ir.prepare(values, rate, 20, "min-latency")
    assert samples[0] != 0 and max(samples) == ir.FULL_SCALE
    index = torpedo.upload_ir("IRTEST-48K", samples)
    assert index == 86 and torpedo.download_ir(86) == ("IRTEST-48K", samples)
    with pytest.raises(ValueError, match="already has"):
        torpedo.upload_ir("IRTEST-48K", samples)
    with pytest.raises(ValueError, match="isn't"):
        torpedo.delete_ir(85, "IRTEST-48K")
    assert len(torpedo.dev.irs) == 87, "nothing deleted"
    torpedo.delete_ir(86, "IRTEST-48K")
    assert len(torpedo.dev.irs) == 86


def test_prepare():
    rate, values = ir.read_wav("captures/irtest/IRTEST-48K.wav")
    original = ir.prepare(values, rate, 20)
    assert len(original) == 896 and original[100] == 0 and original[101] != 0  # onset kept
    assert len(ir.prepare(values, rate, 40)) == 1792
    rate44, values44 = ir.read_wav("captures/irtest/IRTEST-44K1.wav")
    with pytest.raises(ValueError, match="48000"):
        ir.prepare(values44, rate44)
    with pytest.raises(ValueError, match="min phase"):
        ir.prepare(values, rate, align="min-phase")


def test_wav_round_trip(tmp_path):
    rate, values = ir.read_wav("captures/irtest/IRTEST-48K.wav")
    samples = ir.prepare(values, rate)
    path = tmp_path / "x.wav"
    ir.write_wav(path, samples)
    rate, values = ir.read_wav(path)
    assert rate == 48000 and max(values) == pytest.approx(0.5)
    assert ir.prepare(values, rate) == samples
