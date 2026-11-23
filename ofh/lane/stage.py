"""ofh.lane.stage: the contract between ofh.lane.driver and a stage, as a tiny API for stage authors.

A stage is any command.  It reports through ONE file, <run>/results/<stage>.json:

    {"stage": name, "subs": [[sub_name, "PASS"|"FAIL", [row, ...]], ...], "err": null | traceback text, "cur": current sub, "secs": float, "pid": int}

A stage PASSES only if err is null AND it has at least one sub AND every sub is PASS (a stage that checks nothing must not pass).  Rows are
short strings; the first row of a sub is its "key" in the summary table.  Rows are compared between runs by ofh.lane.cmp_runs, so keep them
deterministic (no timestamps, no absolute paths; the normaliser removes wall-clock numbers and run-dir tokens, nothing else).

Python stages:

    from ofh.lane import stage

    def my_stage(ctx):
        cpu = ...                      # emulate something
        ctx.sub("hook returns 7", cpu.d[0] == 7, ["d0=%d" % cpu.d[0]])
        ctx.neg("hook fails on mutant", mutant_cpu.d[0] != 7, ["mutant d0=%d" % mutant_cpu.d[0]])    # negative control: see docs/method.md

    if __name__ == "__main__":
        stage.main(my_stage)           # reads OMH_RUN / OMH_STAGE from the environment the driver set

The driver's environment for every stage: OMH_RUN, OMH_STAGE, OMH_SCRATCH (private dir), OMH_PREFIX (ap_<stage>_: put it in front of any temp
file name you create outside OMH_SCRATCH), OMH_ALIGN=1, one BLAS / OpenMP thread, OMH_RUST / OMH_SHM / OMH_RS_STAT* as the driver was told.
Exit codes: 0 = PASS, 1 = FAIL, 86 = loud failure of the harness itself (always FAIL).
"""
import json
import os
import sys
import time
import traceback


class Ctx:
    def __init__(self, stage, run):
        self.stage, self.run = stage, run
        self.subs = []
        self.cur = ""
        self.scratch = os.environ.get("OMH_SCRATCH") or os.getcwd()

    def sub(self, name, ok, rows=()):
        """record one named check; ok is a bool (or 'PASS'/'FAIL')"""
        verdict = ok if isinstance(ok, str) else ("PASS" if ok else "FAIL")
        if verdict not in ("PASS", "FAIL"):
            raise ValueError("verdict must be PASS or FAIL")
        self.subs.append((name, verdict, [str(r) for r in rows]))
        self.cur = name
        return verdict == "PASS"

    def neg(self, name, mutant_detected, rows=()):
        """record a NEGATIVE CONTROL: the check was run on a deliberately broken input and must have FAILED there.  `mutant_detected` is True
        when the check flagged the mutant.  The sub is named 'NEG <name>' so a run table shows which controls ran."""
        return self.sub("NEG " + name, bool(mutant_detected), rows)


def main(fn, stage=None, run=None):
    stage = stage or os.environ.get("OMH_STAGE") or (sys.argv[1] if len(sys.argv) > 1 else "stage")
    run = run or os.environ.get("OMH_RUN") or os.getcwd()
    ctx = Ctx(stage, run)
    t0 = time.time()
    err = None
    try:
        fn(ctx)
    except BaseException:
        err = traceback.format_exc()[-6000:]
    res = dict(stage=stage, subs=ctx.subs, err=err, cur=ctx.cur, secs=round(time.time() - t0, 1), pid=os.getpid())
    os.makedirs(os.path.join(run, "results"), exist_ok=True)
    with open(os.path.join(run, "results", stage + ".json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1)
    ok = not err and bool(ctx.subs) and all(v == "PASS" for _, v, _ in ctx.subs)
    print("STAGE %s %s %.0fs" % (stage, "PASS" if ok else "FAIL", res["secs"]), flush=True)
    if err:
        print(err, flush=True)
    sys.exit(0 if ok else 1)
