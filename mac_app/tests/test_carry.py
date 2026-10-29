"""Files dropped on a folder, and a window streamed, end to end over a real receiver.

Screen capture and posting events are faked: the first needs Screen Recording,
and the second would click on whatever is under this Mac's pointer.
"""

import logging
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import file_transfer
import protocol
import receiver
import stream_mac


def _port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class CarryTests(unittest.TestCase):
    def _server(self, **callbacks):
        server = receiver.ReceiverServer(lambda *a: None, clipboard=types.SimpleNamespace(forget_sync=lambda: None), **callbacks)
        port = _port()
        server.start(types.SimpleNamespace(port=port, auth_token="tok"))
        self.addCleanup(server.stop)
        deadline = time.monotonic() + 3
        while not server.listening and time.monotonic() < deadline:
            time.sleep(0.02)
        return server, ("127.0.0.1", port, "tok")

    def test_files_dropped_on_a_folder_are_moved_into_it(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as landing, tempfile.TemporaryDirectory() as target:
            with open(os.path.join(src, "note.txt"), "w") as f:
                f.write("x")
            shown = []
            server, endpoint = self._server(
                files_folder=landing, files_callback=lambda paths, then: shown.append((paths, then)), drop_target=lambda: target
            )
            server._dropped()  # the button came up just before the files' connection did
            file_transfer.deliver(socket.create_connection, endpoint, [os.path.join(src, "note.txt")])
            deadline = time.monotonic() + 3
            while not shown and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(shown, [([os.path.join(target, "note.txt")], file_transfer.THEN_REVEAL)])

    def test_a_window_streams_and_the_pcs_close_stops_it(self):
        frames = []

        def viewer(connection, session, hello):
            self.assertEqual(hello["title"], "Notes")
            for _ in range(3):
                frames.append(protocol.recv_bytes(connection, session))
            protocol.send_msg(connection, session, {"type": stream_mac.MSG_INPUT, "data": {"kind": "close"}})
            time.sleep(0.3)

        _server, endpoint = self._server(stream_callback=viewer)
        window = {"pid": 1, "title": "Notes", "frame": (10.0, 20.0, 400.0, 300.0)}
        streamer = stream_mac.Streamer(endpoint, window, socket.create_connection, logging.getLogger("t"), lambda *a: None)
        fakes = {
            "capture": lambda number: b"\xff\xd8jpeg",
            "bounds_of": lambda number: (10.0, 20.0, 400.0, 300.0),
            "SCK": None,
        }
        originals = {name: getattr(stream_mac, name) for name in fakes}
        preflight = stream_mac.Quartz.CGPreflightScreenCaptureAccess
        window_number = stream_mac.handoff.window_number
        try:
            for name, fake in fakes.items():
                setattr(stream_mac, name, fake)
            stream_mac.Quartz.CGPreflightScreenCaptureAccess = lambda: True
            stream_mac.handoff.window_number = lambda w: 7
            streamer._run()
        finally:
            for name, original in originals.items():
                setattr(stream_mac, name, original)
            stream_mac.Quartz.CGPreflightScreenCaptureAccess = preflight
            stream_mac.handoff.window_number = window_number
        self.assertEqual(frames, [b"\xff\xd8jpeg"] * 3)
        self.assertTrue(streamer.stop.is_set())

    def test_the_pcs_fractions_land_inside_the_window(self):
        streamer = stream_mac.Streamer(None, {"pid": 1, "title": "", "frame": (100.0, 50.0, 400.0, 200.0)}, None, None, None)
        point = streamer.point({"x": 0.5, "y": 2.0})
        self.assertEqual((point.x, point.y), (300.0, 250.0))
        self.assertEqual(streamer.handle({"kind": "close"}), "close")


if __name__ == "__main__":
    unittest.main()
