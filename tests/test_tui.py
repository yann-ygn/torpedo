import pytest

from torpedo.device import SPEAKER, Torpedo
from torpedo.fake import A001, FakeHid
from torpedo.layout import CHOICE, LAYOUTS, controls
from torpedo.protocol import (ARCADE, ARCADE_EDITABLE, MSG_PARAM, MSG_POLL, MSG_QUERY, PARAMS,
                              PRESET_MODE, SIMULATION, find_param)
from torpedo.tui.app import TorpedoApp
from torpedo.tui.editor import Editor
from torpedo.tui.widgets import ConfirmScreen, PickerScreen, Value

CONTOUR = find_param("arcade_contour").index
EQ_GAIN0 = find_param("eq_l_gain0").index
SIZE = (130, 60)


def test_layouts_match_the_parameter_table():
    for mode, layout in LAYOUTS.items():
        for c in controls(layout):
            p = PARAMS[c.index]
            assert p.writable, c.param
            if c.kind == CHOICE:
                assert len(c.choices) == p.hi - p.lo + 1, c.param
            if mode == ARCADE:
                assert c.index in ARCADE_EDITABLE, f"arcade mode ignores {c.param}"


async def settle(app, pilot):
    """Wait until queued device calls and their UI callbacks are done."""
    for _ in range(200):
        await pilot.pause(0.02)
        if app.connected and not app.busy:
            await pilot.pause(0.02)
            return
    raise AssertionError("device queue never settled")


def widget(app, mode: int, name: str, readonly: bool = False) -> Value:
    editors = [e for e in app.query(f"#mode-{mode} Editor").results(Editor) if e.readonly == readonly]
    return next(w for w in editors[0].query(Value) if w.tag.param == name.upper())


def writes(dev: FakeHid) -> list[bytes]:
    return [b[:3] for b in dev.sent if b[0] not in (MSG_POLL, MSG_QUERY)]


@pytest.fixture
def dev():
    return FakeHid()


@pytest.fixture
def app(dev):
    return TorpedoApp(lambda: Torpedo(dev), "test")


@pytest.mark.asyncio
async def test_initial_state_shown(app, dev):
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert A001[PRESET_MODE] == ARCADE
        assert app.query_one("#modes").current == "mode-1"
        assert widget(app, ARCADE, "arcade_contour").value == A001[CONTOUR]
        dimmed = widget(app, ARCADE, "simuamp_depth", readonly=True)
        assert dimmed.value == A001[dimmed.tag.index] and dimmed.disabled
        assert "LuxTweeDe" in widget(app, ARCADE, "simuhp_speaker").label()
        assert "A001 BDELUXE 1 RBN" in str(app.query_one("#status").render())
        assert writes(dev) == [], "showing the state must not write it back"


@pytest.mark.asyncio
async def test_slider_writes_and_shows_linked_changes(app, dev):
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        contour = widget(app, ARCADE, "arcade_contour")
        contour.focus()
        for _ in range(3):
            await pilot.press("right")
        await settle(app, pilot)
        assert dev.params[CONTOUR] == A001[CONTOUR] + 3
        assert writes(dev)[-1] == bytes([MSG_PARAM, CONTOUR, A001[CONTOUR] + 3])
        assert widget(app, ARCADE, "eq_l_gain0", readonly=True).value == dev.params[EQ_GAIN0]
        assert "edited" in str(app.query_one("#status").render())


@pytest.mark.asyncio
async def test_mode_switch_and_cab_picker(app, dev):
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        app.query_one("#mode").focus()
        await pilot.press("left")
        await settle(app, pilot)
        assert dev.params[PRESET_MODE] == SIMULATION
        assert app.query_one("#modes").current == "mode-0"
        widget(app, SIMULATION, "simuhp_speaker").focus()
        await pilot.press("enter")
        assert isinstance(app.screen, PickerScreen)
        await pilot.press(*"darkprinz", "enter")
        await settle(app, pilot)
        assert dev.params[SPEAKER] == 0x12
        # DarkPrinz has no "Dyn 7b": the mic index stays, on that cab's mic 5
        assert widget(app, SIMULATION, "simuhp_mic").label() == "Dyn 906"


@pytest.mark.asyncio
async def test_front_panel_change_shows_up(app, dev):
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        app.device.submit(lambda t: t.dev.panel_param(CONTOUR, 10))
        for _ in range(50):
            await pilot.pause(0.05)
            if widget(app, ARCADE, "arcade_contour").value == 10:
                break
        w = widget(app, ARCADE, "arcade_contour")
        assert w.value == 10 and w.has_class("-changed")
        assert writes(dev) == []


@pytest.mark.asyncio
async def test_selecting_a_preset_with_edits_asks_first(app, dev):
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        widget(app, ARCADE, "arcade_tone").focus()
        await pilot.press("right")
        await settle(app, pilot)
        app.choose_preset(1)
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#yes")
        await settle(app, pilot)
        assert dev.setup[3] == 1
        assert "A002" in str(app.query_one("#status").render())


@pytest.mark.asyncio
async def test_save_into_empty_slot_and_overwrite_needs_confirmation(app, dev):
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("s")
        await pilot.pause()
        app.screen.query_one("#name").value = "MY SOUND"
        await pilot.press("enter")
        await settle(app, pilot)
        assert dev.stores == [75] and dev.names[75] == "MY SOUND"
        await pilot.press("s")
        await pilot.pause()
        app.screen.query_one("#slot").value = "A002"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("enter")  # focus is on Cancel
        await settle(app, pilot)
        assert dev.stores == [75]


@pytest.mark.asyncio
async def test_reconnects_after_unplug(app, dev):
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        read = dev.read

        def unplugged(*a):
            raise OSError("read error")
        dev.read = unplugged
        for _ in range(50):
            await pilot.pause(0.05)
            if not app.connected:
                break
        assert not app.connected
        dev.read = read
        for _ in range(100):
            await pilot.pause(0.05)
            if app.connected:
                break
        assert app.connected
        assert "A001" in str(app.query_one("#status").render())
