"""Condense a hidspy log: drop the report ID, collapse runs of same-type messages."""
import sys

N = int(sys.argv[2]) if len(sys.argv) > 2 else 20


def parse(line):
    parts = line.split()
    if len(parts) < 3 or parts[1] not in ("in", "out"):
        return None
    return float(parts[0]), parts[1], bytes.fromhex("".join(parts[3:]))  # parts[2] = report ID


def show(b):
    txt = "".join(chr(c) if 32 <= c < 127 else "." for c in b[:N])
    return f"{b[:N].hex(' '):<{3 * N}} |{txt}|"


rows = [r for r in map(parse, open(sys.argv[1], encoding="utf-8")) if r]
i, prev_t = 0, 0.0
while i < len(rows):
    t, d, b = rows[i]
    j = i
    while j + 1 < len(rows) and rows[j + 1][1] == d and rows[j + 1][2][:2] == b[:2] and d == "in" and b[0] in (3, 6):
        j += 1
    if t - prev_t > 1.5:
        print(f"---------- +{t - prev_t:.1f}s")
    print(f"{t:8.3f} {d:<3} {show(b)}" + (f"  ... x{j - i + 1} (until {rows[j][0]:.3f})" if j > i else ""))
    prev_t = rows[j][0]
    i = j + 1
