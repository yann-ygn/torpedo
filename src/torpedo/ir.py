"""IR files and the IR transfer format (see PROTOCOL.md "IR transfers").

An IR on the unit is 896 (20 ms) or 1792 (40 ms) samples at 48 kHz, stored
as signed 24-bit little-endian integers normalized to full scale. A transfer
is two `6e` header reports, then `6f` reports carrying 20 samples each, then
`70`. Torpedo Remote exports them as 32-bit float WAVs at half scale (peak
0.5), which `write_wav` mimics.
"""

import struct
from array import array

RATE = 48000
LENGTHS = {20: 896, 40: 1792}  # ms -> samples
FULL_SCALE = 2**23 - 1
SAMPLES_PER_CHUNK = 20
MAKER = "Generic wave file"
NAME_MAX = 31  # the name field is 32 bytes, NUL-terminated (Remote's names were up to 17)


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


def prepare(values: list[float], rate: int, length_ms: int = 20, align: str = "original") -> list[int]:
    """Turn a WAV's samples into what the unit stores, as Torpedo Remote does for 48 kHz files:
    optional trim of the silence before the onset, cut/pad to 20 or 40 ms, peak-normalize."""
    if rate != RATE:
        raise ValueError(f"the IR must be {RATE} Hz (got {rate}); resampling isn't implemented")
    if length_ms not in LENGTHS:
        raise ValueError("length must be 20 or 40 ms")
    if align == "min-latency":
        start = next((k for k, v in enumerate(values) if v != 0.0), 0)
        values = values[start:]
    elif align != "original":
        raise ValueError("align must be original or min-latency (min phase isn't implemented)")
    n = LENGTHS[length_ms]
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
