"""stage 'loop': a countdown loop on the emulator + a negative control judged by value."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lane import stage          # noqa: E402
from _common import machine     # noqa: E402

SRC = """
    start:
        mov 6,d1
        mov 0,d0
    loop:
        add d1,d0
        sub 1,d1
        bne loop
    done:
        nop
"""


def total(src):
    prog, cpu = machine(src)
    cpu.run(prog.labels["start"], {prog.labels["done"]})
    return cpu.d[0]


def run(ctx):
    ctx.sub("sum 6..1 = 21", total(SRC) == 21, ["d0=%d" % total(SRC)])
    mutant = total(SRC.replace("sub 1,d1", "sub 2,d1"))          # 6+4+2 = 12, then d1 hits 0 -> a different value
    ctx.neg("loop step mutated (sub 2) is detected", mutant != 21, ["mutant d0=%d, original d0=21" % mutant])


stage.main(run)
