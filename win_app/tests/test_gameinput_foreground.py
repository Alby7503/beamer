"""UIPI refusals are reported without runtime service recovery."""

import ctypes
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import desktop_win
import input_injector


class FakeUser32:
    def __init__(self, classes, foreground=None, set_results=()):
        self.classes = classes
        self.foreground = foreground
        self.set_results = list(set_results)
        self.set_calls = []

    def GetForegroundWindow(self):
        return self.foreground

    def GetClassNameW(self, hwnd, buffer, size):
        buffer.value = self.classes.get(hwnd, "")
        return len(buffer.value)

    def SetCursorPos(self, x, y):
        self.set_calls.append((x, y))
        return self.set_results.pop(0) if self.set_results else 1


GAMEINPUT, EXPLORER = 0x5403CC, 0xD0002E
CLASSES = {GAMEINPUT: input_injector.GAMEINPUT_WINDOW_CLASS, EXPLORER: "CabinetWClass"}


class GameInputForegroundTests(unittest.TestCase):
    def setUp(self):
        self.user32 = FakeUser32(CLASSES)
        self.clock = [1000.0]
        patches = [
            mock.patch.object(input_injector, "user32", self.user32),
            mock.patch.object(desktop_win, "user32", self.user32),
            mock.patch.object(input_injector.time, "monotonic", lambda: self.clock[0]),
            mock.patch.object(ctypes, "get_last_error", create=True, return_value=0),
            mock.patch.object(input_injector, "_seen_foreground", [None]),
            mock.patch.object(input_injector, "_problem_at", float("-inf")),
            mock.patch.object(desktop_win, "_refused_under", [None]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_gameinput_foreground_reports_the_limit_without_restarting_a_service(self):
        self.user32.foreground = GAMEINPUT
        notice = mock.Mock()
        with mock.patch.object(input_injector, "on_problem", notice), \
                mock.patch.object(subprocess, "run") as run, \
                mock.patch.object(subprocess, "Popen") as popen, \
                mock.patch.object(os, "system") as system:
            input_injector.watch_foreground()

        notice.assert_called_once()
        self.assertIn("administrator windows locally", notice.call_args.args[0])
        run.assert_not_called()
        popen.assert_not_called()
        system.assert_not_called()

    def test_input_problem_notice_is_throttled_for_thirty_seconds(self):
        notices = []
        with mock.patch.object(input_injector, "on_problem", notices.append):
            input_injector.report_input_problem()
            self.clock[0] += 29
            input_injector.report_input_problem()
            self.clock[0] += 1
            input_injector.report_input_problem()

        self.assertEqual(len(notices), 2)
        self.assertIn("click a normal window on this PC", notices[0])

    def test_ordinary_foreground_does_not_report_a_problem(self):
        self.user32.foreground = EXPLORER
        notice = mock.Mock()
        with mock.patch.object(input_injector, "on_problem", notice):
            input_injector.watch_foreground()
        notice.assert_not_called()

    def test_successful_cursor_placement_runs_the_arrival_callback(self):
        self.user32.foreground = EXPLORER
        placed = mock.Mock()

        desktop_win.set_cursor_position_with_callback(
            1818, 2159, placed, place_if_current=lambda action: action(),
        )

        placed.assert_called_once_with()
        self.assertEqual(self.user32.set_calls, [(1818, 2159)])

    def test_failed_cursor_placement_does_not_claim_arrival_and_warns_once_per_blocker(self):
        self.user32.foreground = EXPLORER
        self.user32.set_results = [0, 0, 1, 0]
        placed = mock.Mock()
        notice = mock.Mock()
        with mock.patch.object(input_injector, "report_input_problem", notice), \
                self.assertLogs(desktop_win.LOGGER, "WARNING") as logs:
            for _ in range(4):
                desktop_win.set_cursor_position_with_callback(10, 20, placed)

        placed.assert_called_once_with()
        self.assertEqual(notice.call_count, 2)
        self.assertEqual(len(logs.records), 2)
        self.assertIn("CabinetWClass", logs.output[0])

    def test_stale_visit_neither_moves_the_cursor_nor_calls_arrival(self):
        placed = mock.Mock()
        current = [False]

        desktop_win.set_cursor_position_with_callback(
            1818, 2159, placed,
            place_if_current=lambda action: action() if current[0] else None,
        )

        self.assertEqual(self.user32.set_calls, [])
        placed.assert_not_called()

    def test_refused_placement_does_not_call_arrival_even_for_a_current_visit(self):
        self.user32.foreground = EXPLORER
        self.user32.set_results = [0]
        placed = mock.Mock()
        with mock.patch.object(input_injector, "report_input_problem"):
            desktop_win.set_cursor_position_with_callback(
                1818, 2159, placed,
                place_if_current=lambda action: action(),
            )

        self.assertEqual(self.user32.set_calls, [(1818, 2159)])
        placed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
