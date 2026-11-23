import os
import struct
import sys
import tempfile
import unittest

from _t import ROOT

sys.path.insert(0, os.path.join(ROOT, "tools"))
import leakcheck as L          # noqa: E402


def encode_container(payloads, model=b"\x11\x30"):
    """the documented container transform, forwards (test helper): header 32 B, payload (16-byte groups permuted + ^0xff), trailer 32 B"""
    out = bytearray()
    for pay in payloads:
        assert len(pay) % 16 == 0
        hdr = bytearray(32)
        hdr[0:2] = b"OE"
        hdr[2:4] = model
        struct.pack_into("<I", hdr, 12, len(pay))
        enc = bytearray(len(pay))
        for g in range(0, len(pay), 16):
            for i in range(16):
                enc[g + L.PERM[i]] = pay[g + i] ^ 0xff
        out += hdr + enc + bytes(32)
    return bytes(out)


class Leak(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = L.ROOT
        L.ROOT = self.tmp.name

    def tearDown(self):
        L.ROOT = self.old
        self.tmp.cleanup()

    def put(self, name, data):
        p = os.path.join(self.tmp.name, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    def test_container_roundtrip_decodes_what_was_encoded(self):
        pays = [bytes(range(256)) * 2, bytes(reversed(range(256)))]
        got = [p for _, p in L.decode_container(encode_container(pays))]
        self.assertEqual(got, pays)

    def test_forbidden_types_names_and_container_detected(self):
        self.put("a.bin", b"x")
        self.put("my_dump.txt", b"x")
        self.put("ok.txt", b"hello")
        self.put("disguised.txt", encode_container([bytes(range(16))]))
        probs = []
        L.check_files(probs)
        txt = "\n".join(probs)
        self.assertIn("a.bin: forbidden", txt)
        self.assertIn("my_dump.txt: name", txt)
        self.assertIn("disguised.txt: starts like an OM firmware container", txt)
        self.assertNotIn("ok.txt", txt)

    def test_big_hex_and_base64_and_size(self):
        self.put("h.md", b"x " + b"ab12" * 60)
        self.put("b.md", b"A" * 400)
        self.put("big.txt", b"a " * (700 * 1024))
        probs = []
        L.check_files(probs)
        txt = "\n".join(probs)
        self.assertIn("h.md: contains a hex string", txt)
        self.assertIn("b.md: contains a long base64", txt)
        self.assertIn("big.txt", txt)

    def test_address_literals_need_justification(self):
        self.put("x.py", b"A = 0x12345678\nB = 0xffffffff\nC = 0x10\n")
        self.put("tools/addr_allowlist.txt", b"*.py 0xffffffff   # 32-bit mask\n")
        names = ["x.py"]
        rules = L.load_allowlist()
        probs, seen = L.scan_addresses(names, rules)
        self.assertEqual(len(probs), 1)
        self.assertIn("0x12345678", probs[0])
        self.assertEqual(len(seen), 2)                    # 0x10 is not address-like

    def test_allowlist_requires_justification(self):
        self.put("tools/addr_allowlist.txt", b"*.py 0xffffffff\n")
        with self.assertRaises(SystemExit):
            L.load_allowlist()

    def test_stock_run_detected_incl_decoded_partition(self):
        import random
        rng = random.Random(5)
        stock_payload = bytes(rng.randrange(256) for _ in range(4096))      # non-periodic: a shared run has exactly one place
        shared = stock_payload[1000:1040]                              # 40 bytes of "firmware"
        self.put("doc.md", b"intro text " + shared + b" outro")
        self.put("clean.md", b"nothing here that is in any firmware image at all, really nothing")
        stock = os.path.join(self.tmp.name, "..", "stock_%d.BIN" % os.getpid())
        with open(stock, "wb") as f:
            f.write(encode_container([stock_payload]))
        try:
            bufs = L.stock_buffers(stock, None)
            probs = L.scan_stock(["doc.md", "clean.md"], bufs)
        finally:
            os.remove(stock)
        self.assertTrue(any("doc.md: 40-byte run" in p and "decoded partition" in p for p in probs), probs)
        self.assertFalse(any("clean.md" in p for p in probs))

    def test_low_entropy_runs_ignored(self):
        self.put("zeros.md", b"\x00" * 64 + b"\xff" * 64)
        probs = L.scan_stock(["zeros.md"], [("s", b"\x00" * 1000 + b"\xff" * 1000)])
        self.assertEqual(probs, [])

    def test_below_16_bytes_not_reported(self):
        self.put("short.md", b"0123456789abcde")                      # 15 bytes
        self.assertEqual(L.scan_stock(["short.md"], [("s", b"xx0123456789abcdexx")]), [])


class RepoItself(unittest.TestCase):
    def test_repository_passes_leakcheck(self):
        """the shipped tree itself: no forbidden file, no firmware-looking blob, every address-like literal justified"""
        probs = []
        names = L.check_files(probs)
        rules = L.load_allowlist()
        aprobs, _ = L.scan_addresses(names, rules)
        self.assertEqual(probs + aprobs, [])


if __name__ == "__main__":
    unittest.main()
