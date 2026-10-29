"""One Mac window shown live on the PC, and worked from there.

The window stays here. Its contents go to the PC as JPEG frames on a connection
of their own (channel "stream" on the PC's port, as files do), and what is done
in the PC's copy comes back on the same connection as `stream_input` messages:
the pointer as fractions of the window, buttons, the wheel and keys. They are
posted here at the window's own place, marked as Beamer's like all its input.

Frames come from ScreenCaptureKit, which hands over a new one only when the
window changes (about 30 a second measured, against 10 polling the old
CGWindowListCreateImage, which stays as the fallback). Without changes the last
frame is sent again every KEEPALIVE_SECONDS, so the PC can tell quiet from gone.

ponytail: JPEG frames, no video codec. Fine on a LAN at window sizes; a hardware
H.264 encoder (VideoToolbox) is the upgrade if Wi-Fi chokes.
"""

import threading
import time

import AppKit
import objc
import Quartz
from Foundation import NSObject

try:
    import CoreMedia
    import ScreenCaptureKit as SCK
except ImportError:  # pragma: no cover - the polling fallback carries on
    CoreMedia = SCK = None

import handoff
import input_injector_mac
import protocol
from input_injector_mac import INJECTED_MARK

CHANNEL = "stream"
MSG_INPUT = "stream_input"
FRAMES_PER_SECOND = 20
JPEG_QUALITY = 0.55
CONNECT_TIMEOUT_SECONDS = 1.0
IO_TIMEOUT_SECONDS = 10.0
FRAME_REFRESH_SECONDS = 0.5
KEEPALIVE_SECONDS = 1.0
SCK_START_SECONDS = 3.0
BGRA = 1111970369

_BUTTON_EVENTS = {
    "left": (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGEventLeftMouseDragged, Quartz.kCGMouseButtonLeft),
    "right": (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp, Quartz.kCGEventRightMouseDragged, Quartz.kCGMouseButtonRight),
}


def capture(number):
    """The window's contents as JPEG bytes at one pixel per point, or None once it is gone or
    Screen Recording is not granted."""
    image = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull,
        Quartz.kCGWindowListOptionIncludingWindow,
        number,
        Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageNominalResolution,
    )
    if image is None:
        return None
    # ImageIO, not NSBitmapImageRep: measured a third faster for the same picture.
    data = Quartz.CFDataCreateMutable(None, 0)
    destination = Quartz.CGImageDestinationCreateWithData(data, "public.jpeg", 1, None)
    Quartz.CGImageDestinationAddImage(destination, image, {Quartz.kCGImageDestinationLossyCompressionQuality: JPEG_QUALITY})
    if not Quartz.CGImageDestinationFinalize(destination):
        return None
    return bytes(data)


def _wait(start):
    """Calls `start(handler)` and waits for the handler's (result, error). Each handler is a real
    function: one that returns a value, as a lambda building a tuple does, where ScreenCaptureKit
    expects none was measured corrupting memory."""
    box = {}
    done = threading.Event()

    def handler(*args):
        box["args"] = args
        done.set()

    start(handler)
    if not done.wait(SCK_START_SECONDS):
        raise OSError("ScreenCaptureKit did not answer")
    return box["args"]


class _Polled:
    """The fallback: one CGWindowListCreateImage per frame, at FRAMES_PER_SECOND at most."""

    def __init__(self, number):
        self.number = number
        self.at = 0.0

    def next(self):
        time.sleep(max(0.0, 1.0 / FRAMES_PER_SECOND - (time.monotonic() - self.at)))
        self.at = time.monotonic()
        return capture(self.number)

    def stop(self):
        pass


if SCK is not None:

    class _Output(NSObject, protocols=[objc.protocolNamed("SCStreamOutput")]):
        def stream_didOutputSampleBuffer_ofType_(self, stream, buffer, kind):
            pixels = CoreMedia.CMSampleBufferGetImageBuffer(buffer)
            if pixels is not None:
                self.sink(pixels)


class _Pushed:
    """ScreenCaptureKit's stream of one window: each new frame is encoded as it arrives, and
    next() waits for one, or answers the last again after KEEPALIVE_SECONDS."""

    def __init__(self, number, width, height):
        (content, error) = _wait(SCK.SCShareableContent.getShareableContentWithCompletionHandler_)
        window = next((w for w in (content.windows() if content else ()) if w.windowID() == number), None)
        if window is None:
            raise OSError(f"ScreenCaptureKit cannot see the window: {error}")
        config = SCK.SCStreamConfiguration.alloc().init()
        config.setWidth_(int(width))
        config.setHeight_(int(height))
        config.setMinimumFrameInterval_(CoreMedia.CMTimeMake(1, 30))
        config.setShowsCursor_(False)
        config.setPixelFormat_(BGRA)
        self.context = Quartz.CIContext.contextWithOptions_(None)
        self.space = Quartz.CGColorSpaceCreateDeviceRGB()
        self.lock = threading.Lock()
        self.fresh = threading.Event()
        self.jpeg = None
        self.output = _Output.alloc().init()
        self.output.sink = self._frame
        filter_ = SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(window)
        self.stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(filter_, config, None)
        ok, error = self.stream.addStreamOutput_type_sampleHandlerQueue_error_(self.output, SCK.SCStreamOutputTypeScreen, None, None)
        if not ok:
            raise OSError(f"ScreenCaptureKit refused the window: {error}")
        (error,) = _wait(self.stream.startCaptureWithCompletionHandler_)
        if error is not None:
            raise OSError(f"ScreenCaptureKit did not start: {error}")

    def _frame(self, pixels):
        image = Quartz.CIImage.imageWithCVPixelBuffer_(pixels)
        with self.lock:
            data = self.context.JPEGRepresentationOfImage_colorSpace_options_(
                image, self.space, {Quartz.kCGImageDestinationLossyCompressionQuality: JPEG_QUALITY}
            )
        if data is not None:
            self.jpeg = bytes(data)
            self.fresh.set()

    def next(self):
        self.fresh.wait(KEEPALIVE_SECONDS)
        self.fresh.clear()
        return self.jpeg

    def stop(self):
        try:
            self.stream.removeStreamOutput_type_error_(self.output, SCK.SCStreamOutputTypeScreen, None)
            _wait(self.stream.stopCaptureWithCompletionHandler_)
        except Exception:
            pass


def frames(number, width, height, logger):
    """ScreenCaptureKit's frames of the window, or polled ones where it is missing or refuses."""
    if SCK is not None:
        try:
            return _Pushed(number, width, height)
        except Exception as exc:
            logger.warning("ScreenCaptureKit failed, polling the window instead: %s", exc)
    return _Polled(number)


def bounds_of(number):
    infos = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionIncludingWindow, number) or ()
    for info in infos:
        b = info.get("kCGWindowBounds")
        if b:
            return (float(b["X"]), float(b["Y"]), float(b["Width"]), float(b["Height"]))
    return None


def _post(event):
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGEventSourceUserData, INJECTED_MARK)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


class Streamer:
    """Streams `window` (a handoff.dragged_window dict) to the PC at `endpoint` until either end
    stops: the PC's copy is closed, the window goes, or the link fails."""

    def __init__(self, endpoint, window, socket_factory, logger, alert):
        self.endpoint = endpoint
        self.window = window
        self.socket_factory = socket_factory
        self.logger = logger
        self.alert = alert
        self.stop = threading.Event()
        self.frame = window["frame"]
        self._held = set()
        self._last_click = {}

    def start(self):
        threading.Thread(target=self._run, name="Beamer-stream", daemon=True).start()

    def _run(self):
        if not Quartz.CGPreflightScreenCaptureAccess():
            Quartz.CGRequestScreenCaptureAccess()
            self.alert("Beamer", "To show a window on the PC, allow Beamer under Screen Recording in System Settings, then drag it across again.")
            return
        number = handoff.window_number(self.window)
        if number is None:
            self.logger.warning("the window to stream could not be found")
            return
        host, port, token = self.endpoint
        sock = None
        try:
            sock = self.socket_factory((host, port), timeout=CONNECT_TIMEOUT_SECONDS)
            sock.settimeout(IO_TIMEOUT_SECONDS)
            session = protocol.SecureSession(token)
            sock.sendall(session.preamble())
            protocol.recv_preamble(sock, session)
            x, y, w, h = self.frame
            hello = protocol.hello_msg(channel=CHANNEL)
            hello["data"].update(title=self.window["title"], w=int(w), h=int(h))
            protocol.send_msg(sock, session, hello)
            reply = protocol.recv_msg(sock, session)
            if reply.get("type") != protocol.MSG_WELCOME or not (reply.get("data") or {}).get("stream"):
                raise protocol.ProtocolError("the PC did not take the window")
            threading.Thread(target=self._read, args=(sock, session), name="Beamer-stream-input", daemon=True).start()
            self.logger.info("streaming %r to the PC", self.window["title"])
            source = frames(number, w, h, self.logger)
            refreshed = 0.0
            try:
                while not self.stop.is_set():
                    now = time.monotonic()
                    if now - refreshed > FRAME_REFRESH_SECONDS:
                        refreshed = now
                        bounds = bounds_of(number)
                        if bounds is None:
                            break
                        self.frame = bounds
                    jpeg = source.next()
                    if jpeg is None:
                        if isinstance(source, _Polled):
                            break
                        continue
                    protocol.send_bytes(sock, session, jpeg)
            finally:
                source.stop()
        except (OSError, protocol.ProtocolError, protocol.ConnectionClosed) as exc:
            if not self.stop.is_set():
                self.logger.warning("the window stream stopped: %s", exc)
        finally:
            self.stop.set()
            self._release()
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            self.logger.info("stopped streaming %r", self.window["title"])

    def _read(self, sock, session):
        try:
            while not self.stop.is_set():
                try:
                    message = protocol.recv_msg(sock, session)
                except OSError as exc:
                    if "timed out" in str(exc):
                        continue
                    raise
                if message.get("type") == MSG_INPUT:
                    data = message.get("data")
                    if isinstance(data, dict) and self.handle(data) == "close":
                        break
        except (OSError, protocol.ProtocolError, protocol.ConnectionClosed):
            pass
        self.stop.set()

    def point(self, data):
        x, y, w, h = self.frame
        fx = min(max(float(data.get("x", 0.0)), 0.0), 1.0)
        fy = min(max(float(data.get("y", 0.0)), 0.0), 1.0)
        return Quartz.CGPoint(x + fx * w, y + fy * h)

    def handle(self, data):
        """Posts one stream_input here; answers "close" when the PC's copy was closed."""
        kind = data.get("kind")
        if kind == "close":
            return "close"
        if kind == "key":
            name, down = data.get("name"), bool(data.get("down"))
            if isinstance(name, str) and name:
                if down:
                    # Keys go to whatever is in front: make sure that is still the streamed window's app.
                    self._activate()
                input_injector_mac.inject_key(name, down)
            return None
        point = self.point(data)
        button = data.get("button") if data.get("button") in _BUTTON_EVENTS else "left"
        down_type, up_type, drag_type, cg_button = _BUTTON_EVENTS[button]
        if kind == "move":
            held = next(iter(self._held), None)
            event_type = _BUTTON_EVENTS[held][2] if held else Quartz.kCGEventMouseMoved
            _post(Quartz.CGEventCreateMouseEvent(None, event_type, point, _BUTTON_EVENTS[held][3] if held else 0))
        elif kind in ("down", "up"):
            if kind == "down":
                self._activate()
                self._held.add(button)
            else:
                self._held.discard(button)
            event = Quartz.CGEventCreateMouseEvent(None, down_type if kind == "down" else up_type, point, cg_button)
            if kind == "down":
                clicks = input_injector_mac.plan_click_state(button, int(point.x), int(point.y), time.monotonic(), self._last_click)
            else:
                clicks = (self._last_click.get(button) or (0, 0, 0, 1))[3]
            Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, int(clicks))
            _post(event)
        elif kind == "scroll":
            _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, point, 0))
            input_injector_mac.inject_scroll(float(data.get("dy", 0.0)), float(data.get("dx", 0.0)), "line")
        return None

    def _activate(self):
        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(self.window["pid"])
        if app is not None and not app.isActive():
            app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)

    def _release(self):
        for button in list(self._held):
            try:
                self.handle({"kind": "up", "button": button, "x": 0.5, "y": 0.5})
            except Exception:
                pass
