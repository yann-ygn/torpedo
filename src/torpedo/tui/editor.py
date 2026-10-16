"""The preset editor: one layout (layout.py) as rows of label + value widgets."""

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Label, Static

from ..device import SPEAKER
from ..layout import CAB, CHOICE, IR_SLOT, MIC, SLIDER, Control, Section, Values, controls
from ..protocol import PARAMS, Cab
from .widgets import Choice, Slider, Value, slider_label_width


def make_widget(c: Control, **kw) -> Value:
    if c.kind == SLIDER:
        p = PARAMS[c.index]
        return Slider(p.lo, p.hi, c, fmt=c.fmt, ends=c.ends, **kw)
    if c.kind == CHOICE:
        return Choice(c.choices, c, **kw)
    return Choice([], c, inline=False, **kw)  # CAB, MIC, IR_SLOT: options come from the unit


class Editor(Vertical):
    DEFAULT_CSS = """
    Editor { height: auto; padding: 0 1; }
    Editor .section { height: 1; margin-top: 1; }
    Editor .title { width: 14; text-style: bold; color: $accent; }
    Editor .switch { width: 12; }
    Editor .row { height: 1; }
    Editor .label { width: 14; padding-left: 2; color: $foreground; }
    Editor .columns Label { width: 1fr; text-style: underline; color: $foreground; }
    Editor .columns .label { width: 14; text-style: none; }
    Editor .alt { width: 1fr; color: $foreground; }
    Editor.readonly .title, Editor.readonly .label, Editor.readonly .columns Label,
    Editor.readonly .alt { color: $text-disabled; }
    """

    def __init__(self, layout: tuple[Section, ...], readonly: bool = False, **kw):
        super().__init__(**kw)
        self.sections, self.readonly = layout, readonly
        self.rows: list[tuple[object, Horizontal]] = []
        self.alts: dict[Value, Static] = {}
        if readonly:
            self.add_class("readonly")
        # sliders with end words (arcade's cold ... hot) share one width for
        # those words and the value, so their bars line up
        ended = [c for c in controls(layout) if c.kind == SLIDER and any(c.ends)]
        self.ends_width = tuple(max((len(c.ends[i]) for c in ended), default=0) for i in (0, 1))
        self.label_width = max((slider_label_width(PARAMS[c.index].lo, PARAMS[c.index].hi, c.fmt)
                                for c in ended), default=0)

    def _widget(self, c: Control, row: int, col: int, classes: str | None = None) -> Value:
        pad = ({"ends_width": self.ends_width, "label_width": self.label_width}
               if c.kind == SLIDER and any(c.ends) else {})
        w = make_widget(c, classes=classes, **pad)
        w.grid = (row, col)
        w.disabled = self.readonly
        return w

    def compose(self) -> ComposeResult:
        r = 0
        for s in self.sections:
            with Horizontal(classes="section"):
                yield Label(s.title, classes="title")
                if s.switch:
                    yield self._widget(s.switch, r, 0, "switch")
                if s.head:
                    yield self._widget(s.head, r, 1)
            r += 1
            if s.columns:
                with Horizontal(classes="row columns"):
                    yield Label("", classes="label")
                    for heading in s.columns:
                        yield Label(heading)
            for row in s.rows:
                with Horizontal(classes="row") as h:
                    self.rows.append((row, h))
                    yield Label(row.label, classes="label")
                    for col, c in enumerate(row.cells):
                        w = self._widget(c, r, col)
                        yield w
                        if c.when:
                            self.alts[w] = Static("", classes="alt")
                            yield self.alts[w]
                r += 1

    def show(self, values: Values, cabs: list[Cab], irs: list[Cab],
             changed: frozenset[int] = frozenset(), skip: frozenset[int] = frozenset()):
        """Show the unit's values. `changed` flash; `skip` are being edited (debounced)."""
        for row, h in self.rows:
            h.display = row.when is None or row.when(values)
        speaker = values.get(SPEAKER, -1)
        mics = cabs[speaker].mics if 0 <= speaker < len(cabs) else []
        for w in self.query(Value):
            c: Control = w.tag
            if c.kind == CAB:
                w.set_options([f"{x.name}  ({x.maker})" for x in cabs])
            elif c.kind == MIC:
                w.set_options([m or "-" for m in mics])
            elif c.kind == IR_SLOT:
                w.set_options([x.name for x in irs])
            if c.index in values and c.index not in skip:
                w.value = values[c.index]
            if c.when:
                shown = c.when(values)
                w.display = shown
                self.alts[w].display = not shown
                self.alts[w].update(c.otherwise(values) if c.otherwise else "")
            if c.index in changed:
                w.flash()

    def on_value_nav(self, event: Value.Nav):
        """Up/down: the nearest row in that direction, same column if it has one."""
        event.stop()
        r0, c0 = event.widget.grid
        found = [w for w in self.query(Value)
                 if w.focusable and w.display and w.parent.display
                 and (w.grid[0] - r0) * event.delta > 0]
        if found:
            target = min(found, key=lambda w: (abs(w.grid[0] - r0), abs(w.grid[1] - c0)))
            target.focus()
