import json
import os
import subprocess
import sys
import tempfile
import unittest

from _t import ROOT

from ofh.lane import cmp_runs, driver, ram, stage


def write(p, text):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


class Gate(unittest.TestCase):
    def test_floor_and_buffer(self):
        self.assertTrue(ram.gate_ok(10, 2, 1.5, 2.0, 0.6))
        self.assertFalse(ram.gate_ok(1.9, 0, 0.1, 2.0, 0.6), "below the absolute floor")
        self.assertFalse(ram.gate_ok(5, 3.0, 1.5, 2.0, 0.6), "growth + estimate would eat the buffer: 5-3-1.5=0.5 < 0.6")
        self.assertTrue(ram.gate_ok(5, 3.0, 1.4, 2.0, 0.6))

    def test_growth(self):
        g = ram.growth_gb(["a", "b"], {"a": 2048, "b": 1024}, {"a": 1024, "b": 4000})
        self.assertAlmostEqual(g, 1.0)                       # a still grows 1 GB, b is over its estimate: 0


class Spec(unittest.TestCase):
    def mk(self, stages):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.json")
            write(p, json.dumps({"stages": stages}))
            return driver.load_spec(p)

    def test_order_longest_first_respects_deps(self):
        sp = self.mk([{"name": "b", "cmd": ["x"], "deps": ["a"], "est_min": 99}, {"name": "a", "cmd": ["x"], "est_min": 1},
                      {"name": "c", "cmd": ["x"], "est_min": 50}])
        o = driver.order(sp["stages"])
        self.assertEqual(o.index("a") < o.index("b"), True)
        self.assertEqual(o[0], "c")

    def test_cycle_and_unknown_dep(self):
        with self.assertRaises(ValueError):
            self.mk([{"name": "a", "cmd": ["x"], "deps": ["b"]}, {"name": "b", "cmd": ["x"], "deps": ["a"]}])
        with self.assertRaises(ValueError):
            self.mk([{"name": "a", "cmd": ["x"], "deps": ["zz"]}])


class IoAudit(unittest.TestCase):
    def report(self, logs, stages=None, spec_extra=None):
        with tempfile.TemporaryDirectory() as d:
            run = os.path.join(d, "run")
            for st, lines in logs.items():
                write(os.path.join(run, "io", "%s.1.log" % st), "\n".join(lines) + "\n")
            stages = stages or [{"name": "build", "deps": [], "reads_outputs_of": []}, {"name": "a", "deps": ["build"], "reads_outputs_of": []},
                                {"name": "b", "deps": ["build"], "reads_outputs_of": []}]
            spec = dict(_dir=d, io_roots=["."], **(spec_extra or {}))
            root = d.replace("\\", "/")
            return driver.io_report(run, stages, spec), root

    def test_conflict_when_two_stages_write_one_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = d.replace("\\", "/")
        rep, root = self.report({"a": [], "b": []})
        rep, root = self.report({})
        # build real path logs
        with tempfile.TemporaryDirectory() as d:
            root = d.replace("\\", "/")
            run = os.path.join(d, "run")
            write(os.path.join(run, "io", "a.1.log"), "W %s/shared.txt\n" % root)
            write(os.path.join(run, "io", "b.1.log"), "W %s/shared.txt\nR %s/only_b.txt\n" % (root, root))
            stages = [{"name": "build", "deps": [], "reads_outputs_of": []}, {"name": "a", "deps": ["build"], "reads_outputs_of": []},
                      {"name": "b", "deps": ["build"], "reads_outputs_of": []}]
            rep = driver.io_report(run, stages, dict(_dir=d, io_roots=["."]))
            self.assertEqual(len(rep["conflicts"]), 1)
            self.assertTrue(rep["conflicts"][0][0].endswith("shared.txt"))

    def test_write_by_one_read_by_other_is_conflict_unless_dependency(self):
        with tempfile.TemporaryDirectory() as d:
            root = d.replace("\\", "/")
            run = os.path.join(d, "run")
            write(os.path.join(run, "io", "a.1.log"), "W %s/out.txt\n" % root)
            write(os.path.join(run, "io", "b.1.log"), "R %s/out.txt\n" % root)
            st = lambda ra: [{"name": "build", "deps": [], "reads_outputs_of": []}, {"name": "a", "deps": ["build"], "reads_outputs_of": []},
                             {"name": "b", "deps": ["build"], "reads_outputs_of": ra}]
            self.assertEqual(len(driver.io_report(run, st([]), dict(_dir=d, io_roots=["."]))["conflicts"]), 1)
            self.assertEqual(len(driver.io_report(run, st(["a"]), dict(_dir=d, io_roots=["."]))["conflicts"]), 0)

    def test_root_stage_exempt_and_caches_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            root = d.replace("\\", "/")
            run = os.path.join(d, "run")
            write(os.path.join(run, "io", "build.1.log"), "W %s/built.txt\n" % root)
            write(os.path.join(run, "io", "a.1.log"), "R %s/built.txt\nW %s/cache/x.json\n" % (root, root))
            write(os.path.join(run, "io", "b.1.log"), "W %s/cache/x.json\n" % root)
            stages = [{"name": "build", "deps": [], "reads_outputs_of": []}, {"name": "a", "deps": ["build"], "reads_outputs_of": []},
                      {"name": "b", "deps": ["build"], "reads_outputs_of": []}]
            rep = driver.io_report(run, stages, dict(_dir=d, io_roots=["."], io_ignore=["*/cache/*"]))
            self.assertEqual(rep["conflicts"], [])

    def test_stage_prefix_file_must_be_private(self):
        with tempfile.TemporaryDirectory() as d:
            root = d.replace("\\", "/")
            run = os.path.join(d, "run")
            write(os.path.join(run, "io", "a.1.log"), "W %s/ap_a_tmp.txt\n" % root)
            write(os.path.join(run, "io", "b.1.log"), "R %s/ap_a_tmp.txt\n" % root)
            stages = [{"name": "a", "deps": [], "reads_outputs_of": []}, {"name": "b", "deps": [], "reads_outputs_of": []}]
            rep = driver.io_report(run, stages, dict(_dir=d, io_roots=["."]))
            self.assertEqual(len(rep["conflicts"]), 1)


class CmpRuns(unittest.TestCase):
    def mkrun(self, d, name, subs, log, err=None):
        run = os.path.join(d, name)
        write(os.path.join(run, "results", "s1.json"), json.dumps({"stage": "s1", "subs": subs, "err": err, "cur": "", "secs": 3.0}))
        write(os.path.join(run, "logs", "s1.log"), log)
        return run

    def test_normalisation_only_removes_allowed_noise(self):
        with tempfile.TemporaryDirectory() as d:
            a = self.mkrun(d, "A", [["c", "PASS", ["took 12 s wall 3s at %s/x" % os.path.join(d, "A")]]], "done in 5s tmp_x_123_ab12\n")
            b = self.mkrun(d, "B", [["c", "PASS", ["took 99 s wall 7s at %s/x" % os.path.join(d, "B")]]], "done in 9s tmp_y_456_cd34\n")
            n, bad, lines = cmp_runs.compare(a, b, cmp_runs.make_norm(a, b))
            self.assertEqual(bad, 0, lines)

    def test_real_difference_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            a = self.mkrun(d, "A", [["c", "PASS", ["d0=7"]]], "x\n")
            b = self.mkrun(d, "B", [["c", "PASS", ["d0=8"]]], "x\n")
            n, bad, lines = cmp_runs.compare(a, b, cmp_runs.make_norm(a, b))
            self.assertEqual(bad, 1)
            self.assertTrue(any(l.startswith("RESULT DIFF") for l in lines))

    def test_missing_stage_and_log_and_verdict_flip(self):
        with tempfile.TemporaryDirectory() as d:
            a = self.mkrun(d, "A", [["c", "PASS", ["r"]]], "x\n")
            b = self.mkrun(d, "B", [["c", "FAIL", ["r"]]], "y\n")
            n, bad, lines = cmp_runs.compare(a, b, cmp_runs.make_norm(a, b))
            self.assertEqual(bad, 2)               # result verdict differs AND log differs

    def test_extra_norm(self):
        with tempfile.TemporaryDirectory() as d:
            a = self.mkrun(d, "A", [["c", "PASS", ["seed=111"]]], "x\n")
            b = self.mkrun(d, "B", [["c", "PASS", ["seed=222"]]], "x\n")
            self.assertEqual(cmp_runs.compare(a, b, cmp_runs.make_norm(a, b))[1], 1)
            self.assertEqual(cmp_runs.compare(a, b, cmp_runs.make_norm(a, b, [[r"seed=\d+", "seed=N"]]))[1], 0)

    def test_rs_check_flags_missing_native_and_ref_with_markers(self):
        with tempfile.TemporaryDirectory() as d:
            a = self.mkrun(d, "A", [["c", "PASS", ["r"]]], "x\n")
            b = self.mkrun(d, "B", [["c", "PASS", ["r"]]], "x\n")
            write(os.path.join(a, "rsstat", "ap_s1_rs_stat.1.txt"),
                  "RS_CORE_LOADED %s loaded x\n{'native': 0, 'py_steps': 5}\n" % ("0" * 16))
            bad, lines = cmp_runs.check_rs(a, b)
            self.assertGreaterEqual(bad, 1)
            self.assertTrue(any("native 0" in l for l in lines))
            write(os.path.join(b, "rsstat", "x.txt"), "x")
            bad2, lines2 = cmp_runs.check_rs(a, b)
            self.assertTrue(any("RUN B HAS rsstat" in l for l in lines2))

    def test_dual_check(self):
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "dual", "p.txt"), "argv\n{'segments': 4, 'mismatch': 0}\n")
            self.assertEqual(cmp_runs.check_dual(d)[0], 0)
            write(os.path.join(d, "dual", "q.txt"), "argv\n{'segments': 4, 'mismatch': 2}\n")
            self.assertEqual(cmp_runs.check_dual(d)[0], 1)
            self.assertEqual(cmp_runs.check_dual(os.path.join(d, "none"))[0], 1)


class StageContract(unittest.TestCase):
    def run_stage(self, body):
        with tempfile.TemporaryDirectory() as d:
            script = os.path.join(d, "st.py")
            write(script, "import sys\nsys.path.insert(0, %r)\nfrom ofh.lane import stage\n%s\n" % (ROOT, body))
            r = subprocess.run([sys.executable, script], env=dict(os.environ, OMH_RUN=d, OMH_STAGE="st"), capture_output=True, text=True)
            with open(os.path.join(d, "results", "st.json")) as f:
                return r.returncode, json.load(f)

    def test_pass(self):
        rc, res = self.run_stage("stage.main(lambda c: c.sub('x', True, ['row']))")
        self.assertEqual(rc, 0)
        self.assertEqual(res["subs"][0][1], "PASS")

    def test_no_subs_fails(self):
        rc, res = self.run_stage("stage.main(lambda c: None)")
        self.assertEqual(rc, 1)

    def test_exception_fails_with_traceback(self):
        rc, res = self.run_stage("def f(c):\n    c.sub('x', True)\n    raise RuntimeError('boom')\nstage.main(f)")
        self.assertEqual(rc, 1)
        self.assertIn("boom", res["err"])

    def test_neg_control_names(self):
        rc, res = self.run_stage("stage.main(lambda c: c.neg('mutant detected', True, ['by value']))")
        self.assertEqual(res["subs"][0][0], "NEG mutant detected")


class EndToEnd(unittest.TestCase):
    def drive(self, stages, extra=(), spec_extra=None):
        with tempfile.TemporaryDirectory() as d:
            spec = os.path.join(d, "spec.json")
            write(spec, json.dumps(dict({"stages": stages, "io_roots": ["."]}, **(spec_extra or {}))))
            for name, body in (("ok.py", "stage.main(lambda c: c.sub('fine', True, ['r']))"),
                               ("bad.py", "stage.main(lambda c: c.sub('broken', False, ['r']))"),
                               ("nothing.py", "stage.main(lambda c: None)"),
                               ("die.py", "import os\nos._exit(86)"),
                               ("w1.py", "open('shared.txt','w').write('1')\nstage.main(lambda c: c.sub('w', True, ['r']))"),
                               ("w2.py", "open('shared.txt','w').write('2')\nstage.main(lambda c: c.sub('w', True, ['r']))")):
                write(os.path.join(d, name), "import sys\nsys.path.insert(0, %r)\nfrom ofh.lane import stage\n%s\n" % (ROOT, body))
            run = os.path.join(d, "run")
            r = subprocess.run([sys.executable, "-m", "ofh.lane.driver", spec, "--run", run, "--pool", "3", "--settle", "0.2", "--poll", "0.2",
                                "--ram-gb", "0.1", "--ram-buf", "0.0"] + list(extra), cwd=ROOT, capture_output=True, text=True)
            with open(os.path.join(run, "summary.json")) as f:
                return r.returncode, json.load(f), r.stdout

    def st(self, name, script, deps=()):
        return {"name": name, "cmd": ["python", "-u", script], "deps": list(deps), "est_mb": 100, "est_min": 1}

    def test_all_pass(self):
        rc, s, out = self.drive([self.st("a", "ok.py"), self.st("b", "ok.py", ["a"])])
        self.assertEqual(rc, 0, out)
        self.assertTrue(s["allpass"])

    def test_failing_stage_fails_run(self):
        rc, s, _ = self.drive([self.st("a", "ok.py"), self.st("b", "bad.py")])
        self.assertEqual(rc, 1)
        self.assertFalse(s["allpass"])

    def test_stage_that_checks_nothing_fails(self):
        rc, s, _ = self.drive([self.st("n", "nothing.py")])
        self.assertEqual(rc, 1)

    def test_loud_exit_86_is_failure_not_skip(self):
        rc, s, out = self.drive([self.st("d", "die.py")])
        self.assertEqual(rc, 1)
        self.assertIn("LOUD", json.dumps(s["table"]))

    def test_dependency_failure_blocks_dependents(self):
        rc, s, _ = self.drive([self.st("a", "bad.py"), self.st("b", "ok.py", ["a"])])
        self.assertEqual(rc, 1)
        self.assertIn("NOT RUN", json.dumps(s["table"]))

    def test_shared_file_race_is_detected(self):
        rc, s, _ = self.drive([self.st("root", "ok.py"), self.st("a", "w1.py", ["root"]), self.st("b", "w2.py", ["root"])])
        self.assertEqual(rc, 1)
        io = [row for row in s["table"] if row[0].startswith("io")][0]
        self.assertEqual(io[1], "FAIL")

    def test_scratch_redirect_removes_the_race(self):
        rc, s, out = self.drive([self.st("root", "ok.py"), self.st("a", "w1.py", ["root"]), self.st("b", "w2.py", ["root"])],
                                spec_extra={"scratch": {"here": ".", "files": ["shared.txt"]}})
        self.assertEqual(rc, 0, out)

    def test_invalid_rust_value_refused(self):
        with tempfile.TemporaryDirectory() as d:
            spec = os.path.join(d, "spec.json")
            write(spec, json.dumps({"stages": [self.st("a", "ok.py")]}))
            r = subprocess.run([sys.executable, "-m", "ofh.lane.driver", spec, "--run", os.path.join(d, "r"), "--rust", "yes"], cwd=ROOT,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 86)


if __name__ == "__main__":
    unittest.main()
