# lane: run a verification battery in parallel, safely

`python -m ofh.lane.driver SPEC.json --run DIR [--pool 4] [--ram-gb 2] [--ram-buf 0.6] [--mem-scale 1] [--rust 0|1|dual] [--only a,b] [--skip a,b]`

A battery is a list of **stages** (any command) with dependencies. The driver gives every stage its own process, one BLAS/OpenMP thread,
`OMH_ALIGN=1`, a private scratch directory, a temp-file prefix, and an I/O audit; it starts stages only when the **RAM gate** allows it.

## Spec (JSON)

```json
{"name": "my battery",
 "stages": [
   {"name": "build", "cmd": ["python", "-u", "stages/build.py"], "est_mb": 2500, "est_min": 8},
   {"name": "hook1", "cmd": ["python", "-u", "stages/hook1.py"], "deps": ["build"], "est_mb": 1500, "est_min": 5},
   {"name": "legacy", "cmd": ["python", "-u", "old/check.py", "{stage}"], "deps": ["build"], "rust": false,
    "rust_off_reason": "uses an operand form the core routes to Python (documented)"}],
 "scratch": {"here": ".", "files": ["plan_*.txt"], "dirs": ["out"]},
 "io_roots": ["."], "io_ignore": ["*/.cache/*"], "stage_wall_s": 10800, "watch_files": ["stages/*.py"],
 "profile": "my-body.json"}
```

* `cmd` placeholders `{stage}` `{run}` `{root}` `{python}`; the working directory is the spec's directory.
* `est_mb` drives the RAM gate; `est_min` only orders the queue (longest ready stage first).
* `scratch`: shared **output** files that scripts you did not write rewrite next to themselves are redirected into the stage's scratch dir
  (write-mode opens go there, read-mode opens see the private copy once it exists). The scripts are not edited.
* `io_ignore`: content-addressed caches (any stage may fill them) are not conflicts.
* `watch_files`: hashed at start and at the end; a change while the run was going is reported (editing a check mid-run invalidates the run).

## Stage contract (`ofh/lane/stage.py`)

A stage writes `results/<stage>.json` = subs of `(name, PASS|FAIL, [rows])`. It passes only with no error, **at least one sub**, and all subs
PASS. Use `ctx.sub(...)` and `ctx.neg(...)` (negative control). Exit 0/1; 86 is the harness refusing to run (always FAIL).

## RAM gate (`ofh/lane/ram.py`)

Start the next stage only if `free >= --ram-gb` **and** `free - growth - estimate >= --ram-buf`, where `growth` is how much every running
stage still has to grow to reach its own estimate. A stage that does not fit is skipped for now; a smaller one may start. `--mem-scale`
rescales all estimates after you calibrated them from the **peak MB** column. `psutil` is optional (Windows/Linux fallbacks exist).

## I/O audit

`ofh/lane/overlay.py` is loaded into every Python process of a stage through `PYTHONPATH` + `sitecustomize` and logs each distinct file opened
(R/W/D). The summary's `io` row is FAIL when a file was written by one stage and touched by another, except: files under the run dir, caches
in `io_ignore`, files named with a stage's own prefix (`ap_<stage>_...`, which must be touched by that stage alone), a stage that every other
stage depends on (it runs alone first), and declared reads (`reads_outputs_of`).

## Comparing runs (`ofh/lane/cmp_runs.py`) and the A/B

`python -m ofh.lane.cmp_runs A B [--rs] [--dual] [--norm EXTRA.json] [--show-norm]`: every result and every log, token-normalised (wall-clock
numbers, temp names, the two run dirs' own paths, plus what you add). `--rs`: proves the Rust core ran in A (marker per stage, source hash
equals the build record, `native > 0` for emulating stages, B must have no markers). `--dual`: dual counters show verified segments and 0
mismatches. `python -m ofh.lane.ab SPEC --run-a A --run-b B` runs the battery with `--rust 1` and `--rust 0`, prints both wall times and compares.
Both runs must pass first.

Outputs of a run dir: `results/` `logs/` `io/` `scr/<stage>/` `rsstat/` `dual/` `rs_off.json` `summary.md` `summary.json`.
