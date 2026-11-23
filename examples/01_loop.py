"""Example 1: the smallest possible run.  Sum 5+4+3+2+1 with a setlb-free countdown loop, on the Python core (or the Rust core:
`OMH_RUST=1 python examples/01_loop.py` runs the very same thing natively).  No firmware, no profile."""
from _common import machine

prog, cpu = machine("""
    start:
        mov 5,d1
        mov 0,d0
    loop:
        add d1,d0
        sub 1,d1
        bne loop
    done:
        nop
""")
cpu.run(prog.labels["start"], {prog.labels["done"]})
print("d0 =", cpu.d[0], "(expected 15)   steps =", cpu.steps, "   flags z/n/c/v =", cpu.z, cpu.n, cpu.c, cpu.v)
assert cpu.d[0] == 15
