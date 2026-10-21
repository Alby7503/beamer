"""Qt update notice with synthetic settings; run offscreen from a Claude session."""

import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2])]

from PySide6.QtWidgets import QApplication
import app_config
import kvm_bridge_win as kvm
import theme


FOUND = ("1.5.11", "https://github.com/kalkman-code/beamer/releases/tag/v1.5.11")


class UpdateNoticeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.init_fonts()

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "settings.json"
        app_config.write_settings(self.path, app_config.load_settings(self.path))
        for blocker in (
            patch.object(threading.Thread, "start", side_effect=AssertionError("service started")),
            patch.object(kvm.WindowsApplication, "_make_pairing", return_value=None),
            patch.object(kvm.autostart_win, "installed_exe", return_value=None),
            patch("core.locale.is_us_region", return_value=False),
        ):
            blocker.start()
            self.addCleanup(blocker.stop)
        self.window = self.build()

    def build(self):
        window = kvm.WindowsApplication(self.path)
        window.refresh_timer.stop()
        window.full_screen_timer.stop()
        self.addCleanup(window.deleteLater)
        return window

    def test_dismissal_survives_rebuilding_and_other_settings_changes(self):
        self.window._on_update(FOUND)
        self.assertFalse(self.window.update_notice.isHidden())
        self.assertFalse(self.window.isVisible())
        self.window.update_dismiss.click()
        self.assertTrue(self.window.update_notice.isHidden())
        self.assertTrue(self.window._persist())
        self.assertEqual(app_config.load_settings(self.path)["dismissed_update_version"], "1.5.11")
        rebuilt = self.build()
        rebuilt._on_update(FOUND)
        self.assertTrue(rebuilt.update_notice.isHidden())
        rebuilt._on_update(("1.5.12", FOUND[1]))
        self.assertFalse(rebuilt.update_notice.isHidden())

    def test_switching_off_hides_the_notice_and_late_answers(self):
        self.window._on_update(FOUND)
        self.window._set_check_updates(False)
        self.assertFalse(self.window._config.check_updates)
        self.assertTrue(self.window.update_notice.isHidden())
        self.window._on_update(FOUND)
        self.assertTrue(self.window.update_notice.isHidden())
        self.assertIsNone(self.window._update)

    def test_flatpak_has_store_guidance_and_no_download_action(self):
        with patch.object(kvm.portal, "sandboxed", return_value=True):
            self.window._on_update(FOUND)
        self.assertIn("software store", self.window.update_notice_text.text())
        self.assertTrue(self.window.update_button.isHidden())
        self.assertFalse(self.window.update_notes.isHidden())

    def test_linux_start_checks_for_updates_without_waiting_for_a_toggle(self):
        events = []
        owner = SimpleNamespace(update_checker=SimpleNamespace(start=lambda: events.append("check")),
                                _listen=lambda: events.append("listen"))
        with patch.object(kvm.sys, "platform", "linux"), patch.object(kvm.sys, "frozen", False, create=True):
            kvm.WindowsApplication.start(owner)
        self.assertEqual(events, ["check", "listen"])

    def test_unreadable_settings_do_not_block_opening_the_panel(self):
        self.window._on_update(FOUND)
        with patch.object(app_config, "load_settings", side_effect=OSError("locked")):
            self.window._refresh_update_notice()
        self.assertTrue(self.window.update_notice.isHidden())

    def test_whats_new_opens_the_release_only_on_a_click(self):
        with patch.object(kvm.QDesktopServices, "openUrl") as opened:
            self.window._on_update(FOUND)
            self.assertEqual(opened.call_count, 0)
            self.window.update_notes.click()
        self.assertEqual(opened.call_args.args[0].toString(), FOUND[1])

    def test_failed_save_keeps_the_notice_available(self):
        self.window._on_update(FOUND)
        with patch.object(app_config, "write_settings", side_effect=OSError("read only")):
            self.window.update_dismiss.click()
        self.assertFalse(self.window.update_notice.isHidden())
