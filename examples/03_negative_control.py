"""Example 3: the METHOD.  A check that cannot fail proves nothing.  Here the "patch" is a routine that should clamp d0 to 100.  We check it on
good inputs (positive control), and run the SAME check on a deliberately broken variant (negative control: it MUST fail there).  The control is
judged BY VALUE - 'the mutant returned 250, the original returned 100' - never by 'the check printed FAIL somewhere'.  See docs/method.md."""
from _common import machine, E

CLAMP = """
    entry:
        cmp 100,d0
        ble ok                 ; signed: d0 <= 100 -> keep
        mov 100,d0
    ok:
        nop
    exit:
        nop
"""
BROKEN = CLAMP.replace("mov 100,d0", "mov 250,d0")       # the mutant: clamps to the wrong value


def run(src, x):
    prog, cpu = machine(src)
    cpu.d[0] = x
    cpu.run(prog.labels["entry"], {prog.labels["exit"]})
    return cpu.d[0]


good = [(5, 5), (100, 100), (101, 100), (9999, 100)]
for x, want in good:
    got = run(CLAMP, x)
    print("original  x=%-5d -> %-4d (want %d)" % (x, got, want))
    assert got == want
bad = run(BROKEN, 9999)
print("mutant    x=9999  -> %d  (original gives %d)" % (bad, run(CLAMP, 9999)))
assert bad == 250 and bad != run(CLAMP, 9999), "negative control did not bite: the check cannot tell a broken patch from a good one"
print("negative control bit by value: the check distinguishes the mutant")
