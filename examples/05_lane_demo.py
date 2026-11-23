"""Example 5: the lane.  Runs examples/lane_spec.json (3 synthetic stages) in parallel with the RAM gate and the io audit, then prints the
summary table.  Add `--rust 1` after building the Rust core to see the A/B proof instead:
    python examples/05_lane_demo.py            # pure Python
    python examples/05_lane_demo.py --ab       # Rust vs Python, then ofh.lane.cmp_runs --rs"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = os.path.join(ROOT, "examples", "lane_spec.json")
tmp = tempfile.mkdtemp(prefix="omh_lane_")
if "--ab" in sys.argv:
    rc = subprocess.run([sys.executable, "-m", "ofh.lane.ab", spec, "--run-a", os.path.join(tmp, "A"), "--run-b", os.path.join(tmp, "B"),
                         "--pool", "3", "--settle", "0.5", "--poll", "0.3"], cwd=ROOT).returncode
else:
    rc = subprocess.run([sys.executable, "-m", "ofh.lane.driver", spec, "--run", os.path.join(tmp, "run"), "--pool", "3", "--settle", "0.5",
                         "--poll", "0.3"], cwd=ROOT).returncode
print("exit code", rc, "(run dir %s)" % tmp)
sys.exit(rc)
