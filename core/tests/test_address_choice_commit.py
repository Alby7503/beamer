"""Explicit address commits use the pin signal, including an unchanged address."""

from types import SimpleNamespace
import unittest
from unittest import mock

from core.tests.app_methods import load_methods


class AddressCommitTests(unittest.TestCase):
    def test_mac_connection_commit_marks_the_same_address_as_explicit(self):
        cfg = SimpleNamespace(host="192.0.2.10")
        store = mock.Mock()
        methods = load_methods("mac_app/kvm_bridge_app.py", "ControlWindow", ("connect_",),
                               {"config_to_raw": lambda _: {"host": cfg.host}, "tr": lambda text: text,
                                "SettingsError": ValueError})
        window = methods()
        window.controller = SimpleNamespace(cfg=cfg, update_config=mock.Mock())
        window.settings_store = store
        window._host_text = lambda: cfg.host
        window.port_field = SimpleNamespace(stringValue=lambda: "24820")
        window._say = mock.Mock()
        window.connect_(None)
        self.assertEqual(store.save.call_args.kwargs, {"pin_address": True})
        self.assertNotIn("pc_name", store.save.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
