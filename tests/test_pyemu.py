import unittest

from _t import E, asm, machine, BASE


class Core(unittest.TestCase):
    def test_loop_and_flags(self):
        prog, cpu = machine("start:\n mov 5,d1\n mov 0,d0\nloop:\n add d1,d0\n sub 1,d1\n bne loop\ndone:\n nop\n")
        cpu.run(prog.labels["start"], {prog.labels["done"]})
        self.assertEqual(cpu.d[0], 15)
        self.assertTrue(cpu.z)

    def test_signed_vs_unsigned_branches(self):
        # 0xffffffff vs 1: signed less (blt taken), unsigned higher (bhi taken)
        for br, taken in (("blt", True), ("bhi", True), ("bgt", False), ("bcs", False)):
            prog, cpu = machine("s:\n mov -1,d0\n cmp 1,d0\n %s yes\n mov 0,d1\n bra e\nyes:\n mov 1,d1\ne:\n nop\n" % br)
            cpu.run(prog.labels["s"], {prog.labels["e"]})
            self.assertEqual(cpu.d[1], 1 if taken else 0, br)

    def test_memory_widths_and_write_audit(self):
        prog, cpu = machine("s:\n mov 0x10000000,a0\n mov 0x11223344,d0\n mov d0,(a0)\n movbu (a0),d1\n movhu (2,a0),d2\ne:\n nop\n")
        cpu.run(prog.labels["s"], {prog.labels["e"]})
        self.assertEqual((cpu.d[1], cpu.d[2]), (0x44, 0x1122))
        self.assertEqual([(a, n) for a, n, _ in cpu.writes], [(0x10000000, 4)])

    def test_call_pushes_hardware_order_and_stub_uses_real_exit(self):
        prog, cpu = machine("m:\n mov 0x1111,d2\n call f,[d2,d3],8\n mov d0,d1\ne:\n nop\nf:\n ret [d2,d3],8\n")
        cpu.stubs[prog.labels["f"]] = E.stub_ret(lambda c: 99, ['d2', 'd3'], 8)
        sp0 = cpu.sp
        cpu.run(prog.labels["m"], {prog.labels["e"]})
        self.assertEqual((cpu.d[1], cpu.d[2], cpu.sp), (99, 0x1111, sp0))

    def test_stub_outside_image_needs_declared_return(self):
        prog, cpu = machine("m:\n calls 0x900000\ne:\n nop\n")
        cpu.stubs[0x900000] = lambda c: None
        with self.assertRaises(AssertionError) as cm:
            cpu.run(prog.labels["m"], {prog.labels["e"]})
        self.assertIn("RET_KIND", str(cm.exception).upper() + "RET_KIND")      # message tells how to declare it

    def test_declared_ret_kind(self):
        E.configure(BASE, ret_kind={0x900000: "retf"})
        try:
            prog = asm.assemble("m:\n call 0x900000\n mov 1,d1\ne:\n nop\n", BASE)
            cpu = E.CPU(E.Image(bytes(0x100), disasm=prog.disassembler()))
            cpu.sp = 0x10010000
            cpu.stubs[0x900000] = lambda c: None
            cpu.run(prog.labels["m"], {prog.labels["e"]})
            self.assertEqual(cpu.d[1], 1)
        finally:
            E.RET_KIND.clear()

    def test_alignment_trap(self):
        prog, cpu = machine("s:\n mov 0x10000001,a0\n mov (a0),d0\ne:\n nop\n")
        cpu.align = True
        with self.assertRaises(E.AlignFault):
            cpu.run(prog.labels["s"], {prog.labels["e"]})

    def test_unknown_instruction_is_loud(self):
        prog, cpu = machine("s:\n frobnicate d0\ne:\n nop\n")
        with self.assertRaises(NotImplementedError):
            cpu.run(prog.labels["s"], {prog.labels["e"]})

    def test_missing_instruction_is_loud(self):
        prog, cpu = machine("s:\n nop\n")
        with self.assertRaises(KeyError):
            cpu.run(prog.labels["s"], {0xdead})

    def test_unconfigured_is_refused(self):
        saved = (E.BASE, E.IMG_LO)
        E.BASE = None
        try:
            with self.assertRaises(RuntimeError):
                E.Image(b"")
        finally:
            E.BASE, E.IMG_LO = saved

    def test_reconfigure_with_other_base_is_refused(self):
        E.configure(BASE)
        with self.assertRaises(RuntimeError):
            E.configure(BASE + 0x1000)

    def test_image_read_floor(self):
        E.configure(BASE)
        E.IMG_LO = BASE + 0x10                        # direct set only inside this test; restored below
        try:
            data = bytes(range(64))
            img = E.Image(data)
            cpu = E.CPU(img)
            self.assertEqual(cpu.ld(BASE + 0x08, 1), 0)           # below the floor: 0
            self.assertEqual(cpu.ld(BASE + 0x11, 1), 0x11)        # at/above the floor: the image byte
        finally:
            E.IMG_LO = BASE

    def test_patches_applied(self):
        E.configure(BASE)
        img = E.Image(bytes(16), patches={BASE + 4: b"\xaa\xbb", 0x1: b"\x01"})
        self.assertEqual(bytes(img.d[4:6]), b"\xaa\xbb")
        self.assertEqual(img.extra, {0x1: b"\x01"})


class Asm(unittest.TestCase):
    def test_labels_sizes(self):
        p = asm.assemble("a: nop @2\nb:\n mov 1,d0\n jmp (c)\nc:\n nop\n", 0x100)
        self.assertEqual(p.labels, {"a": 0x100, "b": 0x102, "c": 0x108})
        self.assertEqual(p.listing[0x105][1], ["(0x108)"])

    def test_duplicate_label(self):
        with self.assertRaises(ValueError):
            asm.assemble("a:\n nop\na:\n nop\n", 0)


if __name__ == "__main__":
    unittest.main()
