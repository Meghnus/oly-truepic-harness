"""shared setup for the examples: put the repository on sys.path and configure pyemu with a SYNTHETIC base (no firmware anywhere)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ofh.emu import asm, pyemu as E          # noqa: E402

BASE = 0x400000            # arbitrary: nothing here is an address of any real camera


def machine(src, data_size=0x1000, base=BASE):
    """assemble `src`, build an Image whose decoder is the listing, return (program, cpu).  Stack at 0x10010000, data at 0x10000000."""
    E.configure(base)
    prog = asm.assemble(src, base)
    img = E.Image(bytes(data_size), disasm=prog.disassembler())
    cpu = E.CPU(img)
    cpu.sp = 0x10010000
    return prog, cpu
