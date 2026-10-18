"""The worker (mpc/core/tp_core.c) against the emulator, through torpedo-ctl
(mpc/ctl/ctl.c) built with ASan/UBSan, and once with ThreadSanitizer."""

import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from fakebridge import FakeBridge
from torpedo.fake import A001, CABS, ML_IRS, PRESET_NAMES, SETUP_VALUES, FakeHid
from torpedo.protocol import (EDIT_BUFFER, EMPTY_PARAMS, MSG_NAME, MSG_PARAM, MSG_QUERY, PARAM_BY_NAME, PARAMS,
                              QUERY_SETUP, SETUP)

MPC = Path(__file__).resolve().parent.parent / "mpc"
pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not shutil.which("make") or not shutil.which("cc"),
    reason="needs Linux, make and a C compiler")
P = {p.name: p.index for p in PARAMS}


class CtlError(Exception):
    pass


class Ctl:
    """torpedo-ctl in a subprocess: `cmd` returns a command's lines, `changes` collects the rest."""

    def __init__(self, exe: Path, *args: str):
        self.p = subprocess.Popen([str(exe), "--retry-ms", "100", "--poll-ms", "20", *args],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, bufsize=1)
        self.lines: queue.Queue[str] = queue.Queue()
        self.changes: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            line = line.rstrip("\n")
            if line.startswith("changed "):
                self.changes.append(line.removeprefix("changed "))
            else:
                self.lines.put(line)

    def cmd(self, line: str, timeout: float = 10) -> list[str]:
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()
        out = []
        while True:
            reply = self.lines.get(timeout=timeout)
            if reply in ("ok", "end"):
                return out
            if reply.startswith("error"):
                raise CtlError(reply)
            out.append(reply)

    def state(self) -> tuple[dict[str, int], dict[str, int]]:
        lines = [line.split() for line in self.cmd("state")]
        return ({w[1]: int(w[2]) for w in lines if w[0] == "param"},
                {w[1]: int(w[2]) for w in lines if w[0] == "setup"})

    def wait_change(self, text: str, timeout: float = 5):
        end = time.monotonic() + timeout
        while text not in self.changes:
            if time.monotonic() > end:
                raise AssertionError(f"no '{text}' in {self.changes}")
            time.sleep(0.01)

    def settle(self):
        """Let the watcher print what the last operation changed, then forget it."""
        self.cmd("sync")
        self.cmd("sleep 50")
        self.changes.clear()

    def close(self):
        self.p.stdin.write("quit\n")
        self.p.stdin.flush()
        self.p.wait(timeout=10)
        err = self.p.stderr.read()
        assert self.p.returncode == 0 and not err.strip(), err


@pytest.fixture(scope="module")
def exe() -> Path:
    subprocess.run(["make", "-s", "-C", str(MPC), "host"], check=True)
    return MPC / "build" / "ctl"


@pytest.fixture
def bridge():
    with FakeBridge() as b:
        yield b
        assert not b.errors  # nothing unobserved was sent
        assert not b.fake.stores and not b.fake.clears  # nor anything that writes flash


@pytest.fixture
def ctl(exe, bridge):
    c = Ctl(exe, bridge.url)
    c.cmd("connected")
    c.settle()
    yield c
    c.close()


def sent_params(bridge, name: str) -> list[int]:
    return [b[2] for b in bridge.fake.sent if b[0] == MSG_PARAM and b[1] == P[name]]


def test_connect_reads_everything(exe, bridge):
    c = Ctl(exe, bridge.url)
    try:
        c.cmd("connected")
        params, setup = c.state()
        assert params == {p.name: A001[p.index] for p in PARAMS}
        assert setup == dict(zip(SETUP, SETUP_VALUES))
        assert c.cmd("state")[0] == "connected 1 generation 1 serial 032C4B94 firmware 4.23"
        cabs = c.cmd("cabs")
        assert len(cabs) == 32
        name, maker, mics = cabs[0x10].split(" ", 2)[2].split("|")
        assert (name, maker, mics.split(",")) == CABS[0x10]
        assert [line.split(" ", 2)[2] for line in c.cmd("irs")] == ML_IRS
        names = c.cmd("names")
        assert len(names) == 129
        assert names[0] == f"name 0 1 {PRESET_NAMES[0]}"
        assert names[-1] == f"name {EDIT_BUFFER} 1 {PRESET_NAMES[0]}"
        c.wait_change("connection 1")
        c.wait_change("cabs")
    finally:
        c.close()


def test_write_reports_linked_params_not_its_own(ctl, bridge):
    """A001 is in arcade mode: CONTOUR recomputes EQ_L_GAIN0/4, and the preset becomes the edit buffer."""
    ctl.cmd("set ARCADE_CONTOUR 30")
    ctl.cmd("sync")
    assert bridge.fake.params[P["ARCADE_CONTOUR"]] == 30
    for change in ("param EQ_L_GAIN0 30", "param EQ_L_GAIN4 30", f"setup SETUP_PRESET {EDIT_BUFFER}"):
        ctl.wait_change(change)
    assert not any("ARCADE_CONTOUR" in c for c in ctl.changes)
    params, setup = ctl.state()
    assert params["ARCADE_CONTOUR"] == 30 and setup["SETUP_PRESET_SOURCE"] == 0


def test_refused_up_front(ctl, bridge):
    with pytest.raises(CtlError, match="not permitted"):  # EPERM: arcade mode would drop it
        ctl.cmd("set SIMUAMP_GAIN 5")
    with pytest.raises(CtlError, match="Invalid argument"):
        ctl.cmd("set ARCADE_CONTOUR 41")
    with pytest.raises(CtlError, match="Invalid argument"):  # read-only
        ctl.cmd("set SIMUHP_PREVIEW 2")
    with pytest.raises(CtlError, match="Invalid argument"):
        ctl.cmd("setup SETUP_PRESET 3")  # presets are selected with `preset`
    with pytest.raises(CtlError, match="Invalid argument"):
        ctl.cmd("cab 32")
    assert all(b[0] != MSG_PARAM for b in bridge.fake.sent)


def test_order_kept_across_a_mode_change(ctl, bridge):
    """Leaving arcade mode first makes the simulation parameter writable, and it is sent after."""
    ctl.cmd("set PRESET_MODE 0")
    ctl.cmd("set SIMUAMP_GAIN 5")
    ctl.cmd("sync")
    assert bridge.fake.params[P["PRESET_MODE"]] == 0 and bridge.fake.params[P["SIMUAMP_GAIN"]] == 5


def test_rewrite_moves_to_the_end(ctl, bridge):
    """A value written again after a preset change lands after it, not in its old queue position."""
    with bridge.lock:  # the worker blocks on the first write, so the next three queue up together
        ctl.cmd("set ARCADE_DISTANCE 3")
        ctl.cmd("set ARCADE_TONE 5")
        ctl.cmd("preset 1")
        ctl.cmd("set ARCADE_TONE 7")
    ctl.cmd("sync")
    assert bridge.fake.setup[3] == EDIT_BUFFER and bridge.fake.setup[4] == 1
    assert bridge.fake.params[P["ARCADE_TONE"]] == 7
    assert sent_params(bridge, "ARCADE_TONE") == [7]  # 5 was replaced before its turn


def test_sweep_is_coalesced_and_not_echoed(exe):
    """Echoes that lag behind (lag=3) never move the visible value back."""
    with FakeBridge(FakeHid(lag=3)) as bridge:
        c = Ctl(exe, bridge.url)
        try:
            c.cmd("connected")
            c.cmd("set PRESET_MODE 0")
            c.settle()
            c.cmd("sweep SIMUAMP_GAIN 0 30")
            c.cmd("sync")
            assert bridge.fake.params[P["SIMUAMP_GAIN"]] == 30
            assert c.state()[0]["SIMUAMP_GAIN"] == 30
            sent = sent_params(bridge, "SIMUAMP_GAIN")
            assert sent[-1] == 30 and len(sent) < 31
            c.cmd("sleep 100")
            assert not any("SIMUAMP_GAIN" in ch for ch in c.changes)
        finally:
            c.close()
        assert not bridge.errors


def test_front_panel_changes(ctl, bridge):
    with bridge.lock:
        bridge.fake.panel_param(P["ARCADE_CONTOUR"], 25)
    ctl.wait_change("param ARCADE_CONTOUR 25")
    ctl.wait_change("param EQ_L_GAIN0 25")
    with bridge.lock:
        bridge.fake.panel_level(0)
        bridge.fake.panel_signal(19, 39, 1)
    ctl.wait_change("setup SETUP_LEVEL 0")
    ctl.wait_change("setup SETUP_MUTE 1")
    assert ctl.cmd("meters") == ["meters in 19 out 39 clip 1"]


def test_panel_back_to_kept_edit_reads_the_source(ctl, bridge):
    ctl.cmd("set ARCADE_CONTOUR 30")  # edit A001
    ctl.settle()
    with bridge.lock:
        bridge.fake.panel_select(1)  # the unit keeps the edit ("A001*")
    ctl.wait_change("setup SETUP_PRESET 1")
    queries = sum(b[:2] == bytes([MSG_QUERY, QUERY_SETUP]) for b in bridge.fake.sent)
    with bridge.lock:
        bridge.fake.panel_select_kept()  # `0c 03 80` without the source
    ctl.wait_change(f"setup SETUP_PRESET {EDIT_BUFFER}")
    ctl.cmd("sleep 100")
    assert sum(b[:2] == bytes([MSG_QUERY, QUERY_SETUP]) for b in bridge.fake.sent) == queries + 1
    assert ctl.state()[1]["SETUP_PRESET_SOURCE"] == 0


def test_preset_select(ctl, bridge):
    ctl.cmd("preset 1")
    ctl.cmd("sync")
    params, setup = ctl.state()
    assert params == {p.name: bridge.fake.presets[1][p.index] for p in PARAMS}
    assert setup["SETUP_PRESET"] == 1
    ctl.wait_change("param ARCADE_CONTOUR 30")  # differs from A001


def test_cab_keeps_the_mic_name(ctl, bridge):
    """A001: LuxTweeDe (16). Mic A on "Dyn 421" (3) is index 4 on Brit 65C (1)."""
    ctl.cmd("set SIMUHP_MIC 3")
    ctl.cmd("cab 1")
    ctl.cmd("sync")
    assert bridge.fake.params[P["SIMUHP_SPEAKER"]] == 1
    assert bridge.fake.params[P["SIMUHP_MIC"]] == 4
    assert sent_params(bridge, "SIMUHP_MIC_2") == []  # arcade mode: mic B left alone
    ctl.wait_change("param SIMUHP_MIC 4")


def test_ir_select(ctl, bridge):
    with pytest.raises(CtlError, match="not permitted"):
        ctl.cmd("ir 1 85")  # arcade mode
    ctl.cmd("set PRESET_MODE 2")
    ctl.cmd("ir 1 85")
    ctl.cmd("sync")
    assert bridge.fake.params[P["SIMUHP_WAVE1"]] == 85
    with pytest.raises(CtlError, match="Invalid argument"):
        ctl.cmd("ir 0 86")  # the bank has 86 IRs


def stored(ctl, timeout: float = 10) -> dict[str, int]:
    """Wait for the store in progress to end: the `storing` line as a dict."""
    end = time.monotonic() + timeout
    while True:
        w = ctl.cmd("storing")[0].split()
        state = {w[i]: int(w[i + 1]) for i in range(0, len(w), 2)}
        if state["storing"] < 0:
            return state
        assert time.monotonic() < end, "the store never ended"
        time.sleep(0.01)


def test_store_writes_the_edit_into_a_slot(ctl, bridge):
    ctl.cmd("set ARCADE_CONTOUR 30")  # edit A001
    ctl.settle()
    ctl.cmd("store 80")
    assert stored(ctl) == {"storing": -1, "clearing": 0, "status": 1, "slot": 80, "cleared": 0, "seq": 1}
    assert bridge.fake.stores == [80]
    assert bridge.fake.presets[80][P["ARCADE_CONTOUR"]] == 30
    assert ctl.cmd("names")[80] == f"name 80 1 {PRESET_NAMES[0]}"  # the buffer's name; the names were read back
    assert ctl.state()[1]["SETUP_PRESET"] == ctl.state()[1]["SETUP_PRESET_SOURCE"] == 80
    ctl.wait_change("names")
    bridge.fake.stores.clear()  # the fixture insists that nothing else writes flash


def test_store_is_one_at_a_time_and_after_what_was_queued(ctl, bridge):
    with bridge.lock:  # the unit isn't answering yet
        ctl.cmd("set ARCADE_CONTOUR 30")
        ctl.cmd("store 80")
        with pytest.raises(CtlError, match="busy"):
            ctl.cmd("store 81")
        assert ctl.cmd("storing")[0].startswith("storing 80 ")
    assert stored(ctl)["status"] == 1
    assert bridge.fake.stores == [80]
    assert bridge.fake.presets[80][P["ARCADE_CONTOUR"]] == 30  # the edit queued before it was stored with it
    bridge.fake.stores.clear()


def renames(bridge) -> list[str]:
    return [b[2:].split(bytes(1))[0].decode() for b in bridge.fake.sent if b[:2] == bytes([MSG_NAME, EDIT_BUFFER])]


def test_store_under_a_new_name_renames_the_buffer_first(ctl, bridge):
    ctl.cmd("store 80 MY NEW SOUND 2")
    assert stored(ctl)["status"] == 1
    assert renames(bridge) == ["MY NEW SOUND 2"]
    assert bridge.fake.stores == [80] and bridge.fake.names[80] == "MY NEW SOUND 2"
    assert ctl.cmd("names")[80] == "name 80 1 MY NEW SOUND 2"
    ctl.cmd("store 81 MY NEW SOUND 2")  # the name it already has: the unit wouldn't answer a rename
    assert stored(ctl)["status"] == 1
    assert renames(bridge) == ["MY NEW SOUND 2"] and bridge.fake.names[81] == "MY NEW SOUND 2"
    bridge.fake.stores.clear()


def test_a_load_reads_the_buffer_name(ctl, bridge):
    """Loading a preset renames the edit buffer without a word, from here or the panel, and so does going back to
    the kept edit: the name is read after each."""
    def buffer_name() -> str:
        return ctl.cmd("names")[-1].split(" ", 3)[3]
    ctl.cmd("preset 1")
    ctl.cmd("sync")
    assert buffer_name() == PRESET_NAMES[1]
    ctl.cmd("preset 0")
    ctl.cmd("set ARCADE_CONTOUR 30")  # edit A001
    ctl.settle()
    with bridge.lock:
        bridge.fake.panel_select(2)
    ctl.wait_change("setup SETUP_PRESET 2")
    assert buffer_name() == PRESET_NAMES[2]
    with bridge.lock:
        bridge.fake.panel_select_kept()  # back to "A001*"
    ctl.wait_change(f"setup SETUP_PRESET {EDIT_BUFFER}")
    assert buffer_name() == PRESET_NAMES[0]


def test_a_store_after_a_load_renames_from_the_loaded_name(ctl, bridge):
    """The rename is skipped when the buffer has that name already: the loaded preset's, not the one before."""
    ctl.cmd("preset 1")
    ctl.cmd(f"store 80 {PRESET_NAMES[0]}")  # A001's name: a rename
    assert stored(ctl)["status"] == 1
    ctl.cmd("preset 2")
    ctl.cmd(f"store 81 {PRESET_NAMES[2]}")  # the name it has: none (the unit wouldn't answer)
    assert stored(ctl)["status"] == 1
    assert renames(bridge) == [PRESET_NAMES[0]]
    assert bridge.fake.stores == [80, 81] and bridge.fake.names[80:82] == [PRESET_NAMES[0], PRESET_NAMES[2]]
    bridge.fake.stores.clear()


def test_a_rewrite_after_a_store_lands_after_it(ctl, bridge):
    """What was queued when the store was asked for is stored, even if written again before the store's turn."""
    with bridge.lock:  # the worker blocks on the first write, so the rest queue up behind it
        ctl.cmd("set ARCADE_DISTANCE 3")
        ctl.cmd("set ARCADE_CONTOUR 30")
        ctl.cmd("store 80")
        ctl.cmd("set ARCADE_CONTOUR 31")
    assert stored(ctl)["status"] == 1
    ctl.cmd("sync")
    assert bridge.fake.presets[80][P["ARCADE_CONTOUR"]] == 30 and bridge.fake.params[P["ARCADE_CONTOUR"]] == 31
    assert sent_params(bridge, "ARCADE_CONTOUR") == [30, 31]
    bridge.fake.stores.clear()


def test_clear_empties_another_preset(ctl, bridge):
    ctl.cmd("clear 3")
    assert stored(ctl) == {"storing": -1, "clearing": 0, "status": 1, "slot": 3, "cleared": 1, "seq": 1}
    assert bridge.fake.clears == [3] and bridge.fake.presets[3] == EMPTY_PARAMS
    assert [b[:3] for b in bridge.fake.sent if b[0] == MSG_QUERY][-3:] == [bytes([4, 0x69, 3]), bytes([4, 0x0E, 0]),
                                                                           bytes([4, 9, 0x81])]
    assert ctl.cmd("names")[3] == "name 3 0 USER"  # simulation, like every empty slot
    bridge.fake.clears.clear()  # the fixture insists that nothing else writes flash


def test_clearing_the_current_preset_reads_its_parameters_again(ctl, bridge):
    """The unit only renames the edit buffer: the parameters it now holds come from reading them."""
    ctl.cmd("clear 0")  # A001, unedited
    assert stored(ctl)["status"] == 1
    params = ctl.state()[0]
    assert [params[p.name] for p in PARAMS] == EMPTY_PARAMS
    bridge.fake.clears.clear()


def test_a_clear_behind_an_edit_of_its_preset_is_not_sent(ctl, bridge):
    with bridge.lock:  # the unit isn't answering yet: A001 isn't edited when the clear is asked for
        ctl.cmd("set ARCADE_CONTOUR 30")
        ctl.cmd("clear 0")
    assert stored(ctl) == {"storing": -1, "clearing": 0, "status": 2, "slot": 0, "cleared": 1, "seq": 1}
    assert not bridge.fake.clears and bridge.fake.names[0] == PRESET_NAMES[0]


def test_clear_refuses_the_preset_with_unsaved_edits_and_waits_its_turn(ctl, bridge):
    ctl.cmd("set ARCADE_CONTOUR 30")  # edit A001
    ctl.settle()
    with pytest.raises(CtlError, match="not permitted"):
        ctl.cmd("clear 0")
    with bridge.lock:  # the unit isn't answering yet
        ctl.cmd("store 80")
        with pytest.raises(CtlError, match="busy"):
            ctl.cmd("clear 81")
    assert stored(ctl)["status"] == 1
    assert bridge.fake.stores == [80] and not bridge.fake.clears
    bridge.fake.stores.clear()


def test_store_refuses_names_the_unit_does_not_take(ctl, bridge):
    for bad in ("lower case", "TWENTY SIX CHARACTERS LONG", "DASH-ED"):
        with pytest.raises(CtlError, match="Invalid argument"):
            ctl.cmd(f"store 80 {bad}")
    assert not renames(bridge) and not bridge.fake.stores


def test_store_refused_up_front(ctl, bridge):
    with pytest.raises(CtlError, match="Invalid argument"):
        ctl.cmd("store 128")
    with pytest.raises(CtlError, match="Invalid argument"):
        ctl.cmd("store -1")
    assert not bridge.fake.stores


def test_a_store_cut_short_is_reported_as_failed(ctl, bridge):
    ctl.cmd("set ARCADE_CONTOUR 30")
    ctl.settle()
    with bridge.lock:
        ctl.cmd("store 80")
        bridge._conn.shutdown(socket.SHUT_RDWR)  # the cable is pulled before the unit answers
    ctl.wait_change("connection 0")
    ctl.wait_change("connection 1")  # and put back: everything is read again
    assert stored(ctl) == {"storing": -1, "clearing": 0, "status": 2, "slot": 80, "cleared": 0, "seq": 1}  # failed
    assert bridge.fake.stores in ([], [80])  # the unit may have got the command before the cable went
    bridge.fake.stores.clear()


def test_setup_write(ctl, bridge):
    ctl.cmd("setup SETUP_LEVEL 100")
    ctl.cmd("sync")
    assert bridge.fake.setup[0] == 100
    with pytest.raises(CtlError, match="Invalid argument"):
        ctl.cmd("setup SETUP_LEVEL 108")


def test_replug_reads_everything_again(ctl, bridge):
    """A replugged unit sends nothing on its own: the state must be read again."""
    other = FakeHid()
    other.params[P["ARCADE_CONTOUR"]] = 33
    bridge.unplug(replace=other)
    ctl.wait_change("connection 0")
    ctl.wait_change("connection 1")
    assert ctl.cmd("state")[0].startswith("connected 1 generation 2")
    assert ctl.state()[0]["ARCADE_CONTOUR"] == 33
    ctl.cmd("set ARCADE_CONTOUR 34")
    ctl.cmd("sync")
    assert other.params[P["ARCADE_CONTOUR"]] == 34


def test_not_connected(exe, tmp_path):
    c = Ctl(exe, f"unix:{tmp_path}/nothing")
    try:
        with pytest.raises(CtlError, match="not connected"):
            c.cmd("set ARCADE_CONTOUR 30")
        with pytest.raises(CtlError, match="not connected"):
            c.cmd("store 80")
        with pytest.raises(CtlError, match="timed out"):
            c.cmd("connected 200")
    finally:
        c.close()


def test_threads_under_tsan(bridge):
    """Sweeps from another thread while the front panel changes things, with ThreadSanitizer."""
    if subprocess.run(["make", "-s", "-C", str(MPC), "tsan"]).returncode:
        pytest.skip("no ThreadSanitizer")
    c = Ctl(MPC / "build" / "ctl-tsan", bridge.url)
    try:
        c.cmd("connected")
        for v in range(5):
            c.cmd("sweep ARCADE_CONTOUR 0 40")
            with bridge.lock:
                bridge.fake.panel_param(P["ARCADE_TONE"], v)
            c.cmd("sweep ARCADE_CONTOUR 40 0")
        c.cmd("sync")
        c.wait_change("param ARCADE_TONE 4")
        assert bridge.fake.params[P["ARCADE_CONTOUR"]] == 0
    finally:
        c.close()  # TSan reports go to stderr, which close() requires empty
