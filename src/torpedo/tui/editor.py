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
    Editor.readonly .alt, Editor .-off .label, Editor .-off.columns Label,
    Editor .-off .alt { color: $text-disabled; }
    """

    def __init__(self, layout: tuple[Section, ...], readonly: bool = False, **kw):
        super().__init__(**kw)
        self.sections, self.readonly = layout, readonly
        self.rows: list[tuple[object, Horizontal]] = []
        self.alts: dict[Value, Static] = {}
        # sections with an on/off: (switch, its widgets, its rows), dimmed while off
        self.switched: list[tuple[Control, list[Value], list[Horizontal]]] = []
        if readonly:
            self.add_class("readonly")

    @staticmethod
    def padding(s: Section) -> dict:
        """The sliders of a section share one width for their end words (cold ... hot)
        and their value, so their bars and values line up."""
        sliders = [c for c in controls((s,)) if c.kind == SLIDER]
        return {"ends_width": tuple(max((len(c.ends[i]) for c in sliders), default=0) for i in (0, 1)),
                "label_width": max((slider_label_width(PARAMS[c.index].lo, PARAMS[c.index].hi, c.fmt)
                                    for c in sliders), default=0)}

    def _widget(self, c: Control, row: int, col: int, classes: str | None = None,
                pad: dict | None = None) -> Value:
        kw = pad if pad and c.kind == SLIDER else {}
        w = make_widget(c, classes=classes, **kw)
        w.grid = (row, col)
        w.disabled = self.readonly
        return w

    def compose(self) -> ComposeResult:
        r = 0
        for s in self.sections:
            pad = self.padding(s)
            widgets, rows = [], []
            if s.switch and s.dims:
                self.switched.append((s.switch, widgets, rows))
            with Horizontal(classes="section"):
                yield Label(s.title, classes="title")
                if s.switch:
                    yield self._widget(s.switch, r, 0, "switch")
                if s.head:
                    widgets.append(self._widget(s.head, r, 1))
                    yield widgets[-1]
            r += 1
            if s.columns:
                with Horizontal(classes="row columns") as h:
                    rows.append(h)
                    yield Label("", classes="label")
                    for heading in s.columns:
                        yield Label(heading)
            for row in s.rows:
                with Horizontal(classes="row") as h:
                    self.rows.append((row, h))
                    rows.append(h)
                    yield Label(row.label, classes="label")
                    for col, c in enumerate(row.cells):
                        w = self._widget(c, r, col, pad=pad)
                        widgets.append(w)
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
        for switch, widgets, rows in self.switched:
            off = values.get(switch.index) == 0  # a section turned off: dim it, skip it
            for w in widgets:
                w.disabled = self.readonly or off
            for h in rows:
                h.set_class(off, "-off")
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
