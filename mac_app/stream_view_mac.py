"""A PC window, live in a window of its own on this Mac.

The PC's stream_win sends the window as JPEG frames on a connection of its own;
what is done in this copy goes back on it as `stream_input` messages, the pointer
as fractions of the window, so the PC can post it at the window's own place.
Closing this copy stops the stream. The twin of the PC's carry_win.StreamView.
"""

import logging
import socket
import threading

import AppKit
import objc
from Foundation import NSObject
from PyObjCTools import AppHelper

import protocol
from key_codes import SPECIAL_KEY_NAMES

LOGGER = logging.getLogger(__name__)

MSG_INPUT = "stream_input"
FIT = 0.8
# This Mac's modifiers as the PC knows them: Cmd does there what Ctrl does.
_MODIFIERS = (
    (AppKit.NSEventModifierFlagCommand, "ctrl"),
    (AppKit.NSEventModifierFlagControl, "ctrl"),
    (AppKit.NSEventModifierFlagOption, "alt"),
    (AppKit.NSEventModifierFlagShift, "shift"),
)
_views = []


def show(connection, session, hello):
    """The receiver's stream_callback: runs on its thread for as long as the window is shown."""
    holder = {"ready": threading.Event()}
    AppHelper.callAfter(_open, connection, session, hello, holder)
    if not holder["ready"].wait(5.0) or holder.get("view") is None:
        return
    view = holder["view"]
    try:
        while not view.closed:
            try:
                AppHelper.callAfter(view.show_frame, protocol.recv_bytes(connection, session))
            except socket.timeout:
                break
    except (OSError, protocol.ProtocolError, protocol.ConnectionClosed):
        pass
    view.closed = True
    AppHelper.callAfter(view.window().close)


def _open(connection, session, hello, holder):
    try:
        w, h = max(1, int(hello.get("w") or 800)), max(1, int(hello.get("h") or 600))
        screen = AppKit.NSScreen.mainScreen().visibleFrame()
        scale = min(1.0, screen.size.width * FIT / w, screen.size.height * FIT / h)
        style = (
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
            | AppKit.NSWindowStyleMaskMiniaturizable | AppKit.NSWindowStyleMaskResizable
        )
        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (w * scale, h * scale)), style, AppKit.NSBackingStoreBuffered, False
        )
        window.setTitle_(f"{hello.get('title') or 'Window'} — on the PC")
        window.setReleasedWhenClosed_(False)
        window.setAcceptsMouseMovedEvents_(True)
        view = StreamView.alloc().initWithFrame_(((0, 0), (w * scale, h * scale)))
        view.connection, view.session = connection, session
        window.setContentView_(view)
        window.setDelegate_(view)
        window.center()
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        window.makeKeyAndOrderFront_(None)
        window.makeFirstResponder_(view)
        _views.append(view)
        holder["view"] = view
    except Exception:
        LOGGER.exception("The PC's window could not be shown")
    finally:
        holder["ready"].set()


class StreamView(AppKit.NSView):
    def initWithFrame_(self, frame):
        self = objc.super(StreamView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.image = None
        self.closed = False
        self.held = set()
        self.connection = self.session = None
        return self

    def isFlipped(self):
        return True

    def acceptsFirstResponder(self):
        return True

    def acceptsFirstMouse_(self, _event):
        return True

    @objc.python_method
    def show_frame(self, jpeg):
        image = AppKit.NSImage.alloc().initWithData_(AppKit.NSData.dataWithBytes_length_(jpeg, len(jpeg)))
        if image is not None:
            self.image = image
            self.setNeedsDisplay_(True)

    def drawRect_(self, _rect):
        AppKit.NSColor.colorWithWhite_alpha_(0.08, 1.0).set()
        AppKit.NSRectFill(self.bounds())
        if self.image is not None:
            self.image.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                self.bounds(), AppKit.NSZeroRect, AppKit.NSCompositingOperationCopy, 1.0, True, None
            )

    @objc.python_method
    def send(self, **data):
        if self.closed:
            return
        try:
            protocol.send_msg(self.connection, self.session, {"type": MSG_INPUT, "data": data})
        except OSError:
            self.window().close()

    @objc.python_method
    def at(self, event):
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        size = self.bounds().size
        return {"x": point.x / max(1.0, size.width), "y": point.y / max(1.0, size.height)}

    def mouseMoved_(self, event):
        self.send(kind="move", **self.at(event))

    def mouseDragged_(self, event):
        self.send(kind="move", **self.at(event))

    def rightMouseDragged_(self, event):
        self.send(kind="move", **self.at(event))

    def mouseDown_(self, event):
        self.send(kind="down", button="left", **self.at(event))

    def mouseUp_(self, event):
        self.send(kind="up", button="left", **self.at(event))

    def rightMouseDown_(self, event):
        self.send(kind="down", button="right", **self.at(event))

    def rightMouseUp_(self, event):
        self.send(kind="up", button="right", **self.at(event))

    def scrollWheel_(self, event):
        # A trackpad's points as the wheel's clicks, roughly: a notch is about ten points.
        divisor = 10.0 if event.hasPreciseScrollingDeltas() else 1.0
        self.send(kind="scroll", dy=event.scrollingDeltaY() / divisor, dx=-event.scrollingDeltaX() / divisor, **self.at(event))

    def flagsChanged_(self, event):
        flags = event.modifierFlags()
        now = {name for mask, name in _MODIFIERS if flags & mask}
        for name in sorted(now - self.held):
            self.send(kind="key", name=name, down=True)
        for name in sorted(self.held - now):
            self.send(kind="key", name=name, down=False)
        self.held = now

    def keyDown_(self, event):
        special = SPECIAL_KEY_NAMES.get(event.keyCode())
        if special and special not in ("cmd", "cmd_r", "shift", "shift_r", "alt", "alt_r", "ctrl", "ctrl_r", "caps_lock"):
            self.send(kind="key", name=special, down=True)
            self.send(kind="key", name=special, down=False)
            return
        typed = event.characters() or ""
        if len(typed) == 1 and typed.isprintable() and not (self.held - {"shift"}):
            # A typed character carries its own shift: the PC is told to let go of it first.
            if "shift" in self.held:
                self.send(kind="key", name="shift", down=False)
            self.send(kind="key", name=typed, down=True)
            self.send(kind="key", name=typed, down=False)
            if "shift" in self.held:
                self.send(kind="key", name="shift", down=True)
            return
        bare = (event.charactersIgnoringModifiers() or "").lower()
        if len(bare) == 1 and bare.isprintable():
            self.send(kind="key", name=bare, down=True)
            self.send(kind="key", name=bare, down=False)

    def windowWillClose_(self, _notification):
        if self.closed:
            return
        self.closed = True
        for name in sorted(self.held):
            self.send(kind="key", name=name, down=False)
        try:
            protocol.send_msg(self.connection, self.session, {"type": MSG_INPUT, "data": {"kind": "close"}})
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        if self in _views:
            _views.remove(self)
