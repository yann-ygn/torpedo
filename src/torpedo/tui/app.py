import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.timer import Timer
from textual.widgets import (ContentSwitcher, Footer, Header, Input, Label, OptionList, Static,
                             TabbedContent, TabPane)
from textual.widgets.option_list import Option

from ..device import Info, Torpedo
from ..layout import (ARCADE_LAYOUT, CAB, IR_LAYOUT, IR_SLOT, LAYOUTS, MIC, MODE_CONTROL, MODES,
                      SIMULATION_LAYOUT, SLIDER, Control, level_db)
from ..protocol import (EDIT_BUFFER, MSG_PARAM, PARAMS, PRESET_MODE, PRESETS, SETUP_PRESET,
                        SETUP_OPTIONS, SETUP_PRESET_SOURCE, Cab, Meters, PresetName, check_name,
                        preset_name)
from .editor import Editor
from .panes import BackupPane, IrPane, PresetFilesPane, SetupPane
from .widgets import Choice, ConfirmScreen, FormScreen, MeterBar, PickerScreen, TextScreen, Value

DEBOUNCE = 0.15  # seconds; sliders only write once they settle
POLL = 0.1  # front-panel changes and meters
RETRY = 2.0  # reconnect attempts while the Torpedo is unplugged


class DeviceThread:
    """Runs device calls one at a time, in order, off the UI thread."""

    def __init__(self, app: App):
        self.app = app
        self.target: Torpedo | None = None
        self.queue: queue.Queue = queue.Queue()
        self.thread = threading.Thread(target=self._run, name="torpedo-device", daemon=True)
        self.thread.start()

    def submit(self, fn: Callable[[Torpedo], object], on_done: Callable[[object], None] | None = None):
        self.queue.put((fn, on_done))

    @property
    def busy(self) -> bool:
        return self.queue.unfinished_tasks > 0

    def stop(self):
        self.queue.put((None, None))
        self.thread.join(timeout=2)

    def _run(self):
        while True:
            fn, on_done = self.queue.get()
            try:
                if fn is None:
                    return
                try:
                    result = fn(self.target)
                except Exception as e:  # report and keep serving
                    self._call(self.app.device_error, e)
                    continue
                if on_done is not None:
                    self._call(on_done, result)
            finally:
                self.queue.task_done()

    def _call(self, callback, *args):
        try:
            self.app.call_from_thread(callback, *args)
        except (RuntimeError, NoMatches):  # app shut down, or its widgets already gone
            pass


@dataclass
class Snapshot:
    """The device's cache, copied in the device thread for the UI."""
    params: dict[int, int]
    setup: dict[int, int]
    meters: Meters = Meters()

    @property
    def edited(self) -> bool:
        return self.setup.get(SETUP_PRESET) == EDIT_BUFFER

    @property
    def source(self) -> int:
        return self.setup.get(SETUP_PRESET_SOURCE if self.edited else SETUP_PRESET, 0)


def snapshot(t: Torpedo) -> Snapshot:
    return Snapshot(dict(t.params), dict(t.setup), t.meters)


@dataclass
class Loaded:
    info: Info
    names: list[PresetName]
    cabs: list[Cab]
    irs: list[Cab]
    state: Snapshot


def load_all(t: Torpedo) -> Loaded:
    info = t.info()
    t.read_params()
    t.read_setup()
    return Loaded(info, t.read_names(), t.read_cabs(), t.read_irs(), snapshot(t))


def is_disconnect(e: Exception) -> bool:
    return isinstance(e, OSError) and not isinstance(e, TimeoutError)


class PresetList(Vertical):
    DEFAULT_CSS = """
    PresetList { width: 40; }
    PresetList OptionList { height: 1fr; }
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self._shown = None

    def compose(self) -> ComposeResult:
        yield Input(placeholder="/ filter presets", id="preset-filter")
        yield OptionList(id="preset-list")

    def show(self, names: list[PresetName], state: Snapshot):
        words = self.query_one("#preset-filter", Input).value.lower().split()
        key = (tuple((p.name, p.arcade) for p in names), state.source, state.edited, tuple(words))
        if key == self._shown:
            return
        self._shown = key
        options = self.query_one("#preset-list", OptionList)
        highlighted = options.highlighted_option.id if options.highlighted_option else None
        options.clear_options()
        for p in names[:PRESETS]:
            label = f"{preset_name(p.index)} {p.name}"
            if words and not all(w in label.lower() for w in words):
                continue
            current = p.index == state.source
            text = Text(">" if current else " ", style="bold")
            text.append(f"{preset_name(p.index)} ")
            text.append(f"{p.name:<25}", style="dim" if p.empty else "bold" if current else "")
            text.append(" " + ("" if p.empty else "A" if p.arcade else "S"), style="dim")
            if current and state.edited:
                text.append(" *", style="bold yellow")
            options.add_option(Option(text, id=str(p.index)))
        ids = [o.id for o in options.options]
        wanted = highlighted if highlighted in ids else str(state.source)
        if wanted in ids:
            options.highlighted = ids.index(wanted)

    def on_input_changed(self, event: Input.Changed):
        self._shown = None
        self.app.show_presets()

    def on_input_submitted(self, event: Input.Submitted):
        self.query_one("#preset-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        self.app.choose_preset(int(event.option.id))

    @property
    def highlighted(self) -> int | None:
        option = self.query_one("#preset-list", OptionList).highlighted_option
        return int(option.id) if option else None


class SaveScreen(FormScreen):
    """Slot + name; returns (slot index, name)."""

    def __init__(self, slot: int, name: str):
        super().__init__()
        self.slot, self.preset_label = slot, name

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Save into preset (A001..A128)")
            yield Input(preset_name(self.slot), id="slot")
            yield Label("Name (A-Z, 0-9, space)")
            yield Input(self.preset_label, id="name")
            yield Label("", id="error")

    def on_mount(self):
        self.query_one("#name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted):
        text = self.query_one("#slot", Input).value.strip().upper().removeprefix("A")
        if not (text.isdigit() and 1 <= int(text) <= PRESETS):
            self.error(f"preset must be A001..A{PRESETS:03d}")
            return
        try:
            name = check_name(self.query_one("#name", Input).value)
        except ValueError as e:
            self.error(str(e))
            return
        self.dismiss((int(text) - 1, name))


class TorpedoApp(App):
    TITLE = "Torpedo Live"
    CSS = """
    #main { width: 1fr; }
    #topbar { height: 1; margin: 0 1; }
    #meters { margin: 0 1; }
    #status { width: 1fr; }
    #mode { width: 34; }
    #modes { height: 1fr; }
    .readonly-title { margin: 2 1 0 1; color: $text-disabled; text-style: italic; }
    """
    BINDINGS = [
        Binding("s", "save", "Save"),
        Binding("r", "rename", "Rename"),
        Binding("d", "discard", "Discard"),
        Binding("delete", "clear", "Clear"),
        Binding("slash", "filter", "Filter"),
        Binding("f5", "reload", "Reload"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, connect: Callable[[], Torpedo], subtitle: str = ""):
        super().__init__()
        self.connect = connect
        self.base_subtitle = subtitle
        self.connected = False
        self.info: Info | None = None
        self.names: list[PresetName] = []
        self.cabs: list[Cab] = []
        self.irs: list[Cab] = []
        self.state = Snapshot({}, {})
        self.device: DeviceThread | None = None
        self._debounce_timers: dict[object, Timer] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield MeterBar(id="meters")
        with TabbedContent(initial="edit"):
            with TabPane("Edit", id="edit"):
                with Horizontal():
                    yield PresetList(id="presets")
                    with Vertical(id="main"):
                        with Horizontal(id="topbar"):
                            yield Static("connecting...", id="status")
                            yield Choice(MODES, MODE_CONTROL, id="mode")
                        with ContentSwitcher(initial="mode-0", id="modes"):
                            with VerticalScroll(id="mode-0"):
                                yield Editor(SIMULATION_LAYOUT)
                            with VerticalScroll(id="mode-1"):
                                yield Editor(ARCADE_LAYOUT)
                                yield Label("Simulation values set by arcade mode (read-only)",
                                            classes="readonly-title")
                                yield Editor(SIMULATION_LAYOUT, readonly=True)
                            with VerticalScroll(id="mode-2"):
                                yield Editor(IR_LAYOUT)
            yield IrPane("IRs", id="irs")
            yield PresetFilesPane("Preset files", id="files")
            yield SetupPane("Setup", id="setup")
            yield BackupPane("Backup", id="backup")
        yield Footer()

    def on_mount(self):
        self.theme = "dracula"
        self.sub_title = self.base_subtitle
        self.device = DeviceThread(self)
        self.reconnect()
        self.set_interval(POLL, self.poll)

    def on_unmount(self):
        if self.device:
            self.device.stop()

    # --- connection -------------------------------------------------------

    def reconnect(self):
        def job(_):
            try:
                t = self.connect()
            except (RuntimeError, OSError):
                return None
            self.device.target = t
            return load_all(t)
        self.device.submit(job, self.loaded)

    def loaded(self, loaded: Loaded | None):
        if loaded is None:
            self.query_one("#status", Static).update("[b red]Torpedo not found[/], retrying...")
            self.set_timer(RETRY, self.reconnect)
            return
        self.connected = True
        self.info, self.names, self.cabs, self.irs = loaded.info, loaded.names, loaded.cabs, loaded.irs
        sub = f"{loaded.info.serial} · firmware {loaded.info.firmware}"
        self.sub_title = f"{self.base_subtitle} · {sub}" if self.base_subtitle else sub
        self.query_one(IrPane).show_irs(self.irs)
        self.show_state(loaded.state)

    def lost(self):
        """The unit was unplugged or switched off: it sends nothing after that, so
        everything is read again once it is back."""
        self.connected = False
        self.query_one("#status", Static).update("[b red]disconnected[/], waiting for the Torpedo...")
        self.query_one(MeterBar).meters = Meters()

        def close(t):
            self.device.target = None
            if t is not None:
                try:
                    t.close()
                except OSError:
                    pass
        self.device.submit(close)
        self.set_timer(RETRY, self.reconnect)

    def device_error(self, error: Exception):
        if is_disconnect(error):
            if self.connected:
                self.lost()
            return
        self.notify(f"{error}", title=type(error).__name__, severity="error")
        if self.connected:
            self.device.submit(snapshot, self.show_state)  # put widgets back on the real values

    def poll(self):
        if self.connected and not self.busy:
            self.device.submit(lambda t: (t.poll(), snapshot(t)), self.polled)

    def polled(self, result: tuple[list[bytes], Snapshot]):
        msgs, state = result
        self.query_one(MeterBar).meters = state.meters
        if msgs:
            self.show_state(state, frozenset(p[1] for p in msgs if p[0] == MSG_PARAM))

    # --- device -> UI ---------------------------------------------------------

    def show_state(self, state: Snapshot, changed: frozenset[int] = frozenset()):
        self.state = state
        params = state.params
        mode = params.get(PRESET_MODE, 0)
        self.query_one("#modes", ContentSwitcher).current = f"mode-{mode if mode in LAYOUTS else 0}"
        pending = frozenset(k for k in self._debounce_timers if isinstance(k, int))
        for editor in self.query(Editor):
            editor.show(params, self.cabs, self.irs, changed, pending)
        if PRESET_MODE in params:
            self.query_one("#mode", Choice).value = mode
        self.show_status()
        self.show_presets()
        self.query_one(SetupPane).show(state.setup)

    def show_status(self):
        s = self.state
        if not self.names:
            return
        text = f"[b]{preset_name(s.source)} {self.names[s.source].name}[/]"
        if s.edited:
            text += "  [b $warning]● edited[/] (unsaved)"
        level, mute = s.setup.get(SETUP_OPTIONS["level"].index), s.setup.get(SETUP_OPTIONS["mute"].index)
        if level is not None:
            text += f"   [dim]out {level_db(level)} · mute {'[b red]ON[/]' if mute else 'off'}[/]"
        self.query_one("#status", Static).update(text)

    def show_presets(self):
        if self.names:
            self.query_one(PresetList).show(self.names, self.state)

    def set_names(self, names: list[PresetName]):
        self.names = names
        self.show_status()
        self.show_presets()

    def set_irs(self, irs: list[Cab]):
        self.irs = irs
        self.query_one(IrPane).show_irs(irs)
        self.show_state(self.state)

    # --- UI -> device ---------------------------------------------------------

    def run_job(self, job: Callable[[Torpedo], object], then: Callable[[object], None] | None = None):
        """Run `job` on the device, then show the state it left (and pass its result to `then`)."""
        if not self.connected:
            self.notify("The Torpedo isn't connected", severity="warning")
            self.show_state(self.state)
            return

        def wrapped(t):
            return job(t), snapshot(t)

        def done(result):
            value, state = result
            self.show_state(state)
            if then:
                then(value)
        self.device.submit(wrapped, done)

    def debounce(self, key, fn: Callable[[], None]):
        """Run fn once `key` has had no new calls for DEBOUNCE seconds."""
        if timer := self._debounce_timers.pop(key, None):
            timer.stop()

        def fire():
            self._debounce_timers.pop(key, None)
            fn()
        self._debounce_timers[key] = self.set_timer(DEBOUNCE, fire)

    @property
    def busy(self) -> bool:
        """True while changes are waiting to be debounced or sent."""
        return bool(self._debounce_timers) or self.device.busy

    def on_value_changed(self, event: Value.Changed):
        w = event.widget
        if not isinstance(w.tag, Control):
            return
        if w.tag.kind == SLIDER:
            self.debounce(w.tag.index, lambda: self.write(w.tag, w.value))
        else:
            self.write(w.tag, event.value)

    def write(self, c: Control, v: int):
        cabs, irs = self.cabs, self.irs
        if c.kind == CAB:
            self.run_job(lambda t: t.select_cab(v, cabs))
        elif c.kind == IR_SLOT:
            self.run_job(lambda t: t.select_ir(c.slot, v, irs))
        else:
            self.run_job(lambda t: t.set_param(PARAMS[c.index], v))

    def on_value_nav(self, event: Value.Nav):
        if event.delta > 0:
            self.screen.focus_next()
        else:
            self.screen.focus_previous()

    def on_choice_pick(self, event: Choice.Pick):
        w = event.widget
        kind = w.tag.kind if isinstance(w.tag, Control) else None
        if kind == CAB:
            title = "Cabinet"
            items = [(c.index, f"{c.index:3d}  {c.name:<22} {c.maker}") for c in self.cabs]
        elif kind == IR_SLOT:
            title = f"IR {'AB'[w.tag.slot]}"
            items = [(c.index, f"{c.index:3d}  {c.name}") for c in self.irs]
        else:
            title = "Mic" if kind == MIC else getattr(w.tag, "param", None) or getattr(w.tag, "name", "")
            items = [(i, o) for i, o in enumerate(w.options) if o != "-"]

        def picked(v: int | None):
            if v is not None:
                w.user_set(v)
        self.push_screen(PickerScreen(title, items, w.value), picked)

    def choose_preset(self, n: int):
        s = self.state
        if n == s.source and not s.edited:
            return

        def go(yes: bool | None = True):
            if yes:
                self.run_job(lambda t: t.select_preset(n))
        if s.edited:
            self.push_screen(ConfirmScreen(
                f"Load {preset_name(n)} and leave the unsaved edits to {preset_name(s.source)}?\n\n"
                f"(The unit keeps them as {preset_name(s.source)}* on its panel.)", "Load"), go)
        else:
            go()

    # --- actions ----------------------------------------------------------------

    def action_save(self):
        if not self.names:
            return
        empty = next((p.index for p in self.names[:PRESETS] if p.empty), self.state.source)

        def chosen(result: tuple[int, str] | None):
            if result is None:
                return
            slot, name = result
            target = self.names[slot]

            def store(yes: bool | None = True):
                if yes:
                    self.run_job(lambda t: (t.save(slot, name, overwrite=True), t.read_names())[1],
                                 self.saved)
            if target.empty:
                store()
            else:
                self.push_screen(ConfirmScreen(
                    f"{preset_name(slot)} holds \"{target.name}\". Replace it with the current "
                    f"sound, named \"{name}\"? (flash write)", "Replace"), store)
        self.push_screen(SaveScreen(empty, self.names[self.state.source].name), chosen)

    def saved(self, names: list[PresetName]):
        self.set_names(names)
        self.notify(f"Saved {preset_name(self.state.source)} \"{names[self.state.source].name}\"")

    def action_rename(self):
        if not self.names:
            return
        s = self.state
        if s.edited:
            self.notify("The preset has unsaved edits, and renaming would store them too: "
                        "save (s) or discard (d) first", severity="warning")
            return
        n = s.source

        def renamed(name: str | None):
            if name:
                self.run_job(lambda t: (t.save(n, name, overwrite=True), t.read_names())[1],
                             self.saved)
        self.push_screen(TextScreen(f"Rename {preset_name(n)}", self.names[n].name, check_name),
                         renamed)

    def action_clear(self):
        """Empty the preset highlighted in the list, like Remote's Delete."""
        n = self.query_one(PresetList).highlighted
        if n is None or not self.names:
            return
        s, target = self.state, self.names[n]
        if s.edited and s.source == n:
            self.notify(f"{preset_name(n)} has unsaved edits: save (s) or discard (d) first",
                        severity="warning")
            return

        def clear(yes: bool | None = True):
            if yes:
                self.run_job(lambda t: t.clear_preset(n), lambda names: self.cleared(n, names))
        if target.empty:
            clear()
        else:
            self.push_screen(ConfirmScreen(
                f"Clear {preset_name(n)} \"{target.name}\"? It becomes an empty USER slot with "
                "the default sound (flash write).", "Clear"), clear)

    def cleared(self, n: int, names: list[PresetName]):
        self.set_names(names)
        self.notify(f"Cleared {preset_name(n)}")

    def action_discard(self):
        if not self.state.edited:
            self.notify("No unsaved edits")
            return
        self.run_job(lambda t: t.discard())

    def action_filter(self):
        self.query_one(TabbedContent).active = "edit"
        self.query_one("#preset-filter", Input).focus()

    def action_reload(self):
        if self.connected:
            self.device.submit(load_all, self.loaded)
