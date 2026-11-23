"""Example 4: random programs, lock-step.  Generates random instruction streams (every mnemonic the core knows, every operand form incl.
malformed ones), runs them on pyemu AND on the Rust core and compares the complete state after every step.  This is how you trust (or distrust)
the fast core on code you have never seen.  Needs `python -m emu.build` first."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from difftest import cpufuzz
    cpufuzz.setup(1)
except ImportError as e:
    sys.exit("the Rust core is not built (%s): run `python -m emu.build` first" % e)
n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
stats, rs = cpufuzz.fuzz_lockstep(n, 1)
print("lock-step:", stats)
stats, _ = cpufuzz.fuzz_runcut(n // 2, 2)
print("run()-cut :", stats)
print("native steps executed by the Rust core:", rs["native"], "; python-routed:", rs["py_steps"])
