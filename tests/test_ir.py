import math

import pytest

from torpedo import ir
from torpedo.device import Torpedo
from torpedo.fake import FakeHid
from torpedo.protocol import find_param


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


def test_uploaded_ir_can_be_selected(torpedo):
    """The IR slots' range is the bank's size, which grows with uploads."""
    wave0 = find_param("simuhp_wave0")
    torpedo.set_param(find_param("preset_mode"), 2)
    with pytest.raises(ValueError, match="bank 1 holds 86"):
        torpedo.set_param(wave0, 86)
    rate, values = ir.read_wav("captures/irtest/IRTEST-48K.wav")
    index = torpedo.upload_ir("IRTEST-48K", ir.prepare(values, rate, 20, "min-latency"))
    assert torpedo.select_ir(0, index, torpedo.read_irs()) == {wave0.index: 86}
    torpedo.select_ir(0, 0)
    torpedo.delete_ir(86, "IRTEST-48K")
    with pytest.raises(ValueError, match="bank 1 holds 86"):
        torpedo.set_param(wave0, 86)


def test_prepare():
    rate, values = ir.read_wav("captures/irtest/IRTEST-48K.wav")
    original = ir.prepare(values, rate, 20)
    assert len(original) == 896 and original[100] == 0 and original[101] != 0  # onset kept
    assert len(ir.prepare(values, rate, 40)) == 1792
    with pytest.raises(ValueError, match="align"):
        ir.prepare(values, rate, align="min-fuss")


def remote(name: str) -> list[int]:
    """What Torpedo Remote uploaded (captures/irtest/README.md)."""
    rate, values = ir.read_wav(f"captures/irtest/{name}")
    return ir.prepare(values, rate)


def test_min_phase_matches_remote():
    rate, values = ir.read_wav("captures/irtest/IRTEST-48K.wav")
    ours = ir.prepare(values, rate, align="min-phase")
    assert max(range(len(ours)), key=lambda k: abs(ours[k])) == ir.MIN_PHASE_PEAK
    # Remote's own normalization is off by up to 1.6 LSB (float processing)
    assert max(abs(a - b) for a, b in zip(ours, remote("REMOTE-48K-MINPHASE.wav"))) <= 2


def test_resampling_is_close_to_remote():
    rate, values = ir.read_wav("captures/irtest/IRTEST-44K1.wav")
    ours, theirs = ir.prepare(values, rate), remote("REMOTE-44K1.wav")
    error = math.sqrt(sum((a - b) ** 2 for a, b in zip(ours, theirs)) / sum(b * b for b in theirs))
    assert len(ours) == 896 and 20 * math.log10(error) < -70


def test_downsampling_keeps_a_tone():
    tone = [math.sin(2 * math.pi * 1000 * k / 96000) for k in range(960)]
    out = ir.resample(tone, 96000)
    assert len(out) == 480
    expected = [math.sin(2 * math.pi * 1000 * k / 48000) for k in range(480)]
    assert max(abs(a - b) for a, b in zip(out[40:-40], expected[40:-40])) < 1e-3


def test_wav_round_trip(tmp_path):
    rate, values = ir.read_wav("captures/irtest/IRTEST-48K.wav")
    samples = ir.prepare(values, rate)
    path = tmp_path / "x.wav"
    ir.write_wav(path, samples)
    rate, values = ir.read_wav(path)
    assert rate == 48000 and max(values) == pytest.approx(0.5)
    assert ir.prepare(values, rate) == samples
