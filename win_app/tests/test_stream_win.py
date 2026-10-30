import unittest

import stream_win


class StreamWinTests(unittest.TestCase):
    def test_an_address_bar_reads_as_a_url_to_open(self):
        self.assertEqual(stream_win.as_url("example.com/a?b=1"), "https://example.com/a?b=1")
        self.assertEqual(stream_win.as_url("http://intranet.local/x"), "http://intranet.local/x")
        self.assertEqual(stream_win.as_url("https://example.com:8443/x"), "https://example.com:8443/x")
        for text in ("", "search words here", "file:///C:/x", "localhost", "javascript:alert(1)", "user@evil.com/x", "https://a@b.com/"):
            self.assertIsNone(stream_win.as_url(text), text)

    def test_a_window_counts_as_moving_from_windows_own_events_and_never_while_resized(self):
        start, end = stream_win.EVENT_SYSTEM_MOVESIZESTART, stream_win.EVENT_SYSTEM_MOVESIZEEND
        stream_win.on_move_event(start, 42, 2)  # HTCAPTION
        self.assertEqual(stream_win._moving["hwnd"], 42)
        stream_win.on_move_event(end, 7, None)
        self.assertEqual(stream_win._moving["hwnd"], 42, "another window's end leaves this move alone")
        stream_win.on_move_event(end, 42, None)
        self.assertIsNone(stream_win._moving["hwnd"])
        stream_win.on_move_event(start, 42, 11)  # HTRIGHT: a resize
        self.assertIsNone(stream_win._moving["hwnd"])
        stream_win.on_move_event(start, 42, None)  # no answer: taken as a move
        self.assertEqual(stream_win._moving["hwnd"], 42)
        stream_win.on_move_event(end, 42, None)

    def test_the_macs_fractions_land_inside_the_window(self):
        streamer = stream_win.Streamer(None, {"hwnd": 1, "title": "", "rect": (100, 50, 401, 201)}, None, None)
        self.assertEqual(streamer.point({"x": 0.5, "y": 2.0}), (300, 250))
        self.assertEqual(streamer.handle({"kind": "close"}), "close")


if __name__ == "__main__":
    unittest.main()
