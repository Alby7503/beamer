import unittest

import stream_win


class StreamWinTests(unittest.TestCase):
    def test_an_address_bar_reads_as_a_url_to_open(self):
        self.assertEqual(stream_win.as_url("example.com/a?b=1"), "https://example.com/a?b=1")
        self.assertEqual(stream_win.as_url("http://intranet.local/x"), "http://intranet.local/x")
        for text in ("", "search words here", "file:///C:/x", "localhost", "javascript:alert(1)"):
            self.assertIsNone(stream_win.as_url(text), text)

    def test_only_the_title_band_counts_as_dragging_the_window(self):
        rect = (100, 50, 800, 600)
        self.assertTrue(stream_win.in_title_band(rect, 400, 60))
        self.assertFalse(stream_win.in_title_band(rect, 400, 50 + stream_win.TITLE_BAND_PX))
        self.assertFalse(stream_win.in_title_band(rect, 950, 60))

    def test_the_macs_fractions_land_inside_the_window(self):
        streamer = stream_win.Streamer(None, {"hwnd": 1, "title": "", "rect": (100, 50, 401, 201)}, None, None)
        self.assertEqual(streamer.point({"x": 0.5, "y": 2.0}), (300, 250))
        self.assertEqual(streamer.handle({"kind": "close"}), "close")


if __name__ == "__main__":
    unittest.main()
