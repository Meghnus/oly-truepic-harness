"""stage 'stub': a stubbed service call keeps the stack discipline."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ofh.lane import stage          # noqa: E402
from _common import machine, E  # noqa: E402


def run(ctx):
    prog, cpu = machine("""
        main:
            mov 7,d0
            mov 0x1111,d2
            call svc,[d2,d3],8
            nop
        end:
            nop
        svc:
            ret [d2,d3],8
    """)
    cpu.stubs[prog.labels["svc"]] = E.stub_ret(lambda c: c.d[0] * 6, ['d2', 'd3'], 8)
    sp0 = cpu.sp
    cpu.run(prog.labels["main"], {prog.labels["end"]})
    ctx.sub("stub result and callee-saved regs", cpu.d[0] == 42 and cpu.d[2] == 0x1111, ["d0=%d d2=%#x" % (cpu.d[0], cpu.d[2])])
    ctx.sub("stack restored", cpu.sp == sp0, ["sp delta %d" % (cpu.sp - sp0)])


stage.main(run)
