"""A PC window carried to the Mac: its page, or the window itself, live.

The twin of the Mac's handoff and stream_mac. A window dragged by its title bar
against the edge that leads to the Mac is read here (dragged_window). Once the
pointer is through, Escape cancels Windows' own move, so the window goes back
where it was instead of snapping to the edge, and then:

- a browser's window sends its address, read from the address bar through UI
  Automation, and the Mac opens it; the window is minimised here;
- any other window streams to the Mac as JPEG frames on a connection of its own
  (channel "stream", as the Mac's do), and what is done in the Mac's copy comes
  back as `stream_input` messages, posted here at the window's place.

ponytail: PrintWindow per frame and no video codec, like the Mac's polling
fallback. A document's file is not looked for: Windows has no AXDocument.
"""

import ctypes
import ctypes.wintypes
import logging
import os
import re
import subprocess
import threading
import time

import protocol

LOGGER = logging.getLogger(__name__)

CHANNEL = "stream"
MSG_INPUT = "stream_input"
FRAMES_PER_SECOND = 15
JPEG_QUALITY = 60
CONNECT_TIMEOUT_SECONDS = 1.0
IO_TIMEOUT_SECONDS = 10.0
# Windows says when a window starts and stops being moved or sized; a move is one begun anywhere
# but on a sizing border, as the window itself answers WM_NCHITTEST.
EVENT_SYSTEM_MOVESIZESTART = 0x000A
EVENT_SYSTEM_MOVESIZEEND = 0x000B
WINEVENT_OUTOFCONTEXT = 0
OBJID_WINDOW = 0
WM_NCHITTEST = 0x0084
HTCAPTION = 2
SMTO_ABORTIFHUNG = 2
HIT_TEST_TIMEOUT_MS = 50
BROWSERS = {"msedge", "chrome", "brave", "firefox", "vivaldi", "opera"}
_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
_NO_WINDOW = 0x08000000
# By its full path, as firewall_win starts it: a powershell.exe earlier on the PATH is not run.
POWERSHELL = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
# A host an address bar may show: letters, digits, hyphens and dots, an optional port, no userinfo.
_HOST = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(:\d{1,5})?$")
SW_MINIMIZE = 6
PW_RENDERFULLCONTENT = 2

_URL_SCRIPT = r"""
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$w = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]%d)
$edit = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Edit)
$bar = $w.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $edit)
if ($bar) { $bar.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value }
"""


def _user32():
    return ctypes.windll.user32


def _rect(hwnd):
    rect = ctypes.wintypes.RECT()
    if not _user32().GetWindowRect(ctypes.wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


# Why the last dragged_window() answered None, for the log line a drag stopped at the edge writes.
last_refusal = "no window has been moved since Beamer started"
# The window Windows is moving right now, from its own move events; see watch_moves.
_moving = {"hwnd": None}
_hook = {}


def _refuse(why):
    global last_refusal
    last_refusal = why
    return None


def is_move(hit) -> bool:
    """Whether a WM_NCHITTEST answer, or None for none, says the move began on the title bar.
    Anything else, a sizing border, the client area or no answer at all, is no window drag."""
    return hit == HTCAPTION


def forget_move() -> None:
    """Drops the window being moved: called wherever a gesture starts or ends, so one never
    outlives the drag it belonged to."""
    _moving["hwnd"] = None


def on_move_event(event, hwnd, hit) -> None:
    """One of Windows' move/size events for a top-level window: `hit` is what it answered for the
    pointer when it began, or None."""
    if event == EVENT_SYSTEM_MOVESIZESTART:
        _moving["hwnd"] = int(hwnd) if is_move(hit) else None
        if not is_move(hit):
            _refuse(f"the moved window did not say the drag began on its title bar (hit test {hit})")
    elif event == EVENT_SYSTEM_MOVESIZEEND and _moving["hwnd"] == int(hwnd or 0):
        _moving["hwnd"] = None


def _hit_test(hwnd):
    user32 = _user32()
    point = ctypes.wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    packed = ((point.y & 0xFFFF) << 16) | (point.x & 0xFFFF)
    result = ctypes.c_size_t(0)
    if not user32.SendMessageTimeoutW(
        ctypes.wintypes.HWND(hwnd), WM_NCHITTEST, 0, packed, SMTO_ABORTIFHUNG, HIT_TEST_TIMEOUT_MS, ctypes.byref(result)
    ):
        return None
    return ctypes.c_long(result.value).value


def watch_moves() -> None:
    """Listens for Windows' move events. Called once, on the GUI thread, whose message loop
    delivers them; the hook and its callback are kept alive here."""
    if _hook:
        return
    callback_type = ctypes.WINFUNCTYPE(
        None, ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD, ctypes.wintypes.HWND,
        ctypes.wintypes.LONG, ctypes.wintypes.LONG, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD,
    )

    def callback(_hook_handle, event, hwnd, obj, _child, _thread, _time):
        try:
            if obj == OBJID_WINDOW and hwnd:
                on_move_event(event, hwnd, _hit_test(hwnd) if event == EVENT_SYSTEM_MOVESIZESTART else None)
        except Exception:
            LOGGER.exception("A window move event could not be read")

    user32 = _user32()
    user32.SetWinEventHook.restype = ctypes.wintypes.HANDLE
    user32.SetWinEventHook.argtypes = [
        ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.wintypes.HMODULE, callback_type,
        ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD,
    ]
    _hook["callback"] = callback_type(callback)
    _hook["handle"] = user32.SetWinEventHook(
        EVENT_SYSTEM_MOVESIZESTART, EVENT_SYSTEM_MOVESIZEEND, None, _hook["callback"], 0, 0, WINEVENT_OUTOFCONTEXT
    )
    if not _hook["handle"]:
        LOGGER.warning("Windows refused the move-event hook; PC windows will not cross to the Mac")


def dragged_window():
    """The window Windows is moving right now, as a dict, or None. Not guessed from where the
    pointer is: a title bar's height follows the display's scaling, and Snap resizes the window
    under the pointer, which left the first guesses wrong. On the hook thread, so only cheap calls."""
    try:
        hwnd = _moving["hwnd"]
        if hwnd is None:
            return _refuse("Windows reported no window being moved")
        user32 = _user32()
        if not user32.IsWindow(ctypes.wintypes.HWND(hwnd)):
            return _refuse("the moved window has gone")
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(ctypes.wintypes.HWND(hwnd), ctypes.byref(pid))
        if pid.value == os.getpid():
            return _refuse("the window being moved is Beamer's")
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(ctypes.wintypes.HWND(hwnd), name, 256)
        if name.value in _SHELL_CLASSES:
            return _refuse(f"the window being moved is the shell's ({name.value})")
        rect = _rect(hwnd)
        if rect is None:
            return _refuse("the moved window has no frame")
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(ctypes.wintypes.HWND(hwnd), title, 512)
        return {"hwnd": int(hwnd), "pid": int(pid.value), "title": title.value or "Window", "rect": rect}
    except Exception as exc:
        return _refuse(f"reading the window failed: {exc!r}")


def _exe_name(pid):
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = ctypes.wintypes.DWORD(260)
        buffer = ctypes.create_unicode_buffer(260)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return os.path.splitext(os.path.basename(buffer.value))[0].lower()
    finally:
        kernel32.CloseHandle(handle)


def page_url(window):
    """The address in a browser window's address bar, as a full http(s) URL, or None."""
    if _exe_name(window["pid"]) not in BROWSERS:
        return None
    try:
        out = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", _URL_SCRIPT % window["hwnd"]],
            capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return as_url(out.stdout.strip())


def as_url(text):
    """What an address bar shows, as a URL to open: browsers hide the scheme."""
    if not text or len(text) > 4096 or any(c.isspace() or ord(c) < 0x20 for c in text):
        return None
    scheme = ""
    for prefix in ("http://", "https://"):
        if text.startswith(prefix):
            scheme, text = prefix, text[len(prefix):]
    host = re.split(r"[/?#]", text, 1)[0]
    if not _HOST.match(host):
        return None
    return (scheme or "https://") + text


def end_move():
    """Escape ends the move Windows is making with the button, putting the window back."""
    import input_injector

    input_injector.inject_key("esc", True)
    input_injector.inject_key("esc", False)


def minimise(window):
    _user32().ShowWindow(ctypes.wintypes.HWND(window["hwnd"]), SW_MINIMIZE)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.wintypes.WORD), ("biBitCount", ctypes.wintypes.WORD), ("biCompression", ctypes.wintypes.DWORD),
        ("biSizeImage", ctypes.wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.wintypes.DWORD), ("biClrImportant", ctypes.wintypes.DWORD),
    ]


def capture(hwnd):
    """The window's contents as JPEG bytes, drawn even where it is covered, or None once it is
    gone or minimised."""
    from PySide6.QtCore import QBuffer, QByteArray
    from PySide6.QtGui import QImage

    user32, gdi32 = _user32(), ctypes.windll.gdi32
    if not user32.IsWindow(ctypes.wintypes.HWND(hwnd)) or user32.IsIconic(ctypes.wintypes.HWND(hwnd)):
        return None
    rect = _rect(hwnd)
    if rect is None or rect[2] <= 0 or rect[3] <= 0:
        return None
    width, height = rect[2], rect[3]
    screen = user32.GetDC(None)
    memory = gdi32.CreateCompatibleDC(screen)
    bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
    old = gdi32.SelectObject(memory, bitmap)
    try:
        if not user32.PrintWindow(ctypes.wintypes.HWND(hwnd), memory, PW_RENDERFULLCONTENT):
            return None
        header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        pixels = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(memory, bitmap, 0, height, pixels, ctypes.byref(header), 0):
            return None
    finally:
        gdi32.SelectObject(memory, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory)
        user32.ReleaseDC(None, screen)
    image = QImage(pixels.raw, width, height, width * 4, QImage.Format.Format_RGB32)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, "JPG", JPEG_QUALITY)
    return bytes(data)


class Streamer:
    """Streams `window` (a dragged_window dict) to the Mac at `endpoint` until either end stops."""

    def __init__(self, endpoint, window, socket_factory, alert):
        self.endpoint = endpoint
        self.window = window
        self.socket_factory = socket_factory
        self.alert = alert
        self.stop = threading.Event()
        self.rect = window["rect"]

    def start(self):
        threading.Thread(target=self._run, name="Beamer-stream", daemon=True).start()

    def _run(self):
        host, port, token = self.endpoint
        sock = None
        try:
            sock = self.socket_factory((host, port), CONNECT_TIMEOUT_SECONDS)
            sock.settimeout(IO_TIMEOUT_SECONDS)
            session = protocol.SecureSession(token)
            sock.sendall(session.preamble())
            protocol.recv_preamble(sock, session)
            _x, _y, w, h = self.rect
            hello = protocol.hello_msg(channel=CHANNEL)
            hello["data"].update(title=self.window["title"], w=int(w), h=int(h))
            protocol.send_msg(sock, session, hello)
            reply = protocol.recv_msg(sock, session)
            if reply.get("type") != protocol.MSG_WELCOME or not (reply.get("data") or {}).get("stream"):
                raise protocol.ProtocolError("the Mac did not take the window")
            threading.Thread(target=self._read, args=(sock, session), name="Beamer-stream-input", daemon=True).start()
            LOGGER.info("streaming %r to the Mac", self.window["title"])
            while not self.stop.is_set():
                started = time.monotonic()
                self.rect = _rect(self.window["hwnd"]) or self.rect
                jpeg = capture(self.window["hwnd"])
                if jpeg is None:
                    break
                protocol.send_bytes(sock, session, jpeg)
                self.stop.wait(max(0.0, 1.0 / FRAMES_PER_SECOND - (time.monotonic() - started)))
        except (OSError, protocol.ProtocolError, protocol.ConnectionClosed) as exc:
            if not self.stop.is_set():
                LOGGER.warning("the window stream stopped: %s", exc)
        finally:
            self.stop.set()
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            LOGGER.info("stopped streaming %r", self.window["title"])

    def _read(self, sock, session):
        try:
            while not self.stop.is_set():
                try:
                    message = protocol.recv_msg(sock, session)
                except OSError as exc:
                    if "timed out" in str(exc):
                        continue
                    raise
                if message.get("type") == MSG_INPUT and isinstance(message.get("data"), dict):
                    if self.handle(message["data"]) == "close":
                        break
        except (OSError, protocol.ProtocolError, protocol.ConnectionClosed):
            pass
        self.stop.set()

    def point(self, data):
        left, top, width, height = self.rect
        fx = min(max(float(data.get("x", 0.0)), 0.0), 1.0)
        fy = min(max(float(data.get("y", 0.0)), 0.0), 1.0)
        return int(left + fx * (width - 1)), int(top + fy * (height - 1))

    def handle(self, data):
        """Posts one stream_input here; answers "close" when the Mac's copy was closed."""
        import desktop_win
        import input_injector

        kind = data.get("kind")
        if kind == "close":
            return "close"
        if kind == "key":
            name = data.get("name")
            if isinstance(name, str) and name:
                if data.get("down"):
                    _user32().SetForegroundWindow(ctypes.wintypes.HWND(self.window["hwnd"]))
                input_injector.inject_key(name, bool(data.get("down")))
            return None
        desktop_win.set_cursor_position(*self.point(data))
        button = "right" if data.get("button") == "right" else "left"
        if kind == "down":
            _user32().SetForegroundWindow(ctypes.wintypes.HWND(self.window["hwnd"]))
            input_injector.inject_mouse_button(button, True)
        elif kind == "up":
            input_injector.inject_mouse_button(button, False)
        elif kind == "scroll":
            input_injector.inject_scroll(float(data.get("dy", 0.0)), float(data.get("dx", 0.0)), "line")
        return None
