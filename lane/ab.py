"""lane.ab: the Rust-vs-Python A/B proof in one command.

    python -m lane.ab SPEC.json --run-a RUN_RUST --run-b RUN_PY [--pool N] [lane.driver options...]

Runs the SAME battery twice - A with --rust 1, B with --rust 0 - then lane.cmp_runs A B --rs, and exits with its verdict.  Both runs must pass
on their own first: an A/B of two failing runs would "agree" on garbage.  Wall times are printed so the speed-up is a measurement, not a claim
(measure on an idle machine: concurrent load makes the comparison meaningless).
Exit codes: 0 = identical results and a valid Rust marker for every emulating stage; 1 = a difference or a failed run; 86 = harness refusal.
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(mod, args):
    return subprocess.run([sys.executable, "-m", mod] + args, cwd=ROOT).returncode


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--run-a" not in argv or "--run-b" not in argv:
        print(__doc__)
        return 2
    a = argv.pop(argv.index("--run-a") + 1)
    argv.remove("--run-a")
    b = argv.pop(argv.index("--run-b") + 1)
    argv.remove("--run-b")
    t = {}
    for tag, run, rust in (("A rust", a, "1"), ("B python", b, "0")):
        t0 = time.time()
        rc = _run("lane.driver", argv + ["--run", run, "--rust", rust])
        t[tag] = time.time() - t0
        print("== %s run: rc %d, wall %.0f s" % (tag, rc, t[tag]), flush=True)
        if rc != 0:
            return 86 if rc == 86 else 1
    print("== wall: rust %.0f s, python %.0f s (ratio %.2f)" % (t["A rust"], t["B python"], t["B python"] / max(t["A rust"], 1e-9)))
    return _run("lane.cmp_runs", [a, b, "--rs"])


if __name__ == "__main__":
    sys.exit(main())
