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
from torpedo.fake import PRESET_NAMES, FakeHid
from torpedo.protocol import EDIT_BUFFER, MSG_PARAM, PARAM_BY_NAME

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
    assert host.text("arcade_poweramp") == "off"
    programs = host.cmd("programs")
    assert programs[0] == "program 0 A001 BDELUXE 1 RBN" and programs[127] == "program 127 S128 USER"  # empty: simulation
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


def test_other_modes_drop_what_their_pages_dont_show(host, bridge):
    """A tab's Q-Links stay on its controls whatever the mode, and simulation and IR mode would take anything."""
    host.set("preset_mode", 2)
    host.settle()
    host.wait_view("IR")
    sent = len(bridge.fake.sent)
    for key in ("simuhp_distance", "simuhp_speaker", "arcade_tone"):
        host.set(key, 3)
    host.cmd(f"set {INDEX['simuhp_mic_next']} 1")
    host.settle()
    assert all(b[0] != MSG_PARAM for b in bridge.fake.sent[sent:])
    assert host.get("simuhp_distance")[0] == 0
    host.set("simuhp_level", 80)  # IR mode's own
    host.settle()
    assert bridge.fake.params[P["SIMUHP_LEVEL"]] == 80
    host.set("preset_mode", 0)
    host.settle()
    host.wait_view("SIMULATION")
    host.set("arcade_tone", 3)
    host.settle()
    assert bridge.fake.params[P["ARCADE_TONE"]] == 38


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


# --- the PRESET page ---------------------------------------------------------------------------------------------

def press(host, key: str):
    """A tap. A tile is a Toggle Switch on MPC, so a lit one goes to 0; an action button goes to 1."""
    value = 1 - host.get(key)[0] if key.startswith("slot_") else 1
    host.cmd(f"set {INDEX[key]} {value:g}")
    host.settle()


def wait_text(host, key: str, text: str, timeout: float = 5):
    end = time.monotonic() + timeout
    while host.text(key) != text:
        assert time.monotonic() < end, f"{key} is {host.text(key)!r}, not {text!r}"
        host.cmd("sleep 20")
        host.cmd("run 40")


def message(host) -> tuple[str, str]:
    return host.text("pmsg"), host.text("pmsg2")


def edit(host):
    host.set("arcade_contour", 30)  # A001 is in arcade mode
    host.settle()
    assert host.text("pui") == "EDITED"


def test_tiles_show_a_bank_and_follow_the_preset(host, bridge):
    assert host.text("bank") == "001-016" and host.text("pui") == "CLEAN"
    assert [host.text(f"slot_{n}") for n in (1, 2)] == ["A001 BDELUXE 1 RBN", "A002 BDELUXE 1 DYN421"]
    assert [host.get(f"slot_{n}")[0] for n in (1, 2)] == [1, 0]  # lit: the current preset
    host.set("preset", 20)
    host.settle()
    assert host.text("bank") == "017-032"  # the tiles went to its bank
    assert host.text("slot_4") == f"A020 {PRESET_NAMES[19]}" and host.get("slot_4")[0] == 1
    host.set("bank", 4)  # browsing loads nothing
    host.settle()
    assert host.text("slot_12") == "S076 USER" and bridge.fake.setup[3] == 19


def test_a_tap_loads_a_preset(host, bridge):
    press(host, "slot_2")
    assert bridge.fake.setup[3] == 1
    assert [host.get(f"slot_{n}")[0] for n in (1, 2)] == [0, 1]


def test_a_tap_over_edits_asks_first(host, bridge):
    edit(host)
    press(host, "slot_2")
    assert host.text("pui") == "CONFIRM" and host.get("slot_2")[0] == 1
    assert message(host) == ("LOAD A002?", "THE EDITS TO A001 ARE DROPPED")
    assert bridge.fake.setup[3] == EDIT_BUFFER  # nothing loaded yet
    press(host, "p_confirm")
    assert bridge.fake.setup[3] == 1 and host.text("pui") == "CLEAN"


def test_save_and_discard_need_edits(host, bridge):
    press(host, "p_save")
    press(host, "p_discard")
    assert host.text("pui") == "CLEAN"
    assert message(host) == ("TAP A PRESET TO LOAD IT", "SAVE AS COPIES IT TO ANOTHER ONE")


def test_save_stores_the_edits_into_their_preset_once_confirmed(host, bridge):
    edit(host)
    assert message(host) == ("A001 HAS UNSAVED EDITS", "SAVE, SAVE AS OR DISCARD THEM")
    press(host, "p_save")
    assert message(host) == ("SAVE THE EDITS INTO A001?", "AS BDELUXE 1 RBN")
    assert not bridge.fake.stores
    press(host, "p_confirm")
    wait_text(host, "pmsg", "SAVED A001")
    assert host.text("pmsg2") == "BDELUXE 1 RBN"
    assert bridge.fake.stores == [0] and bridge.fake.presets[0][P["ARCADE_CONTOUR"]] == 30
    assert host.text("pui") == "CLEAN" and host.text("preset") == "A001 BDELUXE 1 RBN"
    bridge.fake.stores.clear()  # the fixture checks that nothing else wrote flash


def test_save_as_lights_the_empty_presets_and_stores_into_the_one_tapped(host, bridge):
    press(host, "p_saveas")
    assert host.text("pui") == "PICK" and host.get("slot_1")[0] == 0
    assert message(host) == ("SAVE INTO WHICH PRESET?", "TAP IT (THE LIT ONES ARE EMPTY)")
    host.set("bank", 4)
    host.settle()
    assert [host.get(f"slot_{n}")[0] for n in (11, 12)] == [0, 1]  # A075 holds a preset, A076 is empty
    press(host, "slot_12")
    assert message(host) == ("SAVE INTO S076 (EMPTY)?", "AS BDELUXE 1 RBN")
    assert not bridge.fake.stores
    press(host, "p_confirm")
    wait_text(host, "pmsg", "SAVED A076")
    assert bridge.fake.stores == [75]
    assert host.text("slot_12") == "A076 BDELUXE 1 RBN" and host.get("slot_12")[0] == 1  # now the current one
    bridge.fake.stores.clear()


def test_replacing_a_preset_says_what_it_holds_and_can_be_cancelled(host, bridge):
    press(host, "p_saveas")
    press(host, "slot_2")
    assert message(host) == ("REPLACE A002 BDELUXE 1 DYN421?", "AS BDELUXE 1 RBN")
    press(host, "p_cancel")
    assert host.text("pui") == "CLEAN" and host.get("slot_1")[0] == 1
    press(host, "p_confirm")  # nothing left to confirm
    assert not bridge.fake.stores


def test_discard_reloads_the_preset(host, bridge):
    stored = bridge.fake.presets[0][P["ARCADE_CONTOUR"]]
    edit(host)
    press(host, "p_discard")
    assert message(host) == ("DISCARD THE EDITS?", "A001 RELOADS AS STORED")
    press(host, "p_confirm")
    assert bridge.fake.setup[3] == 0 and bridge.fake.params[P["ARCADE_CONTOUR"]] == stored
    assert host.text("pui") == "CLEAN"


def test_a_question_goes_away_when_the_unit_changes_under_it(host, bridge):
    edit(host)
    press(host, "p_discard")
    with bridge.lock:
        bridge.fake.panel_select(2)  # someone turned the preset knob meanwhile
    host.settle()
    assert host.text("pui") == "CLEAN"
    assert message(host) == ("CANCELLED", "THE UNIT CHANGED MEANWHILE")
    press(host, "p_confirm")
    assert bridge.fake.setup[3] == 2



def type_name(host, text: str):
    for c in text:
        host.cmd(f"set {INDEX['k_space' if c == ' ' else 'k_' + c.lower()]} 1")
    host.settle()


def test_rename_types_a_name_then_asks_before_storing(host, bridge):
    press(host, "p_rename")
    assert host.text("pui") == "NAMING"
    assert (*message(host), host.text("pname")) == ("NAME FOR A001", "UP TO 25 LETTERS, DIGITS, SPACES",
                                                    "BDELUXE 1 RBN_")
    press(host, "k_clear")
    type_name(host, "MY TONX")
    press(host, "k_del")
    type_name(host, "E")
    assert host.text("pname") == "MY TONE_"
    press(host, "p_confirm")  # OK
    assert host.text("pui") == "ASKSAVE" and message(host) == ("RENAME A001?", "TO MY TONE")
    assert not bridge.fake.stores
    press(host, "p_confirm")
    wait_text(host, "pmsg", "SAVED A001")
    assert bridge.fake.stores == [0] and bridge.fake.names[0] == "MY TONE"
    assert host.text("slot_1") == "A001 MY TONE" and host.text("preset") == "A001 MY TONE"
    bridge.fake.stores.clear()


def test_a_save_question_can_change_the_name(host, bridge):
    press(host, "p_saveas")
    host.set("bank", 4)
    host.settle()
    press(host, "slot_12")
    press(host, "p_name")
    assert host.text("pname") == "BDELUXE 1 RBN_"  # the name it would have been saved under
    type_name(host, " 2")
    press(host, "p_confirm")  # OK: back to the question
    assert message(host) == ("SAVE INTO S076 (EMPTY)?", "AS BDELUXE 1 RBN 2")
    press(host, "p_name")
    type_name(host, "X")
    press(host, "p_cancel")  # back to the question, with the name it had
    assert message(host) == ("SAVE INTO S076 (EMPTY)?", "AS BDELUXE 1 RBN 2")
    press(host, "p_confirm")
    wait_text(host, "pmsg", "SAVED A076")
    assert bridge.fake.stores == [75] and bridge.fake.names[75] == "BDELUXE 1 RBN 2"
    assert bridge.fake.names[0] == "BDELUXE 1 RBN"
    bridge.fake.stores.clear()


def test_the_keyboard_keeps_to_what_a_name_may_be(host, bridge):
    press(host, "p_rename")
    press(host, "k_clear")
    type_name(host, "  A  B")  # no leading or double spaces
    assert host.text("pname") == "A B_"
    type_name(host, "C" * 30)
    assert host.text("pname") == "A B" + "C" * 22  # 25 at most, no cursor once full
    assert host.text("pmsg2") == "25 CHARACTERS: AS LONG AS IT GETS"
    press(host, "k_clear")
    press(host, "p_confirm")  # OK with no name: nothing
    assert host.text("pui") == "NAMING"
    press(host, "p_cancel")  # a rename's keyboard: back to the page
    assert host.text("pui") == "CLEAN" and not bridge.fake.stores


def test_rename_needs_no_unsaved_edits(host, bridge):
    edit(host)
    press(host, "p_rename")  # it would store the edits too: SAVE, then NAME..., does that
    assert host.text("pui") == "EDITED"


def test_skin_post_leaves_one_toggle_per_tap(tmp_path):
    """A trigger toggles by itself when a tap is released: an extra Mouse Down toggle fired it twice. A list row's
    name label covers its Button, so its Mouse Down toggle is what a tap reaches."""
    sys.path.insert(0, str(MPC / "plugin"))
    import skin_post

    def acts(*pairs):
        return [{"onAction": a, "handler": h, "handleName": "Data"} for a, h in pairs]
    tap, enter, qlink = ("Mouse Down", "Toggle Switch"), ("Enter Pressed", "Toggle Switch"), ("Mouse Down", "Q-Link")
    defs = [{"key": "shTrig_k_q_Q", "value": {"actions": acts(tap, enter)}},
            {"key": "shRow_392x46", "value": {"actions": acts(tap, enter)}},
            {"key": "shToggle_ns0", "value": {"actions": acts(qlink, enter)}},
            {"key": "shPopField_150x64", "value": {"actions": acts(tap, enter)}}]  # no Button: it toggles once
    (tmp_path / "Plugin Skins").mkdir()
    tui = tmp_path / "Plugin Skins" / "TUI.json"
    tui.write_text(json.dumps({"localComponentDefinitions": defs}), encoding="utf-8")
    assert skin_post.main(str(tmp_path)) == 0
    after = {d["key"]: [(a["onAction"], a["handler"]) for a in d["value"]["actions"]]
             for d in json.loads(tui.read_text(encoding="utf-8"))["localComponentDefinitions"]}
    assert after == {"shTrig_k_q_Q": [enter], "shRow_392x46": [tap, enter], "shToggle_ns0": [qlink, enter],
                     "shPopField_150x64": [tap, enter]}
    assert skin_post.main(str(tmp_path)) == 1  # nothing left to fix: the builder's keys changed, say so


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
    assert host.text("preset") == "I001* BDELUXE 1 RBN"  # the sound's mode; the tile keeps the stored one
    assert host.text("slot_1") == "A001 BDELUXE 1 RBN"
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
