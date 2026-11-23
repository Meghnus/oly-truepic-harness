"""ofh.lane.overlay: per-stage scratch redirection + I/O log, loaded into EVERY python process of a stage (the stage itself and the scripts it runs
as subprocesses) by ofh/lane/site/sitecustomize.py when OMH_SCRATCH is set.  Environment:

  OMH_SCRATCH   stage-private scratch directory.
  OMH_SCR_SPEC  JSON {"here": dir, "files": [glob, ...], "dirs": [name, ...]}: the SHARED OUTPUT files that unmodified legacy scripts rewrite
                next to themselves (patch lists, plan texts, logs).  Write-mode opens of those go to OMH_SCRATCH (copy-on-first-append for
                'a' / '+' modes); read-mode opens see the stage's private copy once it exists and the original otherwise.  The original
                files are therefore NEVER written while stages run in parallel, and the scripts are not edited at all.
  OMH_IOLOG     directory; each process appends one line "<mode> <abs path>" (R / W / D) per distinct file it opens / removes / renames (audit
                hook, first occurrence only) to <OMH_IOLOG>/<OMH_STAGE>.<pid>.log; ofh.lane.driver turns the logs into the "io" row: no file is
                written by one stage and touched by another.

Nothing here changes what any check computes.
"""
import builtins
import fnmatch
import json
import os
import shutil
import sys

SC = os.environ.get("OMH_SCRATCH")
_spec = json.loads(os.environ.get("OMH_SCR_SPEC") or "{}")
HERE = os.path.normcase(os.path.abspath(_spec.get("here") or os.getcwd()))
SHARED_FILES = tuple(_spec.get("files", ()))
SHARED_DIRS = tuple(_spec.get("dirs", ()))

_open = builtins.open
_makedirs = os.makedirs


def _rel(p):
    """(relative name, abs path) if p is a shared file directly inside HERE or inside one of SHARED_DIRS, else None"""
    try:
        ap = os.path.normcase(os.path.abspath(os.fspath(p)))
    except TypeError:
        return None
    if os.path.dirname(ap) == HERE and any(fnmatch.fnmatch(os.path.basename(ap), g) for g in SHARED_FILES):
        return os.path.basename(ap), ap
    for d in SHARED_DIRS:
        pre = os.path.join(HERE, d)
        if ap == pre or ap.startswith(pre + os.sep):
            return os.path.relpath(ap, HERE), ap
    return None


def redirect(p, write):
    r = _rel(p)
    if r is None or not SC:
        return p
    rel, ap = r
    t = os.path.join(SC, rel)
    if write:
        _makedirs(os.path.dirname(t), exist_ok=True)
        return t
    return t if os.path.exists(t) else p


def _open_ov(file, mode="r", *a, **k):
    if isinstance(file, int):
        return _open(file, mode, *a, **k)
    m = mode or "r"
    w = any(c in m for c in "wax+")
    t = redirect(file, w)
    if w and t != file and ("a" in m or "+" in m) and "w" not in m and not os.path.exists(t) and os.path.exists(file):
        shutil.copyfile(file, t)          # append / update: start from the original content
    return _open(t, mode, *a, **k)


def _makedirs_ov(name, *a, **k):
    return _makedirs(redirect(name, True), *a, **k)


def install():
    if SC and (SHARED_FILES or SHARED_DIRS):
        builtins.open = _open_ov
        import io
        io.open = _open_ov
        os.makedirs = _makedirs_ov
    if os.environ.get("OMH_IOLOG"):
        seen = set()
        log = None
        stage, pid = os.environ.get("OMH_STAGE", "x"), os.getpid()

        def note(kind, p):
            nonlocal log
            try:
                ap = os.path.abspath(os.fsdecode(p)).replace("\\", "/")
            except Exception:
                return
            k = (kind, ap)
            if k in seen:
                return
            seen.add(k)
            if log is None:
                log = _open(os.path.join(os.environ["OMH_IOLOG"], "%s.%d.log" % (stage, pid)), "a", encoding="utf-8", buffering=1)
            log.write("%s %s\n" % (kind, ap))

        def hook(ev, args):
            if ev == "open":
                p, mode, flags = args
                if p is None or isinstance(p, int):
                    return
                m = mode or ""
                w = any(c in m for c in "wax+") or (isinstance(flags, int) and (flags & 0x3 or flags & 0x40 or flags & 0x200))
                note("W" if w else "R", p)
            elif ev == "os.remove" or ev == "os.unlink":
                note("D", args[0])
            elif ev in ("os.rename", "os.replace"):
                note("D", args[0])
                note("W", args[1])
            elif ev == "shutil.copyfile":
                note("R", args[0])
                note("W", args[1])
        sys.addaudithook(hook)
