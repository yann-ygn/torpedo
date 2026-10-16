"""Log what the Torpedo sends on its own: front-panel changes, meters in `05`.

Sends nothing but `7e` polls (like Torpedo Remote, ~50/s), so close Remote
first or the two will split the replies. Type a note + Enter to label the
next action in the log; Ctrl+C (or creating tools/STOP) stops.

Usage: uv run python tools/listen.py captures/<name>.log

Same format as hidspy.py, so summarize.py works on it. The .log skips the
heartbeat and repeated `05` reports, but keeps every `05` whose first bytes
change (the meters, probably); the .log.raw keeps everything. A disconnect,
a reconnect and the unit going quiet are logged as `event` lines.
"""
import os
import sys
import threading
import time

from torpedo.device import open_hid
from torpedo.protocol import HEARTBEAT, MSG_EMPTY, MSG_PARAM, MSG_POLL, MSG_SETUP, PARAMS, REPORT_LEN, SETUP

PERIOD = 0.02
METER_BYTES = 5  # 05 + 4 bytes; the rest of the report is a stale buffer

dev = open_hid()
here = os.path.dirname(os.path.abspath(__file__))
log_path = sys.argv[1]
stop_path = os.path.join(here, "STOP")
if os.path.exists(stop_path):
    os.remove(stop_path)

log = open(log_path, "w", buffering=1, encoding="utf-8")
raw = open(log_path + ".raw", "w", buffering=1, encoding="utf-8")
t0 = time.time()


def write(f, text):
    f.write(f"{time.time() - t0:9.3f} {text}\n")


def describe(p: bytes) -> str:
    if p[0] == MSG_PARAM and p[1] < len(PARAMS):
        return f"{PARAMS[p[1]].name} = {p[2]}"
    if p[0] == MSG_SETUP and p[1] < len(SETUP):
        return f"{SETUP[p[1]]} = {p[2]}"
    if p[0] == MSG_EMPTY:
        return "05 " + p[1:METER_BYTES].hex(" ")
    return p[:16].hex(" ")


def notes():
    while True:
        try:
            text = input()
        except EOFError:
            return
        for f in (log, raw):
            write(f, f"note             {text}")
        print(f"  -- note: {text}")


def event(text):
    for f in (log, raw):
        write(f, f"{'event':<16} {text}")
    print(f"{time.time() - t0:9.3f}  ** {text}")


def reopen():
    """Wait for the unit to come back after an unplug or power cycle."""
    gone = time.time()
    while not os.path.exists(stop_path):
        try:
            d = open_hid()
        except RuntimeError:
            time.sleep(0.2)
            continue
        event(f"reopened after {time.time() - gone:.1f} s")
        return d
    return None


threading.Thread(target=notes, daemon=True).start()
print(f"listening, logging to {log_path} (type a note + Enter, Ctrl+C to stop)")
last_meter, silent = None, 0
try:
    while dev and not os.path.exists(stop_path):
        start = time.time()
        try:
            if dev.write([0, MSG_POLL] + [0] * (REPORT_LEN - 1)) < 0:
                raise OSError("write failed")
            write(raw, f"{'out':<16} 00 {bytes([MSG_POLL]).ljust(REPORT_LEN, bytes(1)).hex(' ')}")
            p = bytes(dev.read(REPORT_LEN, 100))
        except (OSError, ValueError) as e:
            event(f"disconnected ({e})")
            dev.close()
            dev, last_meter, silent = reopen(), None, 0
            continue
        if p and silent >= 10:
            event(f"replies again after {silent * 0.1:.1f} s")
        silent = 0 if p else silent + 1
        if silent == 10:
            event("no replies for 1 s")
        if p:
            line = f"{'in':<16} 00 {p.hex(' ')}"
            write(raw, line)
            meter = p[:METER_BYTES] if p[0] == MSG_EMPTY else None
            if p[:2] != HEARTBEAT and (meter is None or meter != last_meter):
                write(log, line)
                print(f"{time.time() - t0:9.3f}  {describe(p)}")
            if meter is not None:
                last_meter = meter
        time.sleep(max(0.0, PERIOD - (time.time() - start)))
except KeyboardInterrupt:
    pass
finally:
    if dev:
        dev.close()
    log.write("stopped\n")
    print("stopped")
