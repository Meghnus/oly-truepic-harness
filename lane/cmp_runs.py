"""lane.cmp_runs: diff two lane run dirs stage by stage, normalising ONLY what is allowed to differ.

    python -m lane.cmp_runs RUN_A RUN_B [--rs] [--dual] [--norm EXTRA.json] [--show-norm]

Compares every stage result (verdict + rows of every sub) and every stage log (logs/*.log) of A against B.  The point of the tool is the
A/B proof "the Rust core changes nothing": run the same battery with --rust 1 (A) and --rust 0 (B) and require DIFFS 0.

Token normalisation (the whole list is printed with --show-norm; nothing else is touched, so a real difference cannot hide in it):
  * the two run dirs' own absolute paths (either slash style) -> <RUN>
  * wall-clock numbers: 'wall 12s', '(12s, exit', '12 s', '1.5s', 'in 12s'
  * temp-file names: tmp_<tag>_<pid>_<hex>, tmpXXXXXXXX.bin
  * anything you add with --norm EXTRA.json = [[regex, replacement], ...]  (e.g. a per-run scratch-path token your stages print)

--rs   A ran with --rust 1 (or dual): every stage of A must carry the Rust core's marker (rsstat/ap_<stage>_rs_stat.<pid>.txt: first line
       RS_CORE_LOADED <src_sha16> ...; the second line is the engine counters).  Checked per stage: processes with the core, native / python-routed
       step counts, the marker's SRC_SHA equals emu/_rs_emu.build.json (a stale build cannot pass), stages the driver ran pure-Python by rule
       (rs_off.json) are listed, a stage that emulated with native == 0 is a DIFF (every step went to the Python core: the "Rust run" proved
       nothing), and B must NOT have an rsstat/ dir (B is the reference).
--dual A ran with --rust dual: dual/*.txt counters must show segments > 0 and mismatch == 0.
Exit code 1 on any DIFF.
"""
import ast
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE_NORM = [
    (r"\(\d+s, exit", "(Ns, exit"),
    (r"wall \d+s", "wall Ns"),
    (r"\b\d+ s\]", "N s]"),
    (r"\b\d+ s\b", "N s"),
    (r"\d+\.\d+ ?s\b", "N.Ns"),
    (r"\bin \d+s\b", "in Ns"),
    (r"tmp_\w+?_\d+_[0-9a-f]+", "tmp_X"),
    (r"tmp[a-z0-9_]{8}\.bin", "tmpX.bin"),
]


def make_norm(run_a, run_b, extra=()):
    pats = []
    for d in (run_a, run_b):
        ap = os.path.abspath(d)
        for form in {ap, ap.replace("\\", "/"), ap.replace("/", "\\")}:
            pats.append((re.compile(re.escape(form)), "<RUN>"))
    pats += [(re.compile(p), r) for p, r in BASE_NORM] + [(re.compile(p), r) for p, r in extra]

    def norm(t):
        for rx, rep in pats:
            t = rx.sub(rep, t)
        return t
    norm.patterns = [(rx.pattern, rep) for rx, rep in pats]
    return norm


def compare(a, b, norm):
    """returns (stages_compared, diffs, report lines)"""
    out = []
    bad = n = 0
    for f in sorted(glob.glob(os.path.join(a, "results", "*.json"))):
        g = os.path.join(b, "results", os.path.basename(f))
        if not os.path.exists(g):
            out.append("MISSING in B: %s" % os.path.basename(f))
            bad += 1
            continue
        with open(f, encoding="utf-8") as fa, open(g, encoding="utf-8") as fb:
            ra, rb = json.load(fa), json.load(fb)
        sa = [(n_, v, [norm(x) for x in rows]) for n_, v, rows in ra["subs"]]
        sb = [(n_, v, [norm(x) for x in rows]) for n_, v, rows in rb["subs"]]
        n += 1
        if sa != sb or bool(ra["err"]) != bool(rb["err"]) or (ra["err"] and norm(ra["err"]) != norm(rb["err"])):
            out.append("RESULT DIFF %s" % os.path.basename(f))
            bad += 1
            if len(sa) != len(sb):
                out.append("  sub count A %d B %d" % (len(sa), len(sb)))
            for x, y in zip(sa, sb):
                if x != y:
                    out.append("  A %s %s %s" % (x[0], x[1], x[2][:1]))
                    out.append("  B %s %s %s" % (y[0], y[1], y[2][:1]))
        else:
            out.append("same result %s %s" % (os.path.basename(f), [v for _, v, _ in sa]))
    for f in sorted(glob.glob(os.path.join(a, "logs", "*.log"))):
        g = os.path.join(b, "logs", os.path.basename(f))
        if not os.path.exists(g):
            out.append("MISSING log in B: %s" % os.path.basename(f))
            bad += 1
            continue
        with open(f, encoding="utf-8", errors="replace") as fa, open(g, encoding="utf-8", errors="replace") as fb:
            la, lb = norm(fa.read()).splitlines(), norm(fb.read()).splitlines()
        if la != lb:
            d = [(i, x, y) for i, (x, y) in enumerate(zip(la, lb)) if x != y]
            out.append("LOG DIFF %s lines %d %d first diffs %s" % (os.path.basename(f), len(la), len(lb), d[:3]))
            bad += 1
        else:
            out.append("same log (%d lines) %s" % (len(la), os.path.basename(f)[:70]))
    return n, bad, out


def check_rs(a, b):
    """the Rust-core marker checks of --rs; returns (diffs, lines)"""
    out, bad = [], 0
    try:
        with open(os.path.join(ROOT, "emu", "_rs_emu.build.json"), encoding="utf-8") as f:
            want = json.load(f)["src_sha"][:16]
    except Exception as e:
        want = None
        out.append("NO BUILD RECORD (emu/_rs_emu.build.json unreadable: %r): the marker SRC_SHA cannot be checked" % (e,))
        bad += 1
    offf = os.path.join(a, "rs_off.json")
    off = json.load(open(offf)) if os.path.exists(offf) else {}
    if glob.glob(os.path.join(b, "rsstat", "*")):
        out.append("RUN B HAS rsstat/ (B must be the pure-Python reference run): %s" % os.path.join(b, "rsstat"))
        bad += 1
    for f in sorted(glob.glob(os.path.join(a, "results", "*.json"))):
        st = os.path.basename(f)[:-5]
        if st in off:
            out.append("rs marker %-24s rust OFF by rule: %s" % (st, off[st]))
            continue
        tot, loaded, hooked = {}, 0, 0
        for g in glob.glob(os.path.join(a, "rsstat", "ap_%s_rs_stat.*.txt" % st)):
            with open(g, encoding="utf-8", errors="replace") as fh:
                ls = fh.read().splitlines()
            if ls and ls[0].startswith("RS_CORE_"):
                hooked += 1
                sha = ls[0].split()[1] if len(ls[0].split()) > 1 else "?"
                if want and sha != want:
                    out.append("MARKER SRC_SHA %s != build %s: %s" % (sha, want, os.path.basename(g)))
                    bad += 1
            if ls and ls[0].startswith("RS_CORE_LOADED"):
                loaded += 1
                if len(ls) > 1:
                    for k, v in ast.literal_eval(ls[1]).items():
                        tot[k] = tot.get(k, 0) + v
        emu = tot.get("native", 0) + tot.get("py_entry", 0) + tot.get("py_steps", 0) + tot.get("fb_route", 0)
        tag = "native" if tot.get("native", 0) else ("PYTHON-ROUTED (no native step)" if emu else "no emulation")
        out.append("rs marker %-24s procs %3d/%3d (core loaded / marker)  native %10d  py_entry %9d  fb_route %8d  py_steps %8d  %s" % (
            st, loaded, hooked, tot.get("native", 0), tot.get("py_entry", 0), tot.get("fb_route", 0), tot.get("py_steps", 0), tag))
        if emu and not tot.get("native", 0):
            out.append("EMULATING STAGE WITH native 0 (every step went to the Python core; mark it rust:false in the spec or fix the routing): %s" % st)
            bad += 1
    return bad, out


def check_dual(a):
    out, bad = [], 0
    files = glob.glob(os.path.join(a, "dual", "*.txt"))
    if not files:
        return 1, ["NO dual counters in %s (was the run started with --rust dual?)" % os.path.join(a, "dual")]
    seg = mis = 0
    for f in files:
        with open(f, encoding="utf-8", errors="replace") as fh:
            ls = fh.read().splitlines()
        try:
            d = ast.literal_eval(ls[1])
        except Exception:
            out.append("unreadable dual counters %s" % os.path.basename(f))
            bad += 1
            continue
        seg += d.get("segments", 0)
        mis += d.get("mismatch", 0)
        if d.get("mismatch", 0):
            out.append("DUAL MISMATCH in %s: %d" % (os.path.basename(f), d["mismatch"]))
            bad += 1
    out.append("dual: %d processes, %d segments verified, %d mismatches" % (len(files), seg, mis))
    if seg == 0:
        out.append("dual verified NOTHING (0 segments)")
        bad += 1
    return bad, out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = [x for x in argv if not x.startswith("--")]
    extra = []
    if "--norm" in argv:
        nf = argv[argv.index("--norm") + 1]
        args = [x for x in args if x != nf]
        with open(nf, encoding="utf-8") as f:
            extra = json.load(f)
    a, b = args[:2]
    norm = make_norm(a, b, extra)
    if "--show-norm" in argv:
        for p, r in norm.patterns:
            print("norm  %-60s -> %s" % (p, r))
    n, bad, lines = compare(a, b, norm)
    print("\n".join(lines))
    if "--rs" in argv:
        d, ls = check_rs(a, b)
        bad += d
        print("\n".join(ls))
    if "--dual" in argv:
        d, ls = check_dual(a)
        bad += d
        print("\n".join(ls))
    print("STAGES %d  DIFFS %d" % (n, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
