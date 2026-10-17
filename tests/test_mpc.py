"""The C side (mpc/) against the emulator: built for this machine with ASan and
UBSan (`make -C mpc host`), talking to tests/fakebridge.py."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fakebridge import FakeBridge
from torpedo.fake import A001, SETUP_VALUES
from torpedo.protocol import MSG_POLL, MSG_QUERY, PARAM_BY_NAME, PARAMS, QUERY_INFO, QUERY_PARAMS, QUERY_SETUP, SETUP

MPC = Path(__file__).resolve().parent.parent / "mpc"
needs_cc = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not shutil.which("make") or not shutil.which("cc"),
    reason="needs Linux, make and a C compiler")


def test_tables_up_to_date():
    subprocess.run([sys.executable, str(MPC / "gen_tables.py"), "--check"], check=True)


@pytest.fixture(scope="module")
def probe() -> Path:
    subprocess.run(["make", "-s", "-C", str(MPC), "host"], check=True)
    return MPC / "build" / "probe"


@pytest.fixture
def bridge():
    with FakeBridge() as b:
        yield b


def run(probe: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(probe), *args], capture_output=True, text=True, timeout=30)


def values(out: str, kind: str) -> dict[str, int]:
    return {w[1]: int(w[2]) for w in map(str.split, out.splitlines()) if w[0] == kind}


@needs_cc
def test_probe_reads_info_setup_and_params(probe, bridge):
    r = run(probe, bridge.url)
    assert r.returncode == 0, r.stderr
    assert "serial 032C4B94" in r.stdout and "firmware 4.23" in r.stdout
    assert values(r.stdout, "param") == {p.name: A001[p.index] for p in PARAMS}
    assert values(r.stdout, "setup") == dict(zip(SETUP, SETUP_VALUES))
    assert not bridge.errors
    sent = {b[:2] if b[0] == MSG_QUERY else b[:1] for b in bridge.fake.sent}
    assert sent == {bytes([MSG_POLL]), bytes([MSG_QUERY, QUERY_INFO]),
                    bytes([MSG_QUERY, QUERY_PARAMS]), bytes([MSG_QUERY, QUERY_SETUP])}


@needs_cc
def test_probe_lag(probe):
    """Replies that only show up after a few empty `05`s are still waited for."""
    from torpedo.fake import FakeHid
    with FakeBridge(FakeHid(lag=3)) as bridge:
        r = run(probe, bridge.url)
        assert r.returncode == 0, r.stderr
        assert values(r.stdout, "param") == {p.name: A001[p.index] for p in PARAMS}


@needs_cc
def test_probe_listen_front_panel_and_meters(probe, bridge):
    p = subprocess.Popen([str(probe), "--listen", "1.5", bridge.url], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    for line in p.stdout:
        if line.startswith("listening"):
            break
    contour = PARAM_BY_NAME["ARCADE_CONTOUR"].index
    with bridge.lock:
        bridge.fake.panel_param(contour, 25)
        bridge.fake.panel_signal(19, 39, 1)
    out, err = p.communicate(timeout=30)
    assert p.returncode == 0, err
    assert "param ARCADE_CONTOUR 25" in out
    assert "meters in 19 out 39 clip 1" in out
    assert not bridge.errors


@needs_cc
def test_probe_unreachable(probe, tmp_path):
    r = run(probe, f"unix:{tmp_path}/nothing")
    assert r.returncode == 1 and "cannot open" in r.stderr


def uevent(root: Path, node: str, hid_id: str, name: str):
    d = root / node / "device"
    d.mkdir(parents=True)
    (d / "uevent").write_text(f"DRIVER=hid-generic\nHID_ID={hid_id}\nHID_NAME={name}\n"
                              "HID_UNIQ=032C4B94\n", encoding="utf-8")


@needs_cc
def test_probe_find(probe, tmp_path):
    uevent(tmp_path, "hidraw0", "0003:0000046D:0000C31C", "Logitech USB Keyboard")
    uevent(tmp_path, "hidraw1", "0003:00000483:0000A334", "Other ST Gadget")  # ST's shared VID
    r = run(probe, "--find", "--sysfs", str(tmp_path))
    assert r.returncode == 1 and "no Torpedo Live" in r.stderr
    uevent(tmp_path, "hidraw2", "0003:00000483:0000A334", "Two Notes Audio Engineering Torpedo Live")
    r = run(probe, "--find", "--sysfs", str(tmp_path))
    assert r.returncode == 0 and r.stdout == "/dev/hidraw2\n"
