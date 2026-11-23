"""Example 2: calling a service you do not want to emulate.  `call f,[d2,d3],8` pushes d2,d3 and the return address exactly like the hardware
does; the callee is replaced by a STUB.  pyemu finds the stub's REAL exit (here: the `ret [d2,d3],8` at the stub address in the listing)
and uses it, so the stack, MDR and the callee-saved registers come out right even though no callee code ran.  A stub for code OUTSIDE the
image has no listing to scan: declare how it returns with pyemu.configure(..., ret_kind={addr: 'retf'}) (profile key ret_kind)."""
from _common import machine, E

prog, cpu = machine("""
    main:
        mov 0x10000100,a0       ; a0 -> a data word
        mov 7,d0
        mov 0x1111,d2           ; callee-saved: must survive the stubbed call
        call service,[d2,d3],8
        mov d0,d1               ; d1 <- the stub's result
        nop
    end:
        nop
    service:                    ; the real callee would be here; the stub replaces its effect
        ret [d2,d3],8
""")
seen = []
cpu.stubs[prog.labels["service"]] = E.stub_ret(lambda c: (seen.append(c.d[0]) or c.d[0] * 6), ['d2', 'd3'], 8)
sp0 = cpu.sp
cpu.run(prog.labels["main"], {prog.labels["end"]})
print("stub saw d0 =", seen, "; d1 =", cpu.d[1], "; d2 =", hex(cpu.d[2]), "; sp restored:", cpu.sp == sp0)
assert seen == [7] and cpu.d[1] == 42 and cpu.d[2] == 0x1111 and cpu.sp == sp0
