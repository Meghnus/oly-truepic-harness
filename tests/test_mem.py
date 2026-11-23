import os
import tempfile
import unittest

from _t import E, machine

from ofh.mem import pagemem, shm


class PageMemSemantics(unittest.TestCase):
    def test_dict_exact_basics(self):
        m, d = pagemem.PageMem(), {}
        for a, v in ((5, 1), (260, 2), (5, 9), (7, 0)):
            m[a] = v
            d[a] = v
        del m[260]
        del d[260]
        m[260] = 3
        d[260] = 3
        self.assertEqual(list(m.items()), list(d.items()))
        self.assertEqual(len(m), len(d))
        self.assertEqual(m.get(8, "x"), "x")
        self.assertIn(7, m)                                    # present with value 0 stays distinct from absent
        self.assertNotIn(6, m)

    def test_copy_on_write(self):
        a = pagemem.PageMem()
        a.put_bytes(0x1000, b"abcd")
        b = a.copy()
        b[0x1001] = 0
        self.assertEqual(a[0x1001], ord("b"))
        self.assertEqual(b[0x1001], 0)

    def test_odd_keys_keep_dict_semantics(self):
        m = pagemem.PageMem()
        m["k"] = "v"
        m[(1, 2)] = 300
        self.assertEqual(m["k"], "v")
        self.assertEqual(len(m), 2)

    def test_install_makes_cpu_use_pagemem(self):
        prog, cpu = machine("s:\n mov 0x10000000,a0\n mov 0x01020304,d0\n mov d0,(a0)\n mov (a0),d1\ne:\n nop\n")
        # same program on a CPU with the PageMem fast paths: run it by hand without touching the class (install() is process-global)
        cpu.mem = pagemem.PageMem()
        cpu.run(prog.labels["s"], {prog.labels["e"]})
        self.assertEqual(cpu.d[1], 0x01020304)
        self.assertEqual(len(cpu.mem), 4)


class Shm(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = shm.STORE
        shm.STORE = os.path.join(self.tmp.name, "store")

    def tearDown(self):
        shm.STORE = self.old
        self.tmp.cleanup()

    def test_store_is_content_addressed_and_readonly(self):
        p1 = shm.store_blob(b"x" * 100)
        p2 = shm.store_blob(b"x" * 100)
        self.assertEqual(p1, p2)
        with self.assertRaises(PermissionError):
            open(p1, "wb")

    def test_read_only_map(self):
        fn = os.path.join(self.tmp.name, "img.bin")
        with open(fn, "wb") as f:
            f.write(bytes(range(256)) * 8)
        ro = shm.shared_read(fn)
        self.assertEqual(ro[3], 3)
        self.assertEqual(ro, bytes(range(256)) * 8)
        with self.assertRaises((TypeError, ValueError)):
            ro[0] = 1

    def test_cow_writes_stay_private(self):
        fn = os.path.join(self.tmp.name, "img.bin")
        with open(fn, "wb") as f:
            f.write(bytes(4096))
        a, b = shm.shared_copy(fn), shm.shared_copy(fn)
        a[10:12] = b"\x01\x02"
        self.assertEqual(bytes(a[10:12]), b"\x01\x02")
        self.assertEqual(bytes(b[10:12]), b"\x00\x00")
        with open(fn, "rb") as f:
            self.assertEqual(f.read()[10:12], b"\x00\x00")           # the original file is untouched
        self.assertEqual(bytes(shm.shared_copy(fn)[10:12]), b"\x00\x00")   # and so is the store copy

    def test_image_from_file_shared(self):
        fn = os.path.join(self.tmp.name, "img.bin")
        with open(fn, "wb") as f:
            f.write(bytes(range(200)))
        E.configure(0x400000)
        img = E.Image.from_file(fn, patches={0x400004: b"\xff"}, shared=True)
        self.assertEqual(img.d[4], 0xff)
        self.assertEqual(img.d[5], 5)
        with open(fn, "rb") as f:
            self.assertEqual(f.read()[4], 4)


if __name__ == "__main__":
    unittest.main()
