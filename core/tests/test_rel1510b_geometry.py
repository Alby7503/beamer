"""Queued motion must retain the crossing decisions made by individual samples."""

import unittest
from types import SimpleNamespace

from core import protocol, receiver, return_edge
from core.tests.responder_harness import B, HERE, FakeDesktop, Machine, entry


class ReceiverQueuedGeometryTests(unittest.TestCase):
    def deliver(self, moves, pointer, model, queued):
        desktop = FakeDesktop(cursor=pointer)
        machine = Machine([entry(B, "Driver")], desktop=desktop)
        link = receiver._Link(
            SimpleNamespace(shutdown=lambda _how: None), None, B, None,
            "127.0.0.1", "Driver", [], HERE, [], True,
        )
        responder = machine.responder
        responder._owner = link
        responder._armed = [(B, model)]
        responder._zones_live = True
        original = machine.injector.inject_mouse_move

        def move_pointer(dx, dy):
            original(dx, dy)
            x, y = desktop.cursor
            bounds = desktop.monitors()[0]
            desktop.cursor = (
                min(bounds.right, max(bounds.x, x + dx)),
                min(bounds.bottom, max(bounds.y, y + dy)),
            )

        machine.injector.inject_mouse_move = move_pointer
        messages = [protocol.mousemove_msg(dx, dy, seq)
                    for seq, (dx, dy) in enumerate(moves, 1)]
        if queued:
            for message in messages:
                link.receive(message, len(protocol.dumps_message(message)))
            messages = [item[0] for item in link.inbox]
        for message in messages:
            responder._dispatch(link, message)
        switches = [message for message in link.outbox
                    if message.get("type") == protocol.MSG_SWITCH]
        return desktop.cursor, len(switches)

    def test_queued_diagonal_motion_does_not_cross_after_leaving_selected_part(self):
        def run(queued):
            return self.deliver(
                [(1, 4), (200, 1)], (1919, 358),
                return_edge.PartEdge("right", ["start"], resistance_px=120, clock=lambda: 0),
                queued,
            )

        direct = run(False)
        self.assertEqual(direct, ((1919, 363), 0))
        self.assertEqual(run(True), direct)

    def test_queued_diagonal_motion_keeps_corner_push_pressure(self):
        def run(queued):
            return self.deliver(
                [(119, 1), (1, 119)], (1919, 1079),
                return_edge.CornerPush("bottom_right", "right", resistance_px=200, clock=lambda: 0),
                queued,
            )

        direct = run(False)
        self.assertEqual(direct, ((1919, 1079), 1))
        self.assertEqual(run(True), direct)


if __name__ == "__main__":
    unittest.main()
