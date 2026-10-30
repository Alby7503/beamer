"""What is being dragged on this Mac, read from the system's drag pasteboard.

Any app can read it while a drag is under way. It keeps the last drag's contents
after the drag ends, so a drag is only taken to be of files when the pasteboard
changed after the button went down: see KVMController._dragged_files.
"""

import os
import subprocess

import file_transfer

try:
    import AppKit
except ImportError:  # pragma: no cover - only off a Mac
    AppKit = None


def _pasteboard():
    return AppKit.NSPasteboard.pasteboardWithName_(AppKit.NSPasteboardNameDrag)


def change_count():
    """The drag pasteboard's change count, or None when it cannot be read."""
    if AppKit is None:
        return None
    try:
        return int(_pasteboard().changeCount())
    except Exception:
        return None


def file_paths():
    """The paths of the files on the drag pasteboard, [] for none."""
    if AppKit is None:
        return []
    try:
        urls = _pasteboard().readObjectsForClasses_options_(
            [AppKit.NSURL], {AppKit.NSPasteboardURLReadingFileURLsOnlyKey: True}
        )
        return [str(url.path()) for url in urls or ()]
    except Exception:
        return []


# -- files arriving from the PC ---------------------------------------------------------------

FOLDER = os.path.expanduser("~/Downloads/Beamer")
_FINDER_FOLDER_AT = '''
tell application "Finder"
    repeat with w in Finder windows
        set b to bounds of w
        if (item 1 of b) <= %d and %d <= (item 3 of b) and (item 2 of b) <= %d and %d <= (item 4 of b) then
            return POSIX path of (target of w as alias)
        end if
    end repeat
end tell
'''


def drop_folder():
    """The folder the pointer is over: a Finder window's, the Desktop's when no window is under
    it, or None over any other app's window, which takes nothing."""
    import Quartz

    point = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    x, y = int(point.x), int(point.y)
    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID
    ) or ()
    for info in infos:
        b = info.get("kCGWindowBounds") or {}
        if info.get("kCGWindowLayer") != 0 or not (b.get("X", 0) <= x < b.get("X", 0) + b.get("Width", 0) and b.get("Y", 0) <= y < b.get("Y", 0) + b.get("Height", 0)):
            continue
        if info.get("kCGWindowOwnerName") != "Finder":
            return None
        out = subprocess.run(["osascript", "-e", _FINDER_FOLDER_AT % (x, x, y, y)], capture_output=True, text=True, timeout=3)
        path = out.stdout.strip()
        return path if out.returncode == 0 and os.path.isdir(path) else None
    return os.path.expanduser("~/Desktop")


def show(paths, then):
    """Opens files that arrived to be opened, and shows the rest in Finder."""
    if AppKit is None:
        return
    workspace = AppKit.NSWorkspace.sharedWorkspace()
    urls = [AppKit.NSURL.fileURLWithPath_(path) for path in paths]
    if then == "open" and all(file_transfer.safe_to_open(path) for path in paths):
        for url in urls:
            workspace.openURL_(url)
    else:
        workspace.activateFileViewerSelectingURLs_(urls)


def open_url(url):
    """Opens a page a window from the PC was showing."""
    if AppKit is not None:
        AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.URLWithString_(url))
