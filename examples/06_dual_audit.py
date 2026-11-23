"""Example 6: the dual audit.  Runs a synthetic program under OMH_RUST=dual: every native step / segment is compared with the pure-Python step
on the same start state (registers, flags, writes, memory delta) and a mismatch fails the run.  Needs the Rust core (python -m emu.build)."""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
d = tempfile.mkdtemp(prefix="omh_dual_")
env = dict(os.environ, OMH_RUST="dual", OMH_DUAL_FAIL="1", OMH_DUAL_DIR=d, OMH_PREFIX="ex6_")
rc = 0
for ex in ("01_loop.py", "02_call_stub.py", "03_negative_control.py"):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "examples", ex)], env=env, cwd=ROOT, capture_output=True, text=True)
    print("%-26s rc %d  %s" % (ex, r.returncode, (r.stdout.strip().splitlines() or r.stderr.strip().splitlines() or [""])[-1][:90]))
    rc |= r.returncode
for f in sorted(os.listdir(d)):
    with open(os.path.join(d, f)) as fh:
        print(f, fh.read().splitlines()[1][:150])
sys.exit(rc)
