import os
import sys
from pathlib import Path

from setuptools import setup


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


VERSION = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
BUILD = os.environ.get("BEAMER_BUILD", "1")


setup(
    name="Beamer",
    version=VERSION,
    app=["kvm_bridge_app.py"],
    py_modules=[
        "bridge",
        "clipboard_mac",
        "config",
        "crossing",
        "desktop_mac",
        "effects",
        "effects_overlay",
        "file_drag",
        "file_transfer",
        "fx_ink",
        "fx_instrument",
        "fx_membrane",
        "fx_sparks",
        "fx_warp",
        "gestures",
        "handoff",
        "ignored",
        "input_injector_mac",
        "key_codes",
        "keyboard_layout",
        "link_state",
        "media_keys",
        "notch_beam",
        "notch_island",
        "pages",
        "previews",
        "no_unlock",
        "pairing",
        "protocol",
        "receiver",
        "return_edge",
        "settings_store",
        "stream_mac",
        "stream_view_mac",
        "theme",
        "tokens",
        "wake",
        "wol",
        "widgets",
        "windows_input",
    ],
    # No Mac ships Hanken Grotesk or B612 Mono. macOS registers anything under
    # ATSApplicationFontsPath before the app runs, so the bundle carries the same files the
    # Windows receiver does rather than a second copy of them.
    data_files=[(
        "Fonts",
        [
            "../win_app/assets/HankenGrotesk-Variable.ttf",
            "../win_app/assets/B612Mono-Regular.ttf",
            "../win_app/assets/B612Mono-Bold.ttf",
        ],
    ), ("", ["../VERSION"])],
    options={
        "py2app": {
            "argv_emulation": False,
            # cryptography ships a compiled _rust extension plus cffi; py2app's own recipe pulls
            # both in, but naming the package keeps its data files and submodules in the bundle.
            # PyNaCl (nacl) is the same shape: a compiled _sodium extension reached through cffi.
            "packages": ["rumps", "cryptography", "cffi", "nacl"],
            # effects.py imports its fx_* modules by name at first use, which py2app's import scan
            # cannot see, so they are named here or the bundle ships without a single effect.
            "includes": [
                "objc", "AppKit", "ApplicationServices", "Quartz", "CoreMedia", "ScreenCaptureKit",
                "fx_ink", "fx_instrument", "fx_membrane", "fx_sparks", "fx_warp",
            ],
            "iconfile": "../Beamer.icns",
            "plist": {
                "CFBundleDisplayName": "Beamer",
                "CFBundleIdentifier": "uk.co.kalkman.beamer",
                "CFBundleName": "Beamer",
                "CFBundleShortVersionString": VERSION,
                "CFBundleVersion": BUILD,
                "LSMinimumSystemVersion": "13.0",
                "LSUIElement": False,
                "NSHighResolutionCapable": True,
                "NSPrincipalClass": "NSApplication",
                "ATSApplicationFontsPath": "Fonts",
                "NSHumanReadableCopyright": "Copyright 2026 Toby Kalkman",
                "NSAppleEventsUsageDescription": "Beamer asks your browser for the page a window shows, and Finder for the folder you drop files on, when you drag them across to the PC.",
                "NSLocalNetworkUsageDescription": "Beamer needs to reach the Windows PC on your local network to forward keyboard and mouse input.",
            },
        }
    },
)
