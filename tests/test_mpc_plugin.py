"""The MPC plugin (mpc/plugin: engine + mpc-vst-plugins' wrapper) in a stand-in host (mpc/plugin/test_host.c,
ASan/UBSan), against the emulator through tests/fakebridge.py."""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from fakebridge import FakeBridge
from torpedo.fake import FakeHid
from torpedo.protocol import MSG_PARAM, PARAM_BY_NAME

MPC = Path(__file__).resolve().parent.parent / "mpc"
pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not shutil.which("make") or not shutil.which("cc")
    or not (MPC / "mpc-vst-plugins" / "wrapper" / "vst2_wrap.c").exists(),
    reason="needs Linux, make, a C compiler and the mpc-vst-plugins submodule")
PARAMS = json.loads((MPC / "plugin" / "params.json").read_text(encoding="utf-8"))["params"]
INDEX = {p["key"]: i for i, p in enumerate(PARAMS)}
P = {name: p.index for name, p in PARAM_BY_NAME.items()}


def norm(key: str, value: int) -> float:
    p = PARAMS[INDEX[key]]
    if "options" in p:
        return value / (len(p["options"]) - 1)
    return (value - p["min"]) / (p["max"] - p["min"])


def test_generated_up_to_date():
    subprocess.run([sys.executable, str(MPC / "plugin" / "gen_plugin.py"), "--check"], check=True)


class Host:
    """mpc/build/plugin-host: `cmd` returns a command's lines; automate/update callbacks are collected."""

    def __init__(self, device: str):
        self.p = subprocess.Popen([str(MPC / "build" / "plugin-host")], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                                  env={**os.environ, "TORPEDO_DEVICE": device})
        self.lines: queue.Queue[str] = queue.Queue()
        self.automated: list[tuple[int, float]] = []
        self.updates = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            w = line.split()
            if w and w[0] == "automate":
                self.automated.append((int(w[1]), float(w[2])))
            elif w and w[0] == "update":
                self.updates += 1
            else:
                self.lines.put(line.rstrip("\n"))

    def cmd(self, line: str) -> list[str]:
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()
        out = []
        while (reply := self.lines.get(timeout=20)) not in ("ok", "end"):
            assert not reply.startswith("error"), reply
            out.append(reply)
        return out

    def get(self, key: str) -> tuple[float, str]:
        w = self.cmd(f"get {INDEX[key]}")[0].split(" ", 3)
        return float(w[2]), w[3] if len(w) > 3 else ""

    def text(self, key: str) -> str:
        return self.get(key)[1]

    def set(self, key: str, value: int):
        self.cmd(f"set {INDEX[key]} {norm(key, value)}")

    def settle(self):
        """Let the core send and hear back, then run enough blocks for the wrapper's 100 ms poll."""
        self.cmd("sleep 150")
        self.cmd("run 40")

    def wait_view(self, view: str, timeout: float = 5):
        end = time.monotonic() + timeout
        while self.text("view") != view:
            assert time.monotonic() < end, f"view is {self.text('view')}, not {view}"
            self.cmd("sleep 20")

    def close(self):
        self.p.stdin.write("quit\n")
        self.p.stdin.flush()
        self.p.wait(timeout=10)
        err = self.p.stderr.read()
        assert self.p.returncode == 0 and not err.strip(), err


@pytest.fixture(scope="module")
def built():
    subprocess.run(["make", "-s", "-C", str(MPC), "plugin-host"], check=True)


@pytest.fixture
def bridge():
    with FakeBridge() as b:
        yield b
        assert not b.errors
        assert not b.fake.stores and not b.fake.clears


@pytest.fixture
def host(built, bridge):
    h = Host(bridge.url)
    h.wait_view("ARCADE")  # A001 is in arcade mode
    h.settle()
    h.automated.clear()
    yield h
    h.close()


def test_offline(built, tmp_path):
    h = Host(f"unix:{tmp_path}/nothing")
    try:
        names = [line.split(" ", 2)[2] for line in h.cmd("params")]
        assert names[:len(PARAMS)] == [p["name"] for p in PARAMS]  # then the popups' hidden "open" flags
        assert h.text("view") == "OFFLINE"
        h.set("arcade_contour", 30)  # dropped: not connected
        h.cmd("run 10")  # and silent (test_host checks)
        assert h.cmd("chunk") == ["chunk 0"]  # nothing saved with a project
    finally:
        h.close()


def test_shows_the_unit(host):
    assert host.text("preset") == "A001 BDELUXE 1 RBN"
    assert host.text("simuhp_speaker") == "LuxTweeDe"
    assert host.text("simuhp_mic") == "Dyn 7b"
    assert host.text("preset_level") == "-8 dB"
    assert host.text("arcade_contour") == "52.5%"
    assert host.text("eq_l_freq_f0") == "120 Hz"  # guitar EQ: fixed band
    assert host.text("eq_l_freq_low") == "-"
    assert host.text("simuamp_modele") == "PP EL34"
    programs = host.cmd("programs")
    assert programs[0] == "program 0 A001 BDELUXE 1 RBN" and programs[127] == "program 127 A128 USER"
    assert programs[-1] == "current 0"


def test_write_reaches_the_unit_and_linked_values_come_back(host, bridge):
    host.set("arcade_contour", 30)
    host.settle()
    assert bridge.fake.params[P["ARCADE_CONTOUR"]] == 30
    moved = {i for i, _ in host.automated}
    assert {INDEX["eq_l_gain0"], INDEX["eq_l_gain4"]} <= moved  # recomputed by the unit
    assert host.text("preset") == "A001* BDELUXE 1 RBN"  # edited
    assert host.updates


def test_arcade_mode_refuses_simulation_params(host, bridge):
    host.set("simuamp_gain", 12)
    host.settle()
    assert all(b[0] != MSG_PARAM for b in bridge.fake.sent)
    assert host.get("simuamp_gain")[0] == 0


def test_front_panel_follows(host, bridge):
    with bridge.lock:
        bridge.fake.panel_param(P["ARCADE_TONE"], 40)
    host.settle()
    assert (INDEX["arcade_tone"], pytest.approx(norm("arcade_tone", 40))) in host.automated
    with bridge.lock:
        bridge.fake.panel_select(1)
    host.settle()
    assert host.text("preset") == "A002 BDELUXE 1 DYN421"


def test_program_selects_a_preset(host, bridge):
    host.cmd("program 1")
    host.settle()
    assert bridge.fake.setup[3] == 1
    assert host.cmd("programs")[-1] == "current 1"


def test_choosers_step_through_the_units_lists(host, bridge):
    host.cmd(f"set {INDEX['simuhp_speaker_next']} 1")
    host.settle()
    assert bridge.fake.params[P["SIMUHP_SPEAKER"]] == 17
    host.set("simuhp_speaker", 63)  # past the 32 cabs: the last one
    host.settle()
    assert bridge.fake.params[P["SIMUHP_SPEAKER"]] == 31


def test_mode_pages_and_ir_names(host, bridge):
    host.set("preset_mode", 2)
    host.settle()
    host.wait_view("IR")
    assert host.text("simuhp_wave0") == "ML-FRMN-00"
    host.set("simuhp_wave1", 85)
    host.settle()
    assert bridge.fake.params[P["SIMUHP_WAVE1"]] == 85
    assert host.text("simuhp_wave1") == "ML-FRMN-V30-906-E"
    host.set("eq_mode", 2)  # custom: the band frequencies are the knobs' own
    host.settle()
    assert host.text("eq_l_freq_f0") == "57"


def test_instances_share_one_connection(host, bridge):
    host.cmd("open")
    assert host.text("view") == "ARCADE"  # the second instance sees the unit at once
    host.cmd("close")
    host.set("arcade_tone", 10)
    host.settle()
    assert bridge.fake.params[P["ARCADE_TONE"]] == 10
    assert bridge.connections == 1


def test_replug_reads_the_unit_again(host, bridge):
    """A replugged unit sends nothing by itself: what it holds now must be read again."""
    other = FakeHid()
    other.setup[3] = other.setup[4] = 1
    other.params = list(other.presets[1])
    bridge.unplug(replace=other)
    end = time.monotonic() + 5
    while host.text("preset") != "A002 BDELUXE 1 DYN421":
        assert time.monotonic() < end, host.text("preset")
        host.cmd("sleep 20")
    assert host.text("arcade_contour") == "75%"
