"""Attach hidspy.js to TorpedoRemote.exe and log traffic until a STOP file appears.

Repeated identical inbound reports (the ~47/s idle stream) are collapsed into a
count so that the interesting exchanges stand out.
"""
import os
import sys
import time

import frida

here = os.path.dirname(os.path.abspath(__file__))
log_path = sys.argv[1]
stop_path = os.path.join(here, "STOP")
if os.path.exists(stop_path):
    os.remove(stop_path)

log = open(log_path, "w", buffering=1, encoding="utf-8")
raw = open(log_path + ".raw", "w", buffering=1, encoding="utf-8")
IDLE = ("out 00 7e 00 00 00", "in 00 05 00 00 00 00 ", "in 00 04 02 00 00 00 ")
t0 = time.time()
last = {"key": None, "count": 0}


def flush_repeat():
    if last["count"] > 1:
        log.write(f"           (x{last['count']} identical)\n")
    last["count"] = 0


def on_message(msg, _data):
    if msg["type"] != "send":
        log.write(f"!! {msg}\n")
        return
    p = msg["payload"]
    tag = p["dir"] + (" " + p["code"] if "code" in p else "")
    if p["dir"].startswith("ioctl"):
        return
    raw.write(f"{time.time() - t0:9.3f} {tag:<16} {p['data']}\n")
    if (p["dir"] + " " + p["data"]).startswith(IDLE):
        return
    key = (p["dir"], p.get("code"), p["data"])
    if key == last["key"] and p["dir"] == "in":
        last["count"] += 1
        return
    flush_repeat()
    last["key"], last["count"] = key, 1
    log.write(f"{time.time() - t0:9.3f} {tag:<16} {p['data']}\n")


session = frida.attach("TorpedoRemote.exe")
script = session.create_script(open(os.path.join(here, "hidspy.js"), encoding="utf-8").read())
script.on("message", on_message)
script.load()
try:
    while not os.path.exists(stop_path):
        time.sleep(0.2)
finally:
    flush_repeat()
    session.detach()
    log.write("detached\n")
