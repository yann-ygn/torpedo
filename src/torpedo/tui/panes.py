"""The tabs besides the editor: IR bank, preset files, global settings, backup/restore."""

from datetime import datetime
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (Button, DirectoryTree, Input, Label, OptionList, ProgressBar,
                             TabPane)
from textual.widgets.option_list import Option

from .. import backup, ir, presetfile
from ..layout import level_db
from ..protocol import IR_BANK, SETUP_OPTIONS, Cab, SetupOption
from .widgets import Choice, ConfirmScreen, Slider, Value


def progress_to(bar: ProgressBar):
    """A progress callback for the device thread that updates `bar`."""
    def progress(done: int, total: int):
        bar.app.call_from_thread(bar.update, total=total, progress=done)
    return progress


class WavTree(DirectoryTree):
    def filter_paths(self, paths):
        return [p for p in paths if p.is_dir() or p.suffix.lower() == ".wav"]


class IrPane(TabPane):
    file: Path | None = None  # the WAV selected for upload
    irs: list[Cab] = []
    DEFAULT_CSS = """
    IrPane > Horizontal { height: 1fr; }
    IrPane #ir-bank { width: 1fr; padding: 0 1; }
    IrPane #ir-files { width: 1fr; padding: 0 1; }
    IrPane #ir-list, IrPane WavTree { height: 1fr; }
    IrPane .field { margin-top: 1; color: $text-muted; }
    IrPane .buttons { height: auto; margin-top: 1; }
    IrPane Button { margin-right: 1; }
    IrPane .opt { height: 1; }
    IrPane .opt Label { width: 10; color: $foreground; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="ir-bank"):
                yield Label(f"IR bank {IR_BANK} (what IR mode's slots load)", classes="field")
                yield OptionList(id="ir-list")
                with Horizontal(classes="buttons"):
                    yield Button("Download to folder", id="ir-download")
                    yield Button("Delete", variant="error", id="ir-delete")
            with Vertical(id="ir-files"):
                yield Label("Folder", classes="field")
                yield Input(str(Path.cwd()), id="ir-dir")
                yield WavTree(Path.cwd(), id="ir-tree")
                yield Label("Upload the selected WAV (resampled to 48 kHz) to the end of the bank",
                            classes="field")
                yield Input(placeholder="IR name (default: file name)", id="ir-name")
                with Horizontal(classes="opt"):
                    yield Label("Length")
                    yield Choice(("20 ms", "40 ms"), id="ir-length")
                with Horizontal(classes="opt"):
                    yield Label("Align")
                    yield Choice(ir.ALIGNS, id="ir-align")
                with Horizontal(classes="buttons"):
                    yield Button("Upload", variant="primary", id="ir-upload")

    def show_irs(self, irs: list[Cab]):
        options = self.query_one("#ir-list", OptionList)
        keep = options.highlighted
        options.clear_options()
        options.add_options(Option(f"{c.index:3d}  {c.name}", id=str(c.index)) for c in irs)
        if irs:
            options.highlighted = min(keep or 0, len(irs) - 1)
        self.irs = irs

    def selected_ir(self) -> Cab | None:
        options = self.query_one("#ir-list", OptionList)
        return self.irs[options.highlighted] if options.highlighted is not None else None

    @property
    def folder(self) -> Path:
        return Path(self.query_one("#ir-dir", Input).value.strip() or ".")

    def on_input_submitted(self, event: Input.Submitted):
        if event.input.id == "ir-dir":
            if self.folder.is_dir():
                self.query_one(WavTree).path = self.folder
            else:
                self.app.notify(f"{self.folder} is not a folder", severity="error")

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected):
        self.file = Path(event.path)
        self.query_one("#ir-name", Input).value = self.file.stem[:ir.NAME_MAX]

    def on_value_changed(self, event: Value.Changed):
        event.stop()

    def on_button_pressed(self, event: Button.Pressed):
        {"ir-download": self.download, "ir-delete": self.delete,
         "ir-upload": self.upload}[event.button.id]()

    def download(self):
        entry = self.selected_ir()
        if entry is None:
            return
        path = self.folder / f"{entry.name}.wav"
        if path.exists():
            self.app.notify(f"{path} already exists", severity="error")
            return

        def job(t):
            name, samples = t.download_ir(entry.index)
            ir.write_wav(path, samples)
            return len(samples)
        self.app.run_job(job, lambda n: (self.app.notify(f"IR {entry.index} -> {path} ({n} samples)"),
                                         self.query_one(WavTree).reload()))

    def delete(self):
        entry = self.selected_ir()
        if entry is None:
            return
        later = len(self.irs) - 1 - entry.index
        warning = (f"\n\nThe {later} IRs after it move down one place, and presets using them "
                   "would then play the next IR." if later else "")

        def answered(yes: bool | None):
            if yes:
                self.app.run_job(lambda t: (t.delete_ir(entry.index, entry.name), t.read_irs())[1],
                                 self.app.set_irs)
        self.app.push_screen(ConfirmScreen(
            f"Delete IR {entry.index} \"{entry.name}\" from the unit? (flash write){warning}",
            "Delete"), answered)

    def upload(self):
        if self.file is None:
            self.app.notify("Select a WAV file in the tree first", severity="warning")
            return
        name = self.query_one("#ir-name", Input).value.strip() or self.file.stem
        if not 1 <= len(name) <= ir.NAME_MAX or not name.isascii() or not name.isprintable():
            self.app.notify(f"IR name must be 1..{ir.NAME_MAX} printable ASCII characters",
                            severity="error")
            return
        length = (20, 40)[self.query_one("#ir-length", Choice).value]
        align = ir.ALIGNS[self.query_one("#ir-align", Choice).value]
        path = self.file
        try:
            rate, values = ir.read_wav(path)
            samples = ir.prepare(values, rate, length, align)
        except (ValueError, OSError) as e:
            self.app.notify(str(e), title=path.name, severity="error")
            return

        def job(t):
            index = t.upload_ir(name, samples)
            return index, t.read_irs()

        def done(result):
            index, irs = result
            self.app.set_irs(irs)
            self.app.notify(f"Uploaded \"{name}\" as IR {index} (checked by reading it back)")
        self.app.run_job(job, done)


class SetupPane(TabPane):
    """Global settings: stored by the unit, not part of a preset."""

    DEFAULT_CSS = """
    SetupPane > VerticalScroll { padding: 1 2; }
    SetupPane .opt { height: 1; margin-bottom: 1; }
    SetupPane .opt Label { width: 20; color: $foreground; }
    SetupPane .opt Value { max-width: 60; }
    """
    LABELS = {"level": "Output level", "mute": "Mute", "spdif_freq": "S/PDIF rate (kHz)",
              "midi_cc": "MIDI CC", "midi_pc": "MIDI PC", "midi_channel": "MIDI channel",
              "output": "S/PDIF L/R"}
    PASSING = {"mute": 0, "midi_cc": 1, "midi_pc": 1}  # on/off: the value shown green

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            for o in SETUP_OPTIONS.values():
                with Horizontal(classes="opt"):
                    yield Label(self.LABELS.get(o.name, o.name))
                    if o.choices:
                        yield Choice(o.choices, o, inline=len(o.choices) <= 4,
                                     passing=self.PASSING.get(o.name))
                    else:
                        yield Slider(o.lo, o.hi, o, fmt=level_db if o.name == "level" else str)

    def show(self, setup: dict[int, int]):
        for w in self.query(Value):
            if w.tag.index in setup and f"setup-{w.tag.name}" not in self.app._debounce_timers:
                w.value = setup[w.tag.index]

    def on_value_changed(self, event: Value.Changed):
        event.stop()
        w = event.widget
        option: SetupOption = w.tag

        def write():
            self.app.run_job(lambda t: t.set_setup(option, w.value))
        if isinstance(w, Slider):
            self.app.debounce(f"setup-{option.name}", write)
        else:
            write()


class PresetFilesPane(TabPane):
    DEFAULT_CSS = """
    PresetFilesPane > VerticalScroll { padding: 0 2; }
    PresetFilesPane .field { margin-top: 1; color: $text-muted; width: 1fr; }
    PresetFilesPane .row { height: auto; margin-top: 1; }
    PresetFilesPane #presets-dir { width: 1fr; }
    PresetFilesPane #preset-files { height: 12; margin-top: 1; }
    PresetFilesPane Button { margin-right: 1; }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("Preset files folder", classes="field")
            with Horizontal(classes="row"):
                yield Input("presets", id="presets-dir")
                yield Button("Rescan", id="presets-rescan")
            yield OptionList(id="preset-files")
            with Horizontal(classes="row"):
                yield Button("Export current sound", variant="primary", id="export")
                yield Button("Load selected", id="load")
            yield Label("A preset file holds one sound, with its cab, mics and IRs by name so "
                        "it loads on another unit too; a file whose cab or IRs this unit "
                        "doesn't have is refused. Loading replaces the current sound without "
                        "storing it: press s to save it into a slot.", classes="field")

    def on_mount(self):
        self.rescan()

    @property
    def base(self) -> Path:
        return Path(self.query_one("#presets-dir", Input).value.strip() or ".")

    def rescan(self):
        files = sorted(self.base.glob("*.json")) if self.base.is_dir() else []
        options = self.query_one("#preset-files", OptionList)
        options.clear_options()
        for f in files:
            options.add_option(Option(f.name, id=str(f)))
        if files:
            options.highlighted = 0
        self.query_one("#load", Button).disabled = not files

    def on_input_submitted(self, event: Input.Submitted):
        self.rescan()

    def on_button_pressed(self, event: Button.Pressed):
        {"presets-rescan": self.rescan, "export": self.export,
         "load": self.load}[event.button.id]()

    def export(self):
        """Read the current sound, then save it as `<its name>.json` (the name
        comes from the unit: the app's name list isn't re-read on preset changes)."""
        def save(preset: dict):
            path = self.base / f"{preset['name']}.json"

            def write(yes: bool | None = True):
                if not yes:
                    return
                path.parent.mkdir(parents=True, exist_ok=True)
                presetfile.save_file(preset, path)
                self.app.notify(f"Exported \"{preset['name']}\" to {path}", title="Export")
                self.rescan()
            if path.exists():
                self.app.push_screen(ConfirmScreen(f"Replace {path}?", "Replace"), write)
            else:
                write()
        self.app.run_job(presetfile.export, save)

    def load(self):
        options = self.query_one("#preset-files", OptionList)
        if options.highlighted is None:
            return
        path = Path(options.get_option_at_index(options.highlighted).id)
        try:
            preset = presetfile.load_file(path)
        except (ValueError, OSError) as e:
            self.app.notify(str(e), severity="error")
            return
        if self.app.state.edited:
            self.app.notify("The current preset has unsaved edits, and loading would replace "
                            "them: save (s) or discard (d) first", severity="warning")
            return

        def done(names):
            self.app.set_names(names)
            self.app.notify(f"Loaded \"{preset['name']}\" (unsaved: s stores it)", title="Load")
        self.app.run_job(lambda t: (presetfile.import_preset(t, preset), t.read_names())[1], done)


def find_backups(base: Path) -> list[Path]:
    return sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if base.is_dir() else []


class BackupPane(TabPane):
    DEFAULT_CSS = """
    BackupPane > VerticalScroll { padding: 0 2; }
    BackupPane .field { margin-top: 1; color: $text-muted; width: 1fr; }
    BackupPane .row { height: auto; margin-top: 1; }
    BackupPane #backup-dir { width: 1fr; }
    BackupPane #backup-list { height: 12; margin-top: 1; }
    BackupPane Button { margin-right: 1; }
    BackupPane ProgressBar { margin-top: 1; }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("Backups folder", classes="field")
            with Horizontal(classes="row"):
                yield Input("backup", id="backup-dir")
                yield Button("Rescan", id="rescan")
            yield OptionList(id="backup-list")
            with Horizontal(classes="row"):
                yield Button("Back up now", variant="primary", id="backup-now")
                yield Button("Restore selected...", variant="error", id="restore")
            yield ProgressBar(id="progress", show_eta=False)
            yield Label("A backup holds the 128 presets and the global settings (not the cab "
                        "and IR data). Reading presets selects each one in turn, so it refuses "
                        "to run while the current preset has unsaved edits. Restore only stores "
                        "the presets that differ, after showing them.", classes="field")

    def on_mount(self):
        self.rescan()

    @property
    def base(self) -> Path:
        return Path(self.query_one("#backup-dir", Input).value.strip() or ".")

    def rescan(self):
        files = find_backups(self.base)
        options = self.query_one("#backup-list", OptionList)
        options.clear_options()
        for f in files:
            when = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            options.add_option(Option(f"{f.name}  [dim]{when}[/]", id=str(f)))
        if files:
            options.highlighted = 0
        self.query_one("#restore", Button).disabled = not files

    def on_input_submitted(self, event: Input.Submitted):
        self.rescan()

    def on_button_pressed(self, event: Button.Pressed):
        {"rescan": self.rescan, "backup-now": self.backup,
         "restore": self.restore}[event.button.id]()

    def backup(self):
        path = self.base / f"{datetime.now():%Y-%m-%d_%H%M%S}.json"
        progress = progress_to(self.query_one(ProgressBar))

        def job(t):
            state = backup.read_state(t, progress)
            path.parent.mkdir(parents=True, exist_ok=True)
            backup.save_file(state, path)
            return sum(p["name"] != "USER" for p in state["presets"])

        def done(used: int):
            self.app.notify(f"Saved {used} presets and the global settings to {path}", title="Backup")
            self.rescan()
        self.app.run_job(job, done)

    def restore(self):
        options = self.query_one("#backup-list", OptionList)
        if options.highlighted is None:
            return
        path = Path(options.get_option_at_index(options.highlighted).id)
        try:
            saved = backup.load_file(path)
        except (ValueError, OSError) as e:
            self.app.notify(str(e), severity="error")
            return
        bar = self.query_one(ProgressBar)

        def plan_job(t):
            current = backup.read_state(t, progress_to(bar))
            return backup.plan_restore(saved, current), current

        def planned(result):
            plan, current = result
            if not plan:
                self.app.notify("Nothing to restore: the unit matches the backup", title="Restore")
                return
            names = {p["index"]: p["name"] for p in current["presets"]}
            lines = [f"setup {k}: {SETUP_OPTIONS[k].decode(current['setup'][k])} -> "
                     f"{SETUP_OPTIONS[k].decode(v)}" for k, v in plan.setup.items()]
            lines += [f"{e['label']}: \"{names[e['index']]}\" -> cleared" for e in plan.clears]
            lines += [f"{e['label']}: \"{names[e['index']]}\" -> \"{e['name']}\"" for e in plan.presets]
            lines += [f"skipped {s}" for s in plan.skipped]
            shown = "\n".join(lines[:15]) + (f"\n... ({len(lines) - 15} more)" if len(lines) > 15 else "")
            serial = ("" if saved["serial"] == current["serial"] else
                      f"\n\nNote: backup from unit {saved['serial']}: cab/IR numbers may not match.")

            def answered(yes: bool | None):
                if yes:
                    self.app.run_job(lambda t: backup.apply_restore(t, plan, progress_to(bar)),
                                     lambda _: (self.app.notify("Restored", title="Restore"),
                                                self.app.action_reload()))
            self.app.push_screen(ConfirmScreen(
                f"Restore {path.name}? {len(plan.presets) + len(plan.clears)} presets would be "
                f"stored or cleared (flash writes, ~3 s each):\n\n{shown}{serial}", "Restore"), answered)
        self.app.run_job(plan_job, planned)
