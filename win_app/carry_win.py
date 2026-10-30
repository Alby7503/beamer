"""What crosses the edge besides the pointer, on this PC's side.

- The catcher: a strip along the edge that leads to the Mac, up only while the
  left button is down. A drag of files passes over it on its way to the edge,
  which is how this PC learns what is being dragged: Windows has no shared drag
  pasteboard to read, as the Mac does.
- Files from the Mac: shown in Explorer, opened, or moved into the Explorer
  window or Desktop they were dropped on.
- A window from the Mac: its page opened here, its outline (the ghost) under
  the pointer until the button comes up, or the window itself, live (the stream).

Every receiver callback here may arrive on a worker thread; they are marshalled
to the GUI thread by signals.
"""

import ctypes
import ctypes.wintypes
import logging
import os
import socket
import subprocess
import threading

from PySide6.QtCore import QObject, QPoint, QRect, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QDesktopServices, QFont, QGuiApplication, QImage, QPainter, QPen
from PySide6.QtWidgets import QWidget

import file_transfer
import protocol
import stream_win

LOGGER = logging.getLogger(__name__)

FILES_FOLDER = os.path.join(os.path.expanduser("~"), "Downloads", "Beamer")
CATCHER_PX = 2
GHOST_MAX_PX = 420
GHOST_FRAME_MS = 16
STREAM_FIT = 0.8
STREAM_INPUT = "stream_input"
_NO_WINDOW = 0x08000000
GA_ROOT = 2

# Qt keys to the Mac's key names. Printable keys go as the character they type instead.
_SPECIAL_KEYS = {
    Qt.Key.Key_Return: "enter", Qt.Key.Key_Enter: "enter", Qt.Key.Key_Tab: "tab", Qt.Key.Key_Backspace: "backspace",
    Qt.Key.Key_Escape: "esc", Qt.Key.Key_Delete: "delete", Qt.Key.Key_Left: "left", Qt.Key.Key_Right: "right",
    Qt.Key.Key_Up: "up", Qt.Key.Key_Down: "down", Qt.Key.Key_Home: "home", Qt.Key.Key_End: "end",
    Qt.Key.Key_PageUp: "page_up", Qt.Key.Key_PageDown: "page_down", Qt.Key.Key_Space: "space",
    # Ctrl does on a Mac what Cmd does there, as the PC's own link maps it.
    Qt.Key.Key_Control: "cmd", Qt.Key.Key_Alt: "alt", Qt.Key.Key_Meta: "ctrl", Qt.Key.Key_Shift: "shift",
}
_MODIFIERS = {"cmd", "alt", "ctrl", "shift"}


def show_files(paths, then) -> None:
    """Opens files carried as a window's document; shows the rest in Explorer, the first selected."""
    try:
        if then == file_transfer.THEN_OPEN and all(file_transfer.safe_to_open(path) for path in paths):
            for path in paths:
                os.startfile(path)
        else:
            subprocess.Popen(f'explorer /select,"{os.path.normpath(paths[0])}"')
    except OSError:
        LOGGER.exception("Could not show %s", paths[0])


def _desktop_folder() -> str:
    for candidate in (os.path.join(os.path.expanduser("~"), "Desktop"), os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop")):
        if os.path.isdir(candidate):
            return candidate
    return FILES_FOLDER


def drop_folder():
    """The folder under the pointer: an Explorer window's, the Desktop's, or None over any other
    window, which takes nothing. Runs off the GUI thread; asking Explorer takes a moment."""
    user32 = ctypes.windll.user32
    point = ctypes.wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    hwnd = user32.GetAncestor(user32.WindowFromPoint(point), GA_ROOT)
    name = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, name, 256)
    if name.value in ("Progman", "WorkerW"):
        return _desktop_folder()
    if name.value != "CabinetWClass":
        return None
    # ponytail: an Explorer window with tabs reports every tab; the first is taken.
    script = (
        f"(New-Object -ComObject Shell.Application).Windows() | Where-Object {{ $_.HWND -eq {int(hwnd)} }} | "
        "ForEach-Object { $_.Document.Folder.Self.Path } | Select-Object -First 1"
    )
    out = subprocess.run(
        [stream_win.POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW,
    )
    path = out.stdout.strip()
    return path if os.path.isdir(path) else None


def _edge_strip(edge: str) -> QRect:
    """The strip along `edge` of the whole desktop, in Qt's coordinates."""
    box = QRect()
    for screen in QGuiApplication.screens():
        box = box.united(screen.geometry())
    if edge == "left":
        return QRect(box.left(), box.top(), CATCHER_PX, box.height())
    if edge == "right":
        return QRect(box.right() - CATCHER_PX + 1, box.top(), CATCHER_PX, box.height())
    if edge == "top":
        return QRect(box.left(), box.top(), box.width(), CATCHER_PX)
    return QRect(box.left(), box.bottom() - CATCHER_PX + 1, box.width(), CATCHER_PX)


class Catcher(QWidget):
    """The strip a drag of files passes over at the edge. Drawn at the faintest alpha Windows still
    hit-tests, so it is invisible and still under the pointer."""

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAcceptDrops(True)
        self.files = None

    def paintEvent(self, _event) -> None:
        QPainter(self).fillRect(self.rect(), QColor(0, 0, 0, 1))

    def dragEnterEvent(self, event) -> None:
        files = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if files:
            self.files = files
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        if self.files:
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        # Let go on the strip without crossing: nothing happens to the files.
        event.ignore()


class Ghost(QWidget):
    """A carried window's outline and title, following the pointer."""

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.title = ""
        self.timer = QTimer(self)
        self.timer.setInterval(GHOST_FRAME_MS)
        self.timer.timeout.connect(self.follow)

    def appear(self, data) -> None:
        w, h = max(1, int(data.get("w") or 200)), max(1, int(data.get("h") or 120))
        scale = min(1.0, GHOST_MAX_PX / max(w, h))
        self.resize(int(w * scale), int(h * scale))
        self.title = str(data.get("title") or "")
        self.follow()
        self.show()
        self.timer.start()

    def vanish(self) -> None:
        self.timer.stop()
        self.hide()

    def follow(self) -> None:
        self.move(QCursor.pos() - QPoint(self.width() // 2, 12))

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(40, 120, 255, 46))
        painter.setPen(QPen(QColor(40, 120, 255, 200), 2))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 10, 10)
        painter.setPen(QColor(255, 255, 255, 230))
        painter.setFont(QFont(painter.font().family(), 10))
        painter.drawText(self.rect().adjusted(12, 8, -12, -8), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter, self.title)


class StreamView(QWidget):
    """A Mac window, live. What is done in it goes back to the Mac as stream_input messages."""

    frame = Signal(bytes)
    ended = Signal()

    def __init__(self, connection, session, hello) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.connection, self.session = connection, session
        self.image = QImage()
        self.held_mods = set()
        self.setWindowTitle(f"{hello.get('title') or 'Window'} — on the Mac")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        w, h = max(1, int(hello.get("w") or 800)), max(1, int(hello.get("h") or 600))
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        room = screen.availableGeometry()
        scale = min(1.0, room.width() * STREAM_FIT / w, room.height() * STREAM_FIT / h)
        self.resize(int(w * scale), int(h * scale))
        at = QCursor.pos() - QPoint(self.width() // 2, 20)
        self.move(max(room.left(), min(at.x(), room.right() - self.width())), max(room.top(), min(at.y(), room.bottom() - self.height())))
        self.frame.connect(self._show_frame)
        self.ended.connect(self.close)
        self._closed = False

    def _show_frame(self, jpeg: bytes) -> None:
        image = QImage.fromData(jpeg, "JPG")
        if not image.isNull():
            self.image = image
            self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(20, 20, 20))
        if not self.image.isNull():
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(self.rect(), self.image)

    def send(self, **data) -> None:
        try:
            protocol.send_msg(self.connection, self.session, {"type": STREAM_INPUT, "data": data})
        except OSError:
            self.close()

    def _at(self, event):
        position = event.position()
        return {"x": position.x() / max(1, self.width()), "y": position.y() / max(1, self.height())}

    @staticmethod
    def _button(event):
        return "right" if event.button() == Qt.MouseButton.RightButton else "left"

    def mouseMoveEvent(self, event) -> None:
        self.send(kind="move", **self._at(event))

    def mousePressEvent(self, event) -> None:
        self.send(kind="down", button=self._button(event), **self._at(event))

    def mouseReleaseEvent(self, event) -> None:
        self.send(kind="up", button=self._button(event), **self._at(event))

    def mouseDoubleClickEvent(self, event) -> None:
        # Qt folds the second press into this; the Mac counts the clicks itself.
        self.mousePressEvent(event)

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta()
        self.send(kind="scroll", dy=delta.y() / 120.0, dx=delta.x() / 120.0, **self._at(event))

    def _key(self, name: str, down: bool) -> None:
        self.send(kind="key", name=name, down=down)

    def keyPressEvent(self, event) -> None:
        special = _SPECIAL_KEYS.get(event.key())
        text = event.text()
        if special in _MODIFIERS:
            # Shift goes with the keys that type no character; a typed character carries its own.
            if special != "shift" and not event.isAutoRepeat():
                self.held_mods.add(special)
                self._key(special, True)
            return
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if special is None and text and text.isprintable() and not self.held_mods:
            self._key(text, True)
            self._key(text, False)
            return
        name = special or (chr(event.key()).lower() if 0x20 < event.key() < 0x7F else None)
        if name is None:
            return
        if shift:
            self._key("shift", True)
        self._key(name, True)
        self._key(name, False)
        if shift:
            self._key("shift", False)

    def keyReleaseEvent(self, event) -> None:
        special = _SPECIAL_KEYS.get(event.key())
        if special in self.held_mods and not event.isAutoRepeat():
            self.held_mods.discard(special)
            self._key(special, False)

    def closeEvent(self, event) -> None:
        if not self._closed:
            self._closed = True
            for name in self.held_mods:
                self._key(name, False)
            self.send(kind="close")
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        super().closeEvent(event)


class Carry(QObject):
    """All of the above for one app. Its methods are the receiver's and sender's callbacks."""

    _left = Signal(bool)
    _files = Signal(list, str)
    _open = Signal(str)
    _ghost = Signal(object)
    _stream = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        # The MacSender, set by the app once it exists: the catcher reads its edge.
        self.sender = None
        self.catcher = Catcher()
        self.ghost_window = Ghost()
        self.views = []
        try:
            stream_win.watch_moves()
        except Exception:
            LOGGER.exception("Could not listen for window moves; PC windows will not cross to the Mac")
        self._left.connect(self._on_left)
        self._files.connect(show_files)
        self._open.connect(lambda url: QDesktopServices.openUrl(QUrl(url)))
        self._ghost.connect(self._on_ghost)
        self._stream.connect(self._on_stream)

    # -- the sender's: files leaving this PC --------------------------------------------------

    def left_button(self, down: bool) -> None:
        if not down:
            # Every move ends with the button; a lost end event must not carry a window later.
            stream_win._moving["hwnd"] = None
        self._left.emit(down)

    def dragged_files(self):
        return self.catcher.files

    def _on_left(self, down: bool) -> None:
        sender = self.sender
        edge = sender.edge if sender is not None else None
        if down and edge and sender.files_endpoint is not None and not sender.redirecting:
            self.catcher.files = None
            self.catcher.setGeometry(_edge_strip(edge))
            self.catcher.show()
        else:
            self.catcher.hide()
            self.catcher.files = None

    # -- windows leaving this PC ---------------------------------------------------------------

    def carry_window(self, window) -> None:
        """A window dragged across to the Mac: its page opens there, or it streams. Called on the
        hook or session thread, so the work goes to a thread of its own."""
        threading.Thread(target=self._carry_worker, args=(window,), name="Beamer-carry", daemon=True).start()

    def _carry_worker(self, window) -> None:
        sender = self.sender
        try:
            stream_win.end_move()
        except Exception:
            LOGGER.exception("Could not end the window's move on this PC")
        url = stream_win.page_url(window)
        if url:
            sender.send_open(url)
            stream_win.minimise(window)
            LOGGER.info("carried a page to the Mac")
        elif sender.stream_ok and sender.files_endpoint is not None:
            stream_win.Streamer(sender.files_endpoint, window, sender._socket_factory, sender._alert).start()
        else:
            LOGGER.info("the window has no page to carry, and the Mac does not show live windows")

    def carry_out(self, moment: str) -> None:
        """The receiver's hook for the Mac's pointer driving this PC: a PC window it drags home
        through the edge goes with it, as one this PC's own mouse drags does."""
        if moment == "cross" and self.sender is not None and self.sender.files_endpoint is not None:
            window = stream_win.dragged_window()
            if window:
                self.carry_window(window)

    # -- the receiver's: what arrives from the Mac --------------------------------------------

    def files_arrived(self, paths, then) -> None:
        self._files.emit(list(paths), then)

    def open_url(self, url: str) -> None:
        self._open.emit(url)

    def ghost(self, data) -> None:
        self._ghost.emit(data)

    def _on_ghost(self, data) -> None:
        if data:
            self.ghost_window.appear(data)
        else:
            self.ghost_window.vanish()

    def stream(self, connection, session, hello) -> None:
        """Runs on the receiver's thread for as long as the window is shown."""
        ready = threading.Event()
        holder = {"ready": ready}
        self._stream.emit((connection, session, hello, holder))
        if not ready.wait(5.0) or holder.get("view") is None:
            return
        view = holder["view"]
        try:
            while True:
                try:
                    view.frame.emit(protocol.recv_bytes(connection, session))
                except socket.timeout:
                    break
        except (OSError, protocol.ProtocolError, protocol.ConnectionClosed):
            pass
        view._closed = True
        view.ended.emit()

    def _on_stream(self, args) -> None:
        connection, session, hello, holder = args
        try:
            view = StreamView(connection, session, hello)
            self.views = [v for v in self.views if v.isVisible()] + [view]
            view.show()
            view.raise_()
            view.activateWindow()
            holder["view"] = view
        finally:
            holder["ready"].set()
