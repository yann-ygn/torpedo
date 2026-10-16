"""Textual interface: `torpedo tui` (or `torpedo --fake tui` without a unit)."""


def run(fake: bool = False):
    from ..device import Torpedo
    from .app import TorpedoApp

    if fake:
        from ..fake import FakeHid
        dev = FakeHid()
        TorpedoApp(lambda: Torpedo(dev), "fake device").run()
    else:
        TorpedoApp(Torpedo).run()
