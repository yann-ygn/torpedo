"""IR files and the IR transfer format (see PROTOCOL.md "IR transfers").

An IR on the unit is 896 (20 ms) or 1792 (40 ms) samples at 48 kHz, stored
as signed 24-bit little-endian integers normalized to full scale. A transfer
is two `6e` header reports, then `6f` reports carrying 20 samples each, then
`70`. Torpedo Remote exports them as 32-bit float WAVs at half scale (peak
0.5), which `write_wav` mimics.

Uploads get Remote's processing (checked against what it sent for the
captures/irtest files): resampling to 48 kHz, then the "Align IR" choice,
then cut/pad and peak normalization. Plain Python: IRs are short.
"""

import cmath
import math
import struct
from array import array

RATE = 48000
LENGTHS = {20: 896, 40: 1792}  # ms -> samples
FULL_SCALE = 2**23 - 1
SAMPLES_PER_CHUNK = 20
MAKER = "Generic wave file"
NAME_MAX = 31  # the name field is 32 bytes, NUL-terminated (Remote's names were up to 17)
ALIGNS = ("original", "min-latency", "min-phase")
MIN_PHASE_PEAK = 4  # Remote's min-phase IRs peak there, like the ML IRs
# Windowed-sinc resampler: zero crossings per side, Kaiser beta, cutoff as a
# fraction of the lower Nyquist. Within -76 dB of Remote's 44.1 kHz upload
# (Remote's own filter is unknown).
SINC_HALF, KAISER_BETA, CUTOFF = 16, 8.0, 0.9


def read_wav(path) -> tuple[int, list[float]]:
    """(sample rate, first channel as floats in -1..1). PCM 16/24/32-bit or 32-bit float."""
    raw = open(path, "rb").read()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError(f"{path}: not a WAV file")
    chunks, i = {}, 12
    while i + 8 <= len(raw):
        cid, size = raw[i:i + 4], struct.unpack("<I", raw[i + 4:i + 8])[0]
        chunks[cid] = raw[i + 8:i + 8 + size]
        i += 8 + size + (size & 1)
    if b"fmt " not in chunks or b"data" not in chunks:
        raise ValueError(f"{path}: missing fmt or data chunk")
    tag, channels, rate, _, align, bits = struct.unpack("<HHIIHH", chunks[b"fmt "][:16])
    if tag == 0xFFFE:  # WAVE_FORMAT_EXTENSIBLE: the real tag starts the subformat GUID
        tag = struct.unpack("<H", chunks[b"fmt "][24:26])[0]
    data, width = chunks[b"data"], bits // 8
    frames = [data[k:k + width] for k in range(0, len(data) - align + 1, align)]
    if tag == 3 and bits == 32:
        values = [struct.unpack("<f", f)[0] for f in frames]
    elif tag == 1 and bits in (16, 24, 32):
        values = [int.from_bytes(f, "little", signed=True) / 2 ** (bits - 1) for f in frames]
    else:
        raise ValueError(f"{path}: unsupported WAV format (tag {tag}, {bits} bits)")
    return rate, values


def write_wav(path, samples: list[int]):
    """Write unit samples (24-bit ints) as a mono 48 kHz float WAV, like Remote's export."""
    data = array("f", (s / FULL_SCALE * 0.5 for s in samples)).tobytes()
    fmt = struct.pack("<HHIIHH", 3, 1, RATE, RATE * 4, 4, 32)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", len(body)) + body)


def _bessel_i0(x: float) -> float:
    total = term = 1.0
    k = 1
    while term > 1e-12 * total:
        term *= (x / (2 * k)) ** 2
        total += term
        k += 1
    return total


WINDOW_STEPS = 4096  # Kaiser window table over 0..SINC_HALF, linearly interpolated
_WINDOW = [_bessel_i0(KAISER_BETA * math.sqrt(max(0.0, 1 - (i / WINDOW_STEPS) ** 2)))
           / _bessel_i0(KAISER_BETA) for i in range(WINDOW_STEPS + 2)]


def resample(values: list[float], rate: int, to: int = RATE, count: int | None = None) -> list[float]:
    """Band-limited resampling (Kaiser-windowed sinc); `count` output samples
    (default: the whole signal)."""
    ratio = to / rate
    fc = min(1.0, ratio) * CUTOFF  # cutoff, relative to the input's Nyquist
    span = SINC_HALF / fc  # input samples on each side
    scale = WINDOW_STEPS / SINC_HALF
    out = []
    for m in range(math.ceil(len(values) * ratio) if count is None else count):
        t = m / ratio
        acc = 0.0
        for k in range(max(math.ceil(t - span), 0), min(math.floor(t + span), len(values) - 1) + 1):
            d = (t - k) * fc
            if not d:
                acc += values[k] * fc
                continue
            pos = abs(d) * scale
            i = int(pos)
            window = _WINDOW[i] + (_WINDOW[i + 1] - _WINDOW[i]) * (pos - i)
            acc += values[k] * fc * window * math.sin(math.pi * d) / (math.pi * d)
        out.append(acc)
    return out


def _fft(a: list[complex], inverse: bool = False) -> list[complex]:
    """Iterative radix-2 FFT; len(a) must be a power of 2."""
    n = len(a)
    a = list(a)
    j = 0
    for i in range(1, n):  # bit-reversal permutation
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    size = 2
    while size <= n:
        half = size // 2
        step = cmath.exp((2j if inverse else -2j) * math.pi / size)
        twiddles = [step ** k for k in range(half)]
        for start in range(0, n, size):
            for k in range(half):
                u, v = a[start + k], a[start + k + half] * twiddles[k]
                a[start + k], a[start + k + half] = u + v, u - v
        size *= 2
    return [v / n for v in a] if inverse else a


def minimum_phase(values: list[float]) -> list[float]:
    """Same magnitude response, energy as early as possible (real cepstrum
    folding). The FFT is 4x the IR or more: enough for Remote's result."""
    n = 1 << max(12, (4 * len(values) - 1).bit_length())
    spectrum = _fft(list(values) + [0.0] * (n - len(values)))
    floor = max(abs(v) for v in spectrum) * 1e-9  # keep log() finite at spectral zeros
    cepstrum = _fft([math.log(max(abs(v), floor)) for v in spectrum], inverse=True)
    folded = ([cepstrum[0]] + [2 * c for c in cepstrum[1:n // 2]] + [cepstrum[n // 2]]
              + [0.0] * (n // 2 - 1))
    result = _fft([cmath.exp(v) for v in _fft(folded)], inverse=True)
    return [v.real for v in result[:len(values)]]


def prepare(values: list[float], rate: int, length_ms: int = 20, align: str = "original") -> list[int]:
    """Turn a WAV's samples into what the unit stores, as Torpedo Remote does:
    resample to 48 kHz, align (original; min-latency trims the silence before
    the onset; min-phase converts and puts the peak at sample 4), cut/pad to
    20 or 40 ms, peak-normalize."""
    if length_ms not in LENGTHS:
        raise ValueError("length must be 20 or 40 ms")
    if align not in ALIGNS:
        raise ValueError(f"align must be one of {', '.join(ALIGNS)}")
    n = LENGTHS[length_ms]
    if rate != RATE:
        count = None  # min phase needs the whole IR; the others only what they keep
        if align != "min-phase":  # output m depends on inputs within `span` of m * rate / RATE
            onset = next((k for k, v in enumerate(values) if v), 0) if align == "min-latency" else 0
            span = SINC_HALF / (min(1.0, RATE / rate) * CUTOFF)
            count = math.ceil(max(onset - span, 0) * RATE / rate) + n + 2
        values = resample(values, rate, count=count)
    if align == "min-latency":
        start = next((k for k, v in enumerate(values) if v != 0.0), 0)
        values = values[start:]
    elif align == "min-phase":
        values = minimum_phase(values)
        shift = max(range(len(values)), key=lambda k: abs(values[k])) - MIN_PHASE_PEAK
        values = values[shift:] if shift >= 0 else [0.0] * -shift + values
    values = (values + [0.0] * n)[:n]
    peak = max(abs(v) for v in values)
    if peak == 0:
        raise ValueError("the IR is silent")
    return [round(v / peak * FULL_SCALE) for v in values]


def fletcher8(data: bytes) -> tuple[int, int]:
    a = b = 0
    for x in data:
        a = (a + x) & 0xFF
        b = (b + a) & 0xFF
    return a, b


def fletcher16(data: bytes) -> bytes:
    """Stream trailer: 16-bit byte sum, then 16-bit sum of the running sums, big-endian."""
    a = b = 0
    for x in data:
        a = (a + x) & 0xFFFF
        b = (b + a) & 0xFFFF
    return a.to_bytes(2, "big") + b.to_bytes(2, "big")


def pack_samples(samples: list[int]) -> bytes:
    """Sample bytes + checksum trailer, as carried by the `6f` reports."""
    data = b"".join(s.to_bytes(3, "little", signed=True) for s in samples)
    return data + fletcher16(data)


def headers(index: int, name: str, stream: bytes, n_samples: int, upload: bool) -> list[bytes]:
    """The two `6e` reports (without padding to 64 bytes).

    `index` is the 0-based position for a download and the 1-based slot an
    upload goes to (Remote sent len(bank) + 1). The length field is the stream
    size, plus 0x60 for an upload."""
    length = len(stream) + (0x60 if upload else 0)
    h0 = bytes([0x6E, 0, 6, 0, 0]) + length.to_bytes(2, "big") + bytes([1, index, 3, 0x60])
    h0 += name.encode("ascii").ljust(64 - len(h0), b"\0")
    a, b = fletcher8(stream)
    h1 = bytes([0x6E, 0, 6, 0, 0, 0, 0, 0]) + MAKER.encode().ljust(32, b"\0")
    h1 += bytes([n_samples // LENGTHS[20], a, b])
    h1 += bytes([(sum(h0[9:]) + sum(h1[8:])) & 0xFF])
    return [h0, h1]


def chunks(stream: bytes) -> list[bytes]:
    """`6f` reports: 20 samples (60 bytes) each, zero-padded."""
    size = SAMPLES_PER_CHUNK * 3
    return [bytes([0x6F]) + stream[i:i + size].ljust(size, b"\0") for i in range(0, len(stream), size)]


def parse_transfer(h0: bytes, h1: bytes, data: list[bytes], upload: bool = False) -> tuple[str, list[int]]:
    """Name and samples from a transfer; checks both checksums."""
    length = int.from_bytes(h0[5:7], "big") - (0x60 if upload else 0)
    stream = b"".join(c[1:1 + SAMPLES_PER_CHUNK * 3] for c in data)[:length]
    body, trailer = stream[:-4], stream[-4:]
    if fletcher16(body) != trailer or tuple(h1[41:43]) != fletcher8(stream):
        raise ValueError("IR checksum mismatch")
    name = h0[11:].split(bytes(1))[0].decode("ascii", "replace")
    return name, [int.from_bytes(body[i:i + 3], "little", signed=True) for i in range(0, len(body), 3)]
