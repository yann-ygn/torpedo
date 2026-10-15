"""First write test: nudge ARCADE_CONTOUR by one step, then reselect the preset to discard it.

Usage: python tools/writetest.py          # read only: show setup + current contour
       python tools/writetest.py --write  # do the edit + reselect
"""
import sys, time, hid

CONTOUR = 0x20
d = hid.device(); d.open(0x0483, 0xA334); d.set_nonblocking(1)


def send(*b):
    print("  >>", bytes(b).hex(" "))
    d.write([0] + list(b) + [0] * (64 - len(b)))


def collect(t=0.6):
    out, end = [], time.time() + t
    while time.time() < end:
        p = d.read(65)
        if p:
            p = bytes(p)
            if p[0] != 0x05 and p[:2] != b"\x04\x02":  # meters / heartbeat
                out.append(p)
        else:
            time.sleep(0.001)
    return out


def show(msgs):
    for p in msgs:
        print("  <<", p[:8].hex(" "))


def read_all(query, reply, count):
    send(0x04, query)
    vals, end = {}, time.time() + 3
    while len(vals) < count and time.time() < end:
        vals.update((p[1], p[2]) for p in collect(0.1) if p[0] == reply)
    return vals


collect(0.2)
setup = read_all(0x0b, 0x0c, 18)
params = read_all(0x0a, 0x01, 51)
preset, contour = setup.get(3), params.get(CONTOUR)
print(f"SETUP_PRESET={preset:#04x} PRESET_SOURCE={setup.get(4):#04x} MODIF={setup.get(5)} contour={contour}")

if "--write" in sys.argv:
    if preset is None or preset & 0x80 or contour is None or not 0 <= contour <= 40:
        sys.exit("unexpected state, not writing")
    new = contour + 1 if contour < 40 else contour - 1
    print(f"edit contour {contour} -> {new}")
    send(0x01, CONTOUR, new); show(collect(1.0))
    if "--poll" in sys.argv:  # is 7e a "give me pending events" poll?
        for i in range(10):
            d.write([0, 0x7e] + [0] * 63)
            end = time.time() + 0.05
            while time.time() < end:
                p = d.read(65)
                if p: print(f"  poll {i} <<", bytes(p[:8]).hex(" "))
                else: time.sleep(0.001)
        send(0x04, 0x0b)
        show(m for m in collect(0.5) if m[1] in (3, 4, 5))
    print("contour now", read_all(0x0a, 0x01, 51).get(CONTOUR))
    print(f"reselect preset {preset}")
    send(0x0c, 0x03, preset); show(collect(1.5))
    after = read_all(0x0a, 0x01, 51)
    setup = read_all(0x0b, 0x0c, 18)
    print(f"contour after reselect: {after.get(CONTOUR)} (was {contour}); "
          f"SETUP_PRESET={setup.get(3):#04x} MODIF={setup.get(5)}")
    diff = {i: (params[i], after.get(i)) for i in params if params[i] != after.get(i)}
    print("params differing from before:", diff or "none")
d.close()
