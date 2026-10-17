"""Serve the emulator (torpedo.fake) on a Unix SEQPACKET socket, so the C side
(mpc/) can be tested against it: the C code opens `unix:<path>` instead of the
hidraw node. Each OUT report gets the one IN report the unit would send."""

import os
import shutil
import socket
import tempfile
import threading

from torpedo.fake import FakeHid


class FakeBridge:
    def __init__(self, fake: FakeHid | None = None):
        self.fake = fake or FakeHid()
        self.lock = threading.Lock()  # hold it to call fake.panel_* while a client is connected
        self.errors: list[Exception] = []  # unobserved commands, malformed reports
        self._dir = tempfile.mkdtemp(prefix="tp")  # sun_path is short: not under pytest's tmp_path
        self.path = os.path.join(self._dir, "s")
        self.url = "unix:" + self.path
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self._sock.bind(self.path)
        self._sock.listen()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return  # closed
            with conn:
                while data := conn.recv(256):
                    try:
                        with self.lock:
                            self.fake.write(list(data))
                            reply = self.fake.read(len(data) - 1)
                    except Exception as e:
                        self.errors.append(e)
                        break
                    if reply:
                        conn.send(bytes(reply))

    def close(self):
        try:
            self._sock.shutdown(socket.SHUT_RDWR)  # wakes accept()
        except OSError:
            pass
        self._sock.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
