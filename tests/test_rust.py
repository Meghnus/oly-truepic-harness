"""tests of the Rust core against the Python reference: skipped (loudly named) when the module is not built"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from _t import ROOT, needs_rust, rust_built


def run_py(code, env=None, cwd=ROOT):
    return subprocess.run([sys.executable, "-c", code], cwd=cwd, env=dict(os.environ, **(env or {})), capture_output=True, text=True)


class LoudFailures(unittest.TestCase):
    def test_invalid_value_is_fatal_86(self):
        r = run_py("from emu import pyemu; pyemu.configure(0x400000)", {"OMH_RUST": "bogus"})
        self.assertEqual(r.returncode, 86, r.stderr)
        self.assertIn("is not 0, 1 or dual", r.stderr)

    def test_missing_build_is_fatal_86_not_a_python_run(self):
        """copy the python sources WITHOUT the built module: a run that asks for Rust must die with 86"""
        with tempfile.TemporaryDirectory() as d:
            for pkg in ("emu", "mem", "difftest"):
                shutil.copytree(os.path.join(ROOT, pkg), os.path.join(d, pkg),
                                ignore=shutil.ignore_patterns("_rs_emu*", "target", "__pycache__", "*.pyd", "*.so"))
            r = run_py("from emu import pyemu; pyemu.configure(0x400000); print('RAN PURE PYTHON')", {"OMH_RUST": "1"}, cwd=d)
            self.assertEqual(r.returncode, 86, (r.stdout, r.stderr))
            self.assertNotIn("RAN PURE PYTHON", r.stdout)

    @needs_rust
    def test_stale_build_is_refused(self):
        """edit a source file in a copy of the crate: its hash no longer equals the compiled-in SRC_SHA"""
        from emu import rs_shim, rs_srchash
        with tempfile.TemporaryDirectory() as d:
            crate = os.path.join(d, "rs_emu")
            shutil.copytree(os.path.join(ROOT, "emu", "rs_emu"), crate, ignore=shutil.ignore_patterns("target"))
            self.assertEqual(rs_srchash.src_sha(crate), rs_shim._R.SRC_SHA, "a pristine copy hashes to the compiled-in value")
            with open(os.path.join(crate, "src", "lib.rs"), "a") as f:
                f.write("\n// edit\n")
            self.assertNotEqual(rs_srchash.src_sha(crate), rs_shim._R.SRC_SHA)

    @needs_rust
    def test_core_is_really_native(self):
        r = run_py("from emu import pyemu, rs_shim; import sys\n"
                   "sys.path.insert(0, 'examples')\n"
                   "from _common import machine\n"
                   "prog, cpu = machine('s:\\n mov 3,d0\\n add d0,d0\\ne:\\n nop\\n')\n"
                   "cpu.run(prog.labels['s'], {prog.labels['e']})\n"
                   "assert pyemu.CPU.step.__module__ == 'emu.rs_shim', pyemu.CPU.step.__module__\n"
                   "assert rs_shim._R.stats()['native'] >= 2, rs_shim._R.stats()\n"
                   "print('native', rs_shim._R.stats()['native'], 'd0', cpu.d[0])", {"OMH_RUST": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("d0 6", r.stdout)


@needs_rust
class Differential(unittest.TestCase):
    def test_cpu_lockstep_fuzz(self):
        r = run_py("from difftest import cpufuzz; s, rs = cpufuzz.fuzz_lockstep(250, 5); print(s, rs['native'])")
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])

    def test_run_cut_fuzz(self):
        r = run_py("from difftest import cpufuzz; s, rs = cpufuzz.fuzz_runcut(150, 6); print(s)")
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])

    def test_memory_fuzz_and_journal(self):
        r = run_py("from difftest import memfuzz; print(memfuzz.fuzz_mem(60, 7), memfuzz.fuzz_journal(40, 8))")
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])

    def test_dual_mode_on_examples(self):
        with tempfile.TemporaryDirectory() as d:
            for ex in ("01_loop.py", "02_call_stub.py", "03_negative_control.py"):
                r = subprocess.run([sys.executable, os.path.join(ROOT, "examples", ex)], cwd=ROOT, capture_output=True, text=True,
                                   env=dict(os.environ, OMH_RUST="dual", OMH_DUAL_FAIL="1", OMH_DUAL_DIR=d))
                self.assertEqual(r.returncode, 0, (ex, r.stderr[-1500:]))
            files = os.listdir(d)
            self.assertTrue(files)
            import ast
            tot = 0
            for f in files:
                with open(os.path.join(d, f)) as fh:
                    c = ast.literal_eval(fh.read().splitlines()[1])
                self.assertEqual(c["mismatch"], 0)
                tot += c["segments"]
            self.assertGreater(tot, 0, "dual verified nothing")

    def test_dual_detects_a_planted_mismatch(self):
        """NEGATIVE CONTROL of the dual audit: make the Python REFERENCE disagree with the native engine (a one-bit error after the second
        step, installed before dual captures the reference _step) and require the audit to catch it by value: AssertionError naming the
        mismatch, exit 1.  The same program without the sabotage (test_dual_mode_on_examples) must be clean."""
        code = ("import sys\nsys.path.insert(0, 'examples')\n"
                "from emu import pyemu as E\n"
                "orig = E.CPU._step\n"
                "def bad(self):\n"
                "    orig(self)\n"
                "    if self.steps == 2:\n"
                "        self.d[0] ^= 1\n"
                "E.CPU._step = bad\n"
                "from _common import machine\n"
                "prog, cpu = machine('s:\\n mov 3,d0\\n add d0,d0\\n add d0,d0\\ne:\\n nop\\n')\n"
                "cpu.run(prog.labels['s'], {prog.labels['e']})\n"
                "print('NOT DETECTED d0 =', cpu.d[0])\n")
        with tempfile.TemporaryDirectory() as d:
            r = run_py(code, {"OMH_RUST": "dual", "OMH_DUAL_FAIL": "1", "OMH_DUAL_DIR": d})
        self.assertEqual(r.returncode, 1, (r.stdout, r.stderr[-1500:]))
        self.assertIn("difftest.dual MISMATCH", r.stderr)
        self.assertNotIn("NOT DETECTED", r.stdout)
        # and counted (not raised) when OMH_DUAL_FAIL is off, so lane.cmp_runs --dual can fail the run afterwards
        with tempfile.TemporaryDirectory() as d:
            r = run_py(code, {"OMH_RUST": "dual", "OMH_DUAL_DIR": d})
            import ast
            counters = []
            for f in os.listdir(d):
                with open(os.path.join(d, f)) as fh:
                    counters.append(ast.literal_eval(fh.read().splitlines()[1]))
        self.assertEqual(sum(c["mismatch"] for c in counters), 1, counters)


if __name__ == "__main__":
    unittest.main()
