"""Min/max of every parameter value Torpedo Remote wrote (01 idx val) in a hidspy log.

Usage: uv run python tools/ranges.py captures/<name>.log
Compare with the provisional ranges in src/torpedo/protocol.py.
"""
import sys

from torpedo.protocol import MSG_PARAM, PARAMS

seen: dict[int, list[int]] = {}
for line in open(sys.argv[1], encoding="utf-8"):
    parts = line.split()
    if len(parts) < 4 or parts[1] != "out":
        continue
    b = bytes.fromhex("".join(parts[3:]))  # parts[2] = report ID
    if b[0] == MSG_PARAM and b[1] < len(PARAMS):
        seen.setdefault(b[1], []).append(b[2])

for p in PARAMS:
    if p.index in seen:
        v = seen[p.index]
        cur = f"{p.lo}..{p.hi}" if p.writable else "read-only"
        flag = "" if (min(v), max(v)) == (p.lo, p.hi) else "  <-- differs"
        print(f"{p.index:02x} {p.name:<20} {min(v):>3}..{max(v):<3} ({len(set(v))} values)  now {cur}{flag}")
