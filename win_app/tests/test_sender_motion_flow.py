"""GH15 sender batching: preserve travel and barriers before any socket work."""

import unittest
import collections
from unittest import mock

from links_rig import B, Rig
from core import owner, protocol
import sender


class SenderMotionFlowTests(unittest.TestCase):
    def rig(self):
        rig = Rig()
        rig.links.caps_of[B] = rig.links.caps(B) | {"motion_path"}
        return rig

    def test_motion_waiting_for_clipboard_is_bounded_and_controls_keep_their_place(self):
        rig = self.rig()
        rig.sender._clip_wait[B] = collections.deque()
        for index in range(1000):
            rig.sender._deliver(owner.Send(B, protocol.mousemove_msg(1, 1 if index % 2 else -1)))
        rig.sender._deliver(owner.Send(B, protocol.key_msg("keydown", "a")))
        rig.sender._deliver(owner.Send(B, protocol.mousemove_msg(2, 3)))
        waiting = rig.sender._clip_wait[B]
        self.assertLessEqual(len(waiting), 18)
        self.assertEqual(sum(a.message["data"]["dx"] for a in list(waiting)[:-2]), 1000)
        self.assertEqual(sum(a.message["data"]["dy"] for a in list(waiting)[:-2]), 0)
        self.assertEqual(waiting[-2].message["type"], "keydown")

    def test_worker_sends_first_immediately_and_flushes_trailing_motion_when_idle(self):
        rig = self.rig()
        sent = []
        clock = [10.0]
        sleeps = []

        def deliver(action):
            sent.append(action.message)
            if len(sent) == 1:
                rig.sender._queue([owner.Send(B, protocol.mousemove_msg(2, 3))])
            else:
                rig.sender._stop_event.set()

        def sleep(delay):
            sleeps.append(delay)
            clock[0] += delay
            rig.sender._queue([owner.Send(B, protocol.mousemove_msg(4, 5))])

        rig.sender._deliver = deliver
        rig.sender._queue([owner.Send(B, protocol.mousemove_msg(1, 1))])
        with mock.patch.object(sender.time, "monotonic", side_effect=lambda: clock[0]), mock.patch.object(sender.time, "sleep", side_effect=sleep):
            rig.sender._outbound_worker()
        self.assertEqual([(m["data"]["dx"], m["data"]["dy"]) for m in sent], [(1, 1), (6, 8)])
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], .002)
        self.assertEqual(rig.sender._outbound.unfinished_tasks, 0)

    def test_pending_high_polling_samples_are_bounded_relative_batches(self):
        rig = self.rig()
        rig.sender.set_redirecting(True)
        rig.flush()
        for _ in range(1000):
            rig.sender.on_motion(1, 2)
        self.assertLessEqual(rig.sender._outbound.qsize(), 16)
        moves = rig.sent(B, "mousemove")
        self.assertEqual((sum(m["data"]["dx"] for m in moves), sum(m["data"]["dy"] for m in moves)), (1000, 2000))

    def test_buttons_are_batch_boundaries_and_reversing_jitter_is_summed(self):
        rig = self.rig()
        rig.sender.set_redirecting(True)
        rig.flush()
        rig.links.sent.clear()
        rig.sender.on_motion(1, 0)
        rig.sender.on_motion(0, 2)
        rig.sender.on_button("left", True)
        rig.sender.on_motion(3, 4)
        rig.sender.on_motion(-2, -1)
        messages = rig.sent(B)
        self.assertEqual([m["type"] for m in messages], ["mousemove", "mousedown", "mousemove"])
        self.assertEqual((messages[0]["data"]["dx"], messages[0]["data"]["dy"]), (1, 2))
        self.assertEqual((messages[2]["data"]["dx"], messages[2]["data"]["dy"]), (1, 3))

    def test_high_polling_sign_jitter_is_bounded_and_keeps_net_travel(self):
        rig = self.rig()
        rig.sender.set_redirecting(True)
        rig.flush()
        for index in range(1000):
            rig.sender.on_motion(1, 1 if index % 2 else -1)
        self.assertLessEqual(rig.sender._outbound.qsize(), 16)
        moves = rig.sent(B, "mousemove")
        self.assertEqual((sum(m["data"]["dx"] for m in moves), sum(m["data"]["dy"] for m in moves)), (1000, 0))


if __name__ == "__main__":
    unittest.main()
