import os
import subprocess
import sys
import unittest

from _t import ROOT, needs_rust


def run(ex, *args, env=None):
    return subprocess.run([sys.executable, os.path.join(ROOT, "examples", ex)] + list(args), cwd=ROOT, capture_output=True, text=True,
                          env=dict(os.environ, **(env or {})))


class Examples(unittest.TestCase):
    def test_01(self):
        r = run("01_loop.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("d0 = 15", r.stdout)

    def test_02(self):
        self.assertEqual(run("02_call_stub.py").returncode, 0)

    def test_03_negative_control(self):
        r = run("03_negative_control.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("bit by value", r.stdout)

    def test_05_lane(self):
        r = run("05_lane_demo.py")
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-1500:])
        self.assertIn("ALL PASS", r.stdout)

    @needs_rust
    def test_04_random(self):
        self.assertEqual(run("04_random_programs.py", "120").returncode, 0)

    @needs_rust
    def test_06_dual(self):
        r = run("06_dual_audit.py")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    @needs_rust
    def test_bench_runs_and_rust_wins(self):
        r = run("bench/bench.py", "--mb", "0.02", "--step-count", "5000", "--skip-shm")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rows = {ln.split()[0]: ln.split() for ln in r.stdout.splitlines() if ln.split()[:1] in (["py-dict"], ["py-page"], ["rust"])}
        self.assertEqual(set(rows), {"py-dict", "py-page", "rust"}, r.stdout)
        self.assertGreater(float(rows["rust"][2].replace(",", "")), float(rows["py-dict"][2].replace(",", "")), "rust run() must beat python run()")
    @needs_rust
    def test_examples_agree_on_rust(self):
        for ex in ("01_loop.py", "02_call_stub.py", "03_negative_control.py"):
            a, b = run(ex), run(ex, env={"OMH_RUST": "1"})
            self.assertEqual(a.stdout, b.stdout, ex)


if __name__ == "__main__":
    unittest.main()
