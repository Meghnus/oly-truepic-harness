"""shared helpers for the tests: repo on sys.path; a synthetic machine; skip helpers.  No firmware is ever needed."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ofh.emu import asm, pyemu as E          # noqa: E402

BASE = 0x400000


def machine(src, base=BASE, data=0x1000):
    E.configure(base)
    prog = asm.assemble(src, base)
    img = E.Image(bytes(data), disasm=prog.disassembler())
    cpu = E.CPU(img)
    cpu.sp = 0x10010000
    return prog, cpu


def rust_built():
    try:
        from ofh.emu import rs_shim          # noqa: F401
        return True
    except ImportError:
        return False


needs_rust = unittest.skipUnless(rust_built(), "Rust core not built (python -m ofh.emu.build)")
