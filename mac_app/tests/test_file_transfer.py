import os
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import file_transfer
import protocol


def _pair():
    a, b = socket.socketpair()
    sa, sb = protocol.SecureSession("t"), protocol.SecureSession("t")
    sa.accept_preamble(sb.preamble())
    sb.accept_preamble(sa.preamble())
    return (a, sa), (b, sb)


class FileTransferTests(unittest.TestCase):
    def test_files_and_a_folder_arrive_whole_beside_what_is_already_there(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            big = os.urandom(file_transfer.CHUNK_BYTES * 2 + 7)
            with open(os.path.join(src, "big.bin"), "wb") as f:
                f.write(big)
            os.makedirs(os.path.join(src, "Docs", "sub"))
            with open(os.path.join(src, "Docs", "sub", 'a:b?.txt'), "w") as f:
                f.write("hi")
            open(os.path.join(dst, "big.bin"), "w").close()
            (a, sa), (b, sb) = _pair()
            sender = threading.Thread(
                target=file_transfer.send, args=(a, sa, [os.path.join(src, "big.bin"), os.path.join(src, "Docs")])
            )
            sender.start()
            landed = file_transfer.receive(b, sb, dst)
            sender.join()
            a.close()
            b.close()
            self.assertEqual(landed, [os.path.join(dst, "big (2).bin"), os.path.join(dst, "Docs")])
            with open(landed[0], "rb") as f:
                self.assertEqual(f.read(), big)
            with open(os.path.join(dst, "Docs", "sub", "a_b_.txt")) as f:
                self.assertEqual(f.read(), "hi")
            self.assertFalse([n for _, _, files in os.walk(dst) for n in files if n.endswith(".part")])

    def test_a_name_that_leaves_the_folder_is_refused(self):
        for name in ("../evil", "/etc/passwd", "a//b", "", "."):
            with self.subTest(name=name), self.assertRaises(protocol.ProtocolError):
                file_transfer.clean_name(name)
        self.assertEqual(file_transfer.clean_name("con.txt"), ["_con.txt"])

    def test_only_inert_files_are_opened(self):
        with tempfile.TemporaryDirectory() as folder:
            for name, expected in (("report.pdf", True), ("Photo.JPG", True), ("setup.EXE", False), ("run.command", False), ("go.lnk", False), ("page.html", False), ("noext", False)):
                path = os.path.join(folder, name)
                open(path, "w").close()
                self.assertEqual(file_transfer.safe_to_open(path), expected, name)
            self.assertFalse(file_transfer.safe_to_open(folder))


if __name__ == "__main__":
    unittest.main()
