"""Keyboard-first value widgets (slider, choice), the meters and the overlay screens.

Setting `value` from code never posts anything: only user actions post
`Value.Changed`, so showing the device's state can't write it back.
"""

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Input, Label, OptionList
from textual.widgets.option_list import Option

from ..protocol import LED_DBFS, Meters, meter_db


class Value(Widget, can_focus=True):
    """An integer value; `tag` says what it edits (a layout.Control, a SetupOption...)."""

    BINDINGS = [
        Binding("up", "nav(-1)", show=False),
        Binding("down", "nav(1)", show=False),
    ]
    DEFAULT_CSS = """
    Value { height: 1; width: 1fr; color: $foreground; }
    Value:focus {
        color: $block-cursor-foreground;
        background: $block-cursor-background;
        text-style: $block-cursor-text-style;
    }
    Value:disabled { color: $text-disabled; }
    Value.-changed { background: $warning 30%; }
    """

    value: reactive[int] = reactive(0)

    class Changed(Message):
        def __init__(self, widget: "Value", value: int):
            super().__init__()
            self.widget, self.value = widget, value

        @property
        def control(self) -> "Value":
            return self.widget

    class Nav(Message):
        """Up/down: the editor moves focus by row, others fall back to focus order."""
        def __init__(self, widget: "Value", delta: int):
            super().__init__()
            self.widget, self.delta = widget, delta

    def __init__(self, tag=None, *, classes: str | None = None, id: str | None = None):
        super().__init__(classes=classes, id=id)
        self.tag = tag
        self.grid = (0, 0)  # (row, column), for up/down in the editor

    def user_set(self, value: int):
        value = self.clamp(value)
        if value != self.value:
            self.value = value
            self.post_message(self.Changed(self, value))

    def clamp(self, value: int) -> int:
        return value

    def action_nav(self, delta: int):
        self.post_message(self.Nav(self, delta))

    def flash(self):
        self.add_class("-changed")
        self.set_timer(1.0, lambda: self.remove_class("-changed"))


def slider_label_width(lo: int, hi: int, fmt=str) -> int:
    return max(len(fmt(v)) for v in range(lo, hi + 1))


class Slider(Value):
    """Bar + value: arrows step (shift: x10), Home/End, Enter types a number, mouse drags."""

    COMPONENT_CLASSES = {"slider--fill", "slider--knob", "slider--track", "slider--end"}
    DEFAULT_CSS = """
    Slider > .slider--fill { color: $accent; }
    Slider > .slider--knob { color: $accent; text-style: bold; }
    Slider:focus > .slider--knob { color: $block-cursor-foreground; }
    Slider > .slider--track { color: $text-muted; }
    Slider > .slider--end { color: $foreground; }
    Slider:disabled > .slider--end { color: $text-disabled; }
    Slider:focus > .slider--track, Slider:focus > .slider--end { color: $block-cursor-foreground; }
    Slider:disabled > .slider--fill, Slider:disabled > .slider--knob { color: $text-disabled; }
    """
    BINDINGS = [
        Binding("left", "step(-1)", "Less", show=False),
        Binding("right", "step(1)", "More", show=False),
        Binding("shift+left", "step(-10)", show=False),
        Binding("shift+right", "step(10)", show=False),
        Binding("home", "jump(0)", show=False),
        Binding("end", "jump(1)", show=False),
        Binding("enter", "type", "Type value", show=False),
    ]

    def __init__(self, lo: int, hi: int, tag=None, *, fmt=str, ends: tuple[str, str] = ("", ""),
                 ends_width: tuple[int, int] = (0, 0), label_width: int = 0, **kw):
        """`ends_width` and `label_width` pad the end words and the value, so that
        sliders stacked in a column line up."""
        super().__init__(tag, **kw)
        self.lo, self.hi, self.fmt, self.ends = lo, hi, fmt, ends
        self.ends_width = tuple(max(len(e), w) for e, w in zip(ends, ends_width))
        self.label_width = max(label_width, slider_label_width(lo, hi, fmt))
        self._dragging = False

    def clamp(self, value: int) -> int:
        return max(self.lo, min(self.hi, value))

    def _ends(self) -> tuple[str, str]:
        (a, b), (wa, wb) = self.ends, self.ends_width
        return (f"{a:<{wa}} " if wa else "", f" {b:<{wb}}" if wb else "")

    def _bar_width(self) -> int:
        a, b = self._ends()
        return max(4, min(24, self.size.width - len(a) - len(b) - self.label_width - 2))

    def render(self) -> Text:
        a, b = self._ends()
        width = self._bar_width()
        span = self.hi - self.lo
        v = self.clamp(self.value)
        knob = round((v - self.lo) / span * (width - 1)) if span else 0
        end = self.get_component_rich_style("slider--end")
        text = Text(a, style=end)
        text.append("━" * knob, style=self.get_component_rich_style("slider--fill"))
        text.append("●", style=self.get_component_rich_style("slider--knob"))
        text.append("─" * (width - knob - 1), style=self.get_component_rich_style("slider--track"))
        text.append(b, style=end)
        label = self.fmt(self.value) if v == self.value else f"?{self.value}"
        text.append(f"  {label:>{self.label_width}}")
        return text

    def _value_at(self, x: int) -> int:
        width = self._bar_width()
        x = max(0, min(width - 1, x - len(self._ends()[0])))
        return self.lo + round(x / (width - 1) * (self.hi - self.lo))

    def on_mouse_down(self, event: events.MouseDown):
        if self.disabled:
            return
        self._dragging = True
        self.capture_mouse()
        self.focus()
        self.user_set(self._value_at(event.x))

    def on_mouse_move(self, event: events.MouseMove):
        if self._dragging:
            self.user_set(self._value_at(event.x))

    def on_mouse_up(self, event: events.MouseUp):
        self._dragging = False
        self.release_mouse()

    def action_step(self, delta: int):
        self.user_set(self.value + delta)

    def action_jump(self, end: int):
        self.user_set(self.hi if end else self.lo)

    def action_type(self):
        def done(v: int | None):
            if v is not None:
                self.user_set(v)
        self.app.push_screen(NumberScreen(self.lo, self.hi, self.value), done)


class Choice(Value):
    """One of `options`. Up to 3 options are all shown inline (Enter/Space cycles);
    longer lists show the current one and Enter opens a picker (`Pick`).
    An on/off gets `passing`, the value that lets the sound through: shown
    green when selected, the other one red."""

    COMPONENT_CLASSES = {"choice--on", "choice--passing", "choice--blocking"}
    DEFAULT_CSS = """
    Choice > .choice--on { color: $background; background: $accent; text-style: bold; }
    Choice > .choice--passing { color: $background; background: $success; text-style: bold; }
    Choice > .choice--blocking { color: $background; background: $error; text-style: bold; }
    Choice:disabled > .choice--on { color: $background; background: $foreground 35%; }
    """
    BINDINGS = [
        Binding("left", "step(-1)", show=False),
        Binding("right", "step(1)", show=False),
        Binding("enter,space", "activate", "Pick", show=False),
    ]

    class Pick(Message):
        def __init__(self, widget: "Choice"):
            super().__init__()
            self.widget = widget

    def __init__(self, options: list[str] | tuple[str, ...], tag=None, *, inline: bool | None = None,
                 passing: int | None = None, **kw):
        super().__init__(tag, **kw)
        self.options = list(options)
        self.inline = len(self.options) <= 3 if inline is None else inline
        self.passing = passing

    def set_options(self, options: list[str]):
        if options != self.options:
            self.options = list(options)
            self.refresh()

    def clamp(self, value: int) -> int:
        return max(0, min(len(self.options) - 1, value))

    def label(self, v: int | None = None) -> str:
        v = self.value if v is None else v
        return self.options[v] if 0 <= v < len(self.options) else f"? ({v})"

    def render(self) -> Text:
        if not self.inline:
            return Text(f"{self.label()} ▾")
        text = Text()
        on = self.get_component_rich_style(
            "choice--on" if self.passing is None or self.disabled
            else "choice--passing" if self.value == self.passing else "choice--blocking")
        for i, o in enumerate(self.options):
            text.append(f" {o} ", style=on if i == self.value else "")
            text.append(" ")
        if not 0 <= self.value < len(self.options):
            text.append(self.label())
        return text

    def action_step(self, delta: int):
        self.user_set(self.value + delta)

    def action_activate(self):
        if self.inline:
            self.user_set((self.value + 1) % len(self.options))
        else:
            self.post_message(self.Pick(self))

    def on_click(self, event: events.Click):
        if self.disabled:
            return
        if not self.inline:
            self.post_message(self.Pick(self))
            return
        x = 0
        for i, o in enumerate(self.options):
            x += len(o) + 3
            if event.x < x:
                self.user_set(i)
                return


class MeterBar(Widget):
    """Input and output meters like the unit's LEDs: one segment per LED
    (LED_DBFS) and CLIP, coloured like the panel: 3 green, 2 orange ($warning
    is Dracula's orange), red CLIP. Quieter signals only show in the dB value."""

    SEGMENT = 3  # cells per 6 dB
    COMPONENT_CLASSES = {"meter--low", "meter--high", "meter--unlit", "meter--clip", "meter--noclip"}
    DEFAULT_CSS = """
    MeterBar { height: 1; width: 1fr; }
    MeterBar > .meter--low { color: $success; }
    MeterBar > .meter--high { color: $warning; }
    MeterBar > .meter--unlit { color: $foreground 20%; }
    MeterBar > .meter--clip { color: $background; background: $error; text-style: bold; }
    MeterBar > .meter--noclip { color: $foreground 30%; }
    """
    meters: reactive[Meters] = reactive(Meters)

    def render(self) -> Text:
        text = Text()
        m = self.meters
        self._meter(text, "IN ", m.input, m.clip_in)
        text.append("    ")
        self._meter(text, "OUT", m.output, m.clip_out)
        return text

    def _meter(self, text: Text, label: str, v: int, clip: bool):
        style = self.get_component_rich_style
        db = meter_db(v)
        text.append(f"{label} ")
        for k, floor in enumerate(LED_DBFS):
            top = LED_DBFS[k + 1] if k + 1 < len(LED_DBFS) else 0
            lit = style("meter--high" if k >= 3 else "meter--low")
            for c in range(self.SEGMENT):
                on = db >= floor + (top - floor) * c / self.SEGMENT
                text.append("█" if on else "░", style=lit if on else style("meter--unlit"))
            text.append(" ")
        text.append(" CLIP ", style=style("meter--clip" if clip else "meter--noclip"))
        text.append(f" {db:4.0f} dB" if v else "    -∞ dB", style=style("meter--noclip"))


# --- overlays ---------------------------------------------------------------

class PickerScreen(ModalScreen[int | None]):
    """Filterable list overlay: type to filter, arrows to move, Enter picks, Esc cancels."""

    DEFAULT_CSS = """
    PickerScreen { align: center middle; }
    PickerScreen > Vertical {
        width: 70; height: 80%; padding: 0 1;
        border: thick $accent 60%; background: $surface;
    }
    PickerScreen #title { margin: 1 0 0 0; text-style: bold; }
    PickerScreen OptionList { height: 1fr; margin-bottom: 1; }
    """
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, title: str, items: list[tuple[int, str]], current: int | None = None):
        super().__init__()
        self.title_text, self.items, self.current = title, items, current

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.title_text, id="title")
            yield Input(placeholder="filter", id="filter")
            yield OptionList(id="options")

    def on_mount(self):
        self.fill("")

    def fill(self, text: str):
        options = self.query_one(OptionList)
        options.clear_options()
        words = text.lower().split()
        shown = [(v, label) for v, label in self.items if all(w in label.lower() for w in words)]
        options.add_options(Option(label, id=str(v)) for v, label in shown)
        values = [v for v, _ in shown]
        if values:
            options.highlighted = values.index(self.current) if self.current in values else 0

    def on_input_changed(self, event: Input.Changed):
        self.fill(event.value)

    def on_key(self, event: events.Key):
        options = self.query_one(OptionList)
        if event.key in ("up", "down", "pageup", "pagedown") and self.focused is not options:
            getattr(options, {"up": "action_cursor_up", "down": "action_cursor_down",
                              "pageup": "action_page_up", "pagedown": "action_page_down"}[event.key])()
            event.stop()

    def on_input_submitted(self, event: Input.Submitted):
        options = self.query_one(OptionList)
        if options.highlighted is not None:
            self.dismiss(int(options.get_option_at_index(options.highlighted).id))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        self.dismiss(int(event.option.id))


class FormScreen(ModalScreen):
    DEFAULT_CSS = """
    FormScreen { align: center middle; }
    FormScreen > Vertical {
        width: 64; height: auto; padding: 1 2;
        border: thick $accent 60%; background: $surface;
    }
    FormScreen Label { margin-top: 1; }
    FormScreen #error { color: $error; }
    """
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def error(self, text: str):
        self.query_one("#error", Label).update(text)


class NumberScreen(FormScreen):
    def __init__(self, lo: int, hi: int, value: int):
        super().__init__()
        self.lo, self.hi, self.value = lo, hi, value

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Value ({self.lo}..{self.hi})")
            yield Input(str(self.value), type="integer", id="value")
            yield Label("", id="error")

    def on_input_submitted(self, event: Input.Submitted):
        try:
            v = int(event.value)
        except ValueError:
            v = None
        if v is None or not self.lo <= v <= self.hi:
            self.error(f"must be {self.lo}..{self.hi}")
        else:
            self.dismiss(v)


class TextScreen(FormScreen):
    """One line of text, checked by `check` (raises ValueError) before closing."""

    def __init__(self, prompt: str, value: str, check=lambda s: s):
        super().__init__()
        self.prompt, self.value, self.check = prompt, value, check

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.prompt)
            yield Input(self.value, id="value")
            yield Label("", id="error")

    def on_input_submitted(self, event: Input.Submitted):
        try:
            self.dismiss(self.check(event.value))
        except ValueError as e:
            self.error(str(e))


class ConfirmScreen(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    ConfirmScreen > Grid {
        grid-size: 2; grid-gutter: 1 2; grid-rows: auto 3;
        padding: 1 2; width: 72; height: auto;
        border: thick $warning 80%; background: $surface;
    }
    ConfirmScreen #question { column-span: 2; width: 1fr; }
    ConfirmScreen Button { width: 100%; }
    """
    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    def __init__(self, question: str, yes: str = "OK"):
        super().__init__()
        self.question, self.yes = question, yes

    def compose(self) -> ComposeResult:
        with Grid():
            yield Label(self.question, id="question")
            yield Button(self.yes, variant="error", id="yes")
            yield Button("Cancel", variant="primary", id="no")

    def on_mount(self):
        self.query_one("#no", Button).focus()  # Enter must not do the risky thing

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss(event.button.id == "yes")
