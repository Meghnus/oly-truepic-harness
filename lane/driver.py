"""lane.driver: run a verification battery as independent stages in separate processes, in parallel, safely.

    python -m lane.driver SPEC.json --run DIR [--pool 4] [--ram-gb 2.0] [--ram-buf 0.6] [--mem-scale 1.0]
                          [--only a,b] [--skip a,b] [--rust 0|1|dual] [--mem dict|page] [--fail-fast] [--list]

Why it exists: a serial emulation battery over a whole firmware takes hours, and the obvious fix (split it by hand and run the pieces side
by side) breaks in ways that look like firmware bugs: pieces overwrite each other's scratch files, one piece deletes a temp image another
still reads, memory runs out and the OS kills random processes.  The driver gives every stage:

  * its own process, one BLAS/OpenMP thread, OMH_ALIGN=1 (the alignment trap on), OMH_PREFIX=ap_<stage>_ (a name prefix for any temp file);
  * its own scratch dir OMH_SCRATCH, and with spec.scratch the SHARED output files of unmodified scripts are redirected there (lane.overlay,
    loaded into every python process of the stage through PYTHONPATH/sitecustomize) - the scripts are not edited;
  * an I/O audit (OMH_IOLOG): afterwards the driver PROVES from the logs that no file was written by one stage and touched by another
    (the "io" row of the table; caches whose names are content hashes belong in spec.io_ignore);
  * the RAM gate (lane.ram): a stage starts only if free memory stays above a floor and a buffer even after everything running has grown to
    its estimate;
  * the loud-failure rules of the Rust core (emu.rs_shim W1-W6): invalid --rust value, a missing / stale build, a stage that exits 86, or a
    stage that never produced its core-loaded marker are FAILURES, never skips.

Spec (JSON, relative paths are relative to the spec file; see examples/lane_spec.json):
  {"name": "...",
   "stages": [{"name": "s1", "cmd": ["python", "-u", "check.py", "{stage}"], "deps": [], "est_mb": 1500, "est_min": 5,
               "rust": true, "rust_off_reason": "", "env": {}, "reads_outputs_of": []}, ...],
   "scratch": {"here": ".", "files": ["plan_*.txt"], "dirs": []},
   "io_roots": ["."], "io_ignore": ["*/.cache/*"], "stage_wall_s": 10800, "watch_files": ["checks/*.py"]}
cmd placeholders: {stage} {run} {root} (this repository) {python}.

Outputs in the run dir: results/<stage>.json, logs/<stage>.log, io/, scr/<stage>/, rsstat/, summary.md, summary.json.
Exit code: 0 = every stage PASS and the io row PASS; 1 = anything else; 86 = the harness itself refused to start (preflight).
"""
import argparse
import fnmatch
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time

from lane import ram

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXIT_LOUD = 86


# ---------------------------------------------------------------------------------------------------------------- spec
def load_spec(path):
    path = os.path.abspath(path)
    with open(path, "r", encoding="utf-8") as f:
        spec = json.load(f)
    spec["_dir"] = os.path.dirname(path)
    names = [s["name"] for s in spec["stages"]]
    if len(set(names)) != len(names):
        raise ValueError("duplicate stage names in %s" % path)
    for s in spec["stages"]:
        s.setdefault("deps", [])
        s.setdefault("est_mb", 1500)
        s.setdefault("est_min", 5)
        s.setdefault("rust", True)
        s.setdefault("env", {})
        s.setdefault("reads_outputs_of", [])
        for d in s["deps"]:
            if d not in names:
                raise ValueError("stage %s depends on unknown stage %s" % (s["name"], d))
    order(spec["stages"])                              # raises on a cycle
    return spec


def order(stages):
    """names in a dependency-respecting order, longest estimate first among the ready ones (scheduling hint only)"""
    by = {s["name"]: s for s in stages}
    done, out, left = set(), [], list(by)
    while left:
        ready = sorted((n for n in left if all(d in done for d in by[n]["deps"])), key=lambda n: -by[n]["est_min"])
        if not ready:
            raise ValueError("dependency cycle among %s" % left)
        out.append(ready[0])
        done.add(ready[0])
        left.remove(ready[0])
    return out


def closure(stages, name):
    by = {s["name"]: s for s in stages}
    seen, todo = set(), list(by[name]["deps"])
    while todo:
        d = todo.pop()
        if d not in seen:
            seen.add(d)
            todo += by[d]["deps"]
    return seen


# ---------------------------------------------------------------------------------------------------------------- io audit
def io_report(run, stages, spec):
    """files opened by the stages (lane.overlay logs): which were written/removed by a stage and touched by another.
    stages = list of stage dicts; returns dict(files, shared_writes, conflicts)"""
    names = [s["name"] for s in stages]
    by = {s["name"]: s for s in stages}
    files = {}
    for f in glob.glob(os.path.join(run, "io", "*.log")):
        st = os.path.basename(f).split(".")[0]
        with open(f, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                k, _, p = ln.rstrip("\n").partition(" ")
                files.setdefault(p, {}).setdefault(st, set()).add(k)
    runp = os.path.abspath(run).replace("\\", "/")
    roots = [os.path.abspath(os.path.join(spec["_dir"], r)).replace("\\", "/") for r in spec.get("io_roots", ["."])]
    ignore = list(spec.get("io_ignore", [])) + ["*/__pycache__/*", "*/.omh_cache/*", "*.pyc"]
    uniq = re.compile(r"^(tmp\w+\.\w+|.*_\d+(_[0-9a-f]+)?\.bin)$")
    # a stage that every other stage depends on runs alone first: its writes cannot race
    exempt = {n for n in names if all(n in closure(stages, m) for m in names if m != n)}
    shared_w, conflicts = [], []
    for p, who in sorted(files.items()):
        base = os.path.basename(p)
        if p.startswith(runp + "/") or uniq.match(base) or not any(p == r or p.startswith(r + "/") for r in roots):
            continue
        if any(fnmatch.fnmatch(p, g) for g in ignore):
            continue
        owner = next((s for s in names if base.startswith("ap_%s_" % s)), None)   # a file named with a stage's prefix belongs to that stage alone
        if owner:
            if set(who) != {owner}:
                conflicts.append((p, sorted(who)))
            continue
        if os.path.isdir(p):
            continue
        writers = {s for s, m in who.items() if m & {"W", "D"}} - exempt
        if writers:
            shared_w.append((p, {s: "".join(sorted(m)) for s, m in who.items()}))
            others = {s for s in who if s not in writers
                      and not (who[s] <= {"R"} and any(w in closure(stages, s) or w in by[s]["reads_outputs_of"] for w in writers))}
            if len(writers) > 1 or others:
                conflicts.append((p, sorted(who)))
    return dict(files=len(files), shared_writes=shared_w, conflicts=conflicts)


# ---------------------------------------------------------------------------------------------------------------- summary
def load_res(run, n):
    p = os.path.join(run, "results", n + ".json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def file_shas(spec):
    out = {}
    for g in spec.get("watch_files", []):
        for f in sorted(glob.glob(os.path.join(spec["_dir"], g))):
            with open(f, "rb") as fh:
                out[os.path.relpath(f, spec["_dir"]).replace("\\", "/")] = hashlib.sha1(fh.read()).hexdigest()
    return out


def stage_verdict(r):
    """PASS only with no error, at least one sub, and every sub PASS"""
    return bool(r) and not r.get("err") and bool(r.get("subs")) and all(v == "PASS" for _, v, _ in r["subs"])


def summarize(run, spec, names, done, peak, secs, drift_before):
    table = []                      # (label, verdict, secs, peak, key)
    for n in names:
        rc, dt = done.get(n, (None, 0))
        r = load_res(run, n)
        if r is None:
            why = "no result file (rc %s)" % rc
            if rc == EXIT_LOUD:
                why = "LOUD FAILURE of the harness (exit 86): see logs/%s.log" % n
            table.append((n, "NOT RUN" if rc is None else "FAIL", dt, peak.get(n, 0), why))
            continue
        for name, v, rows in r["subs"]:
            table.append((name if name != n else n, v, r["secs"], peak.get(n, 0), rows[0][:150] if rows else ""))
        if not r["subs"] and not r["err"]:
            table.append((n, "FAIL", r["secs"], peak.get(n, 0), "the stage checked nothing (no sub results)"))
        if r["err"]:
            table.append((n if not r["subs"] else n + ":" + r["cur"], "FAIL", r["secs"], peak.get(n, 0), r["err"].strip().splitlines()[-1][:230]))
        if rc not in (0, None) and not r["err"] and all(v == "PASS" for _, v, _ in r["subs"]):
            table.append((n + ":exit", "FAIL", r["secs"], peak.get(n, 0), "all subs PASS but the process exited %s" % rc))
    stages = [s for s in spec["stages"] if s["name"] in names]
    io = io_report(run, stages, spec)
    table.append(("io (no shared writes)", "PASS" if not io["conflicts"] else "FAIL", 0, 0,
                  "%d files opened by the stages; per-stage output files written under the io roots %d (listed below); cross-stage conflicts %d"
                  % (io["files"], len(io["shared_writes"]), len(io["conflicts"]))))
    drift = sorted(k for k, v in file_shas(spec).items() if drift_before.get(k) != v)
    allpass = all(v == "PASS" for _, v, _, _, _ in table)
    cpu = sum(load_res(run, n)["secs"] for n in names if load_res(run, n))
    lines = ["# lane %s" % spec.get("name", ""), "",
             "wall %.0f s (%.1f min) this invocation; sum of stage seconds %.0f s (%.1f min); %d stage processes; verdict **%s**"
             % (secs, secs / 60, cpu, cpu / 60, len(names), "ALL PASS" if allpass else "FAIL"),
             "watched files changed while this run dir was being produced: %s" % (", ".join(drift) or "none"), "",
             "| stage | verdict | s | peak MB | key |", "|---|---|---|---|---|"]
    for n, v, dt, pk, key in table:
        lines.append("| %s | %s | %.0f | %.0f | %s |" % (n, v, dt, pk, key.replace("|", "/")))
    if io["shared_writes"] or io["conflicts"]:
        lines += ["", "shared writes: %s" % json.dumps(io["shared_writes"], indent=0)[:3000], "conflicts: %s" % json.dumps(io["conflicts"])[:3000]]
    with open(os.path.join(run, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(run, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(dict(name=spec.get("name"), secs=secs, allpass=allpass, table=table, io=io, drift=drift), f, indent=1)
    print("\n".join(lines), flush=True)
    return 0 if allpass else 1


# ---------------------------------------------------------------------------------------------------------------- driver
def build_env(a, spec, run):
    env = dict(os.environ)
    pp = [ROOT, os.path.join(ROOT, "lane", "site")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    env.update(PYTHONPATH=os.pathsep.join(pp), PYTHONIOENCODING="utf-8", OMH_RUN=run, OMH_ROOT=ROOT, OMH_ALIGN="0" if a.no_align else "1",
               OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1", NUMEXPR_NUM_THREADS="1",
               OMH_IOLOG=os.path.join(run, "io"), OMH_RUST=a.rust, OMH_MEM=a.mem)
    sc = spec.get("scratch")
    if sc:
        env["OMH_SCR_SPEC"] = json.dumps({"here": os.path.abspath(os.path.join(spec["_dir"], sc.get("here", "."))),
                                          "files": sc.get("files", []), "dirs": sc.get("dirs", [])})
    if spec.get("profile"):
        env["OMH_PROFILE"] = os.path.abspath(os.path.join(spec["_dir"], spec["profile"]))
    if a.rust != "0":
        env["OMH_RS_STAT"] = "1"
        env["OMH_RS_STAT_DIR"] = os.path.join(run, "rsstat")
        os.makedirs(env["OMH_RS_STAT_DIR"], exist_ok=True)
        if a.rust == "dual":
            env["OMH_DUAL_DIR"] = os.path.join(run, "dual")
    else:
        env.pop("OMH_RS_STAT", None)
        env.pop("OMH_RS_STAT_DIR", None)
    return env


def preflight(env, spec):
    """S3: one child with the stage environment imports the Rust shim; a missing / stale build stops the run here, once"""
    r = subprocess.run([sys.executable, "-u", "-c", "import emu.rs_shim as s; print(s._SRC_SHA[:16])"], cwd=spec["_dir"],
                       env=dict(env, OMH_RS_STAT="0"), capture_output=True, text=True)
    if r.returncode != 0:
        print("OMH_RUST preflight FAILED (rc %d):\n%s\n%s\nstopping: rebuild with `python -m emu.build` or run with --rust 0"
              % (r.returncode, r.stdout[-1500:], r.stderr[-3000:]), flush=True)
        return False
    print("rust preflight OK (SRC_SHA %s)" % r.stdout.strip(), flush=True)
    return True


def driver(a):
    spec = load_spec(a.spec)
    if a.rust not in ("0", "1", "dual"):
        print("lane.driver: --rust must be 0, 1 or dual (got %r)" % a.rust, file=sys.stderr)
        return EXIT_LOUD
    run = os.path.abspath(a.run)
    for d in ("results", "logs", "io", "scr"):
        os.makedirs(os.path.join(run, d), exist_ok=True)
    stages = {s["name"]: s for s in spec["stages"]}
    names = [n for n in order(spec["stages"])]
    if a.only:
        want = set(a.only.split(","))
        unknown = want - set(names)
        if unknown:
            print("lane.driver: unknown stage(s) in --only: %s" % sorted(unknown), file=sys.stderr)
            return 1
        names = [n for n in names if n in want]
    if a.skip:
        names = [n for n in names if n not in set(a.skip.split(","))]
    if a.list:
        for n in names:
            s = stages[n]
            print("%-16s deps %-20s est %3d min %5d MB%s" % (n, ",".join(s["deps"]) or "-", s["est_min"], s["est_mb"], "" if s["rust"] else "  rust OFF"))
        return 0
    deps = {n: [d for d in stages[n]["deps"] if d in names] for n in names}       # --only drops deps that are not run (their results must exist)
    env0 = build_env(a, spec, run)
    if a.rust != "0" and any(stages[n]["rust"] for n in names) and not preflight(env0, spec):
        return EXIT_LOUD
    drift_before = file_shas(spec)
    with open(os.path.join(run, "watched_%s.json" % time.strftime("%H%M%S")), "w") as f:
        json.dump(drift_before, f, indent=0)
    pool = max(1, a.pool)
    t_start = time.time()
    running, done, peak, started, cur = {}, {}, {}, {}, {}
    pending = list(names)
    est_mb = {n: stages[n]["est_mb"] for n in names}
    wall = int(spec.get("stage_wall_s", 10800))
    print("run dir %s; %d stages; pool %d; rust %s; mem %s" % (run, len(names), pool, a.rust, a.mem), flush=True)

    def launch(n):
        s = stages[n]
        scr = os.path.join(run, "scr", n)
        res = os.path.join(run, "results", n + ".json")
        if os.path.exists(res):
            os.remove(res)                                   # never report a stale result of an earlier invocation
        os.makedirs(scr, exist_ok=True)
        for f in glob.glob(os.path.join(run, "rsstat", "ap_%s_rs_stat.*.txt" % n)):
            os.remove(f)                                     # a reused run dir must not show an earlier invocation's markers
        env = dict(env0, OMH_SCRATCH=scr, OMH_STAGE=n, OMH_PREFIX="ap_%s_" % n)
        env.update({k: str(v) for k, v in s["env"].items()})
        if a.rust != "0" and not s["rust"]:
            env["OMH_RUST"] = "0"                            # W6: this stage runs pure Python by rule; recorded so cmp_runs --rs expects it
            env.pop("OMH_RS_STAT", None)
            env.pop("OMH_RS_STAT_DIR", None)
            offf = os.path.join(run, "rs_off.json")
            off = json.load(open(offf)) if os.path.exists(offf) else {}
            off[n] = s.get("rust_off_reason") or "spec: rust=false"
            json.dump(off, open(offf, "w"), indent=1)
        cmd = [c.format(stage=n, run=run, root=ROOT, python=sys.executable) for c in s["cmd"]]
        if cmd[0] in ("python", "python3"):
            cmd[0] = sys.executable
        log = open(os.path.join(run, "logs", n + ".log"), "w", encoding="utf-8")
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, cwd=spec["_dir"])
        running[n] = (p, log)
        started[n] = time.time()
        peak[n] = 0
        print("[%6.0fs] start %-14s pid %d (running %d, free RAM %.1f GB)" % (time.time() - t_start, n, p.pid, len(running), ram.free_gb()), flush=True)

    failed_fast = False
    while pending or running:
        for n, (p, log) in list(running.items()):
            cur[n] = ram.tree_rss(p)
            peak[n] = max(peak[n], cur[n])
            if p.poll() is not None:
                log.close()
                done[n] = (p.returncode, time.time() - started[n])
                del running[n]
                print("[%6.0fs] end   %-14s rc %s %.0fs peak %.0f MB" % (time.time() - t_start, n, p.returncode, done[n][1], peak[n]), flush=True)
        bad = [n for n, d in done.items() if d[0] not in (0, None)]
        if (a.fail_fast and bad) or any(d in done and done[d][0] != 0 for n in pending for d in deps[n]):
            for n in list(pending):
                if a.fail_fast and bad or any(d in done and done[d][0] != 0 for d in deps[n]):
                    pending.remove(n)
                    done[n] = (None, 0)                      # not run: a dependency failed (or --fail-fast); stages in flight finish
                    failed_fast = True
            continue
        ready = [n for n in pending if all(d in done for d in deps[n])]
        for n in list(ready):
            if len(running) >= pool:
                break
            est = est_mb[n] * a.mem_scale / 1024.0
            growth = ram.growth_gb(running, est_mb, cur, a.mem_scale)
            if not ram.gate_ok(ram.free_gb(), growth, est, a.ram_gb, a.ram_buf):
                continue
            pending.remove(n)
            launch(n)
            time.sleep(a.settle)                             # let the new process claim its memory before the next RAM check
        time.sleep(a.poll)
        for n, (p, log) in list(running.items()):
            if time.time() - started[n] > wall:
                p.kill()
                print("[%6.0fs] KILL  %-14s (wall > %ds)" % (time.time() - t_start, n, wall), flush=True)
    secs = time.time() - t_start
    shown = [n for n in order(spec["stages"]) if n in names or os.path.exists(os.path.join(run, "results", n + ".json"))]
    return summarize(run, spec, shown, done, peak, secs, drift_before)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("spec")
    ap.add_argument("--run", required=True)
    ap.add_argument("--pool", type=int, default=4)
    ap.add_argument("--ram-gb", type=float, default=2.0, help="never start a stage with less free RAM than this (GB)")
    ap.add_argument("--ram-buf", type=float, default=0.6, help="keep this much free after all running stages reached their estimates (GB)")
    ap.add_argument("--mem-scale", type=float, default=1.0, help="multiply every est_mb (calibrate once from the peak MB column)")
    ap.add_argument("--only")
    ap.add_argument("--skip")
    ap.add_argument("--rust", default=os.environ.get("OMH_RUST") or "0", help="0 | 1 | dual (default $OMH_RUST or 0); anything else is refused")
    ap.add_argument("--mem", default=os.environ.get("OMH_MEM") or "dict", choices=("dict", "page"))
    ap.add_argument("--fail-fast", action="store_true")
    ap.add_argument("--no-align", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--settle", type=float, default=8.0)
    return driver(ap.parse_args(argv))


if __name__ == "__main__":
    sys.path.insert(0, ROOT)
    sys.exit(main())
