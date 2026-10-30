"""A window dragged against the edge: which one it is, and what it shows.

A browser's window crosses as its page, opened in the PC's own browser; a
document's as its file, opened there; any other window streams live (stream_mac).
All of it reads Accessibility, which Beamer already holds for input, and a
browser's address comes from AppleScript, which asks once for Automation.
"""

import os
import subprocess

try:
    import AppKit
    import ApplicationServices as AX
    import Quartz
except ImportError:  # pragma: no cover - only off a Mac
    AppKit = AX = Quartz = None

# The strip along a window's top, in points, that a pointer must be in for its drag to be the
# window's own: a title bar, or a toolbar that moves the window as Safari's does.
TITLE_BAND_PT = 52
OSASCRIPT_TIMEOUT_SECONDS = 3.0
AX_TIMEOUT_SECONDS = 0.2
SIZE_SLOP_PT = 4
# Browsers whose front tab's address AppleScript can read, by bundle identifier.
SAFARI_LIKE = {"com.apple.Safari", "com.apple.SafariTechnologyPreview"}
CHROME_LIKE = {
    "com.google.Chrome", "com.google.Chrome.canary", "com.brave.Browser", "com.microsoft.edgemac",
    "company.thebrowser.Browser", "com.vivaldi.Vivaldi", "com.operasoftware.Opera",
}


def in_title_band(frame, x, y) -> bool:
    fx, fy, fw, _fh = frame
    return fx <= x <= fx + fw and fy <= y <= fy + TITLE_BAND_PT


def _attribute(element, name):
    error, value = AX.AXUIElementCopyAttributeValue(element, name, None)
    return value if error == 0 else None


def _frame(window):
    position, size = _attribute(window, "AXPosition"), _attribute(window, "AXSize")
    if position is None or size is None:
        return None
    _ok, point = AX.AXValueGetValue(position, AX.kAXValueCGPointType, None)
    _ok, extent = AX.AXValueGetValue(size, AX.kAXValueCGSizeType, None)
    return (float(point.x), float(point.y), float(extent.width), float(extent.height))


def dragged_window(x, y):
    """The front app's focused window when the pointer at (x, y) is in its title band, which is
    what a window being dragged looks like, as a dict; None otherwise. Never raises."""
    if AX is None:
        return None
    try:
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        pid = int(app.processIdentifier())
        if pid == os.getpid():
            # Beamer's own windows stay: AppKit only lets its main thread move them, and this runs
            # on the event tap's (a drag of the Beamer window once crashed it that way).
            return None
        element = AX.AXUIElementCreateApplication(pid)
        # This runs on the event tap's thread: an app that does not answer must not stall input.
        AX.AXUIElementSetMessagingTimeout(element, AX_TIMEOUT_SECONDS)
        window = _attribute(element, "AXFocusedWindow")
        frame = window is not None and _frame(window)
        if not frame or not in_title_band(frame, x, y):
            return None
        return {
            "pid": pid,
            "bundle": str(app.bundleIdentifier() or ""),
            "app": str(app.localizedName() or ""),
            "title": str(_attribute(window, "AXTitle") or app.localizedName() or "Window"),
            "frame": frame,
            "window": window,
        }
    except Exception:
        return None


def _osascript(script):
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=OSASCRIPT_TIMEOUT_SECONDS)
    return out.stdout.strip() if out.returncode == 0 else ""


def page_url(window):
    """The address of a browser window's front tab, or None."""
    bundle = window["bundle"]
    if bundle in SAFARI_LIKE:
        script = f'tell application id "{bundle}" to get URL of front document'
    elif bundle in CHROME_LIKE:
        script = f'tell application id "{bundle}" to get URL of active tab of front window'
    else:
        return None
    try:
        url = _osascript(script)
    except (OSError, subprocess.SubprocessError):
        return None
    return url if url.startswith(("http://", "https://")) else None


def document_path(window):
    """The file a window shows, from its AXDocument, or None."""
    try:
        document = _attribute(window["window"], "AXDocument")
        if not document:
            return None
        path = AppKit.NSURL.URLWithString_(str(document)).path()
        # Files only: a Finder window's document is its folder, which could be a whole disk.
        return str(path) if path and os.path.isfile(str(path)) else None
    except Exception:
        return None


def minimise(window) -> None:
    try:
        AX.AXUIElementSetAttributeValue(window["window"], "AXMinimized", True)
    except Exception:
        pass


def move_on_screen(window, bounds) -> None:
    """Puts a window that hangs off the edge it was dragged against back inside `bounds`
    (left, top, right, bottom), so the clicks a stream sends it land on it."""
    x, y, w, h = window["frame"]
    left, top, right, bottom = bounds
    nx = min(max(x, left), max(left, right - w))
    ny = min(max(y, top), max(top, bottom - h))
    if (nx, ny) == (x, y):
        return
    try:
        AX.AXUIElementSetAttributeValue(
            window["window"], "AXPosition", AX.AXValueCreate(AX.kAXValueCGPointType, Quartz.CGPoint(nx, ny))
        )
        window["frame"] = (nx, ny, w, h)
    except Exception:
        pass


def window_number(window):
    """The window server's number for the window, for capturing it: of the app's on-screen windows
    of the dragged one's size, the nearest to where it is now, or None. Size, not place, picks it:
    ending the drag and putting the window back on screen move it, which left a match by frame
    with none at all."""
    frame = window.get("window") is not None and _frame(window["window"]) or window["frame"]
    x, y, w, h = frame
    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID
    ) or ()
    best = None
    for info in infos:
        b = info.get("kCGWindowBounds") or {}
        if int(info.get("kCGWindowOwnerPID", -1)) != window["pid"] or info.get("kCGWindowLayer") != 0:
            continue
        if abs(b.get("Width", 0) - w) > SIZE_SLOP_PT or abs(b.get("Height", 0) - h) > SIZE_SLOP_PT:
            continue
        distance = abs(b.get("X", 0) - x) + abs(b.get("Y", 0) - y)
        if best is None or distance < best[0]:
            best = (distance, int(info["kCGWindowNumber"]))
    return best[1] if best else None
