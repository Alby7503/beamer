"""Native update notice, built offscreen with synthetic settings; Claude pass only."""

from types import SimpleNamespace
from unittest import mock

import kvm_bridge_app as kvm
import settings_store
import test_crossing_page_machines as fixture


FOUND = ("1.5.11", "https://github.com/kalkman-code/beamer/releases/tag/v1.5.11")


class UpdateNoticeTest(fixture.Page):
    def test_dismissal_survives_rebuilding_and_other_settings_changes(self):
        window = self.build([])
        window.show_update(FOUND)
        self.assertFalse(window.update_notice.view.isHidden())
        self.assertFalse(window.window.isVisible())
        window.dismissUpdate_(None)
        self.assertTrue(window.update_notice.view.isHidden())
        self.assertEqual(self.store.current()["dismissed_update_version"], "1.5.11")
        cfg = self.store.save(settings_store.config_to_raw(self.controller.cfg))
        self.controller.update_config(cfg)
        self.store._cache = None
        window = self.build([])
        window.show_update(FOUND)
        self.assertTrue(window.update_notice.view.isHidden())
        window.show_update(("1.5.12", FOUND[1]))
        self.assertFalse(window.update_notice.view.isHidden())

    def test_switching_off_hides_the_notice_and_late_answers(self):
        window = self.build([])
        window.show_update(FOUND)
        window._set_check_updates(False)
        self.assertFalse(self.controller.cfg.check_updates)
        self.assertTrue(window.update_notice.view.isHidden())
        window.show_update(FOUND)
        self.assertTrue(window.update_notice.view.isHidden())
        self.assertIsNone(window.update_url)

    def test_whats_new_opens_the_release_only_on_a_click(self):
        window = self.build([])
        workspace = mock.Mock()
        with mock.patch.object(kvm.AppKit, "NSWorkspace", SimpleNamespace(sharedWorkspace=lambda: workspace)):
            window.show_update(FOUND)
            self.assertEqual(workspace.openURL_.call_count, 0)
            window.update_notes.view.performClick_(None)
        self.assertEqual(workspace.openURL_.call_args.args[0].absoluteString(), FOUND[1])

    def test_failed_save_keeps_the_notice_available(self):
        window = self.build([])
        window.show_update(FOUND)
        with mock.patch.object(self.store, "save", side_effect=settings_store.SettingsError("read only")):
            window.dismissUpdate_(None)
        self.assertFalse(window.update_notice.view.isHidden())
