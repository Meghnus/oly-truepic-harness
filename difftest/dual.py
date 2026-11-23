"""difftest.dual: OMH_RUST=dual - lock-step differential mode for REAL worlds (testing only, never a production setting).

The native CPU._step / step / run stay installed exactly as under OMH_RUST=1 (no Python _step substitution),
so the native multi-step run() loop, its state cache and the trap-arming body of step() are what is verified.  CPU.run and CPU.step get a
thin wrapper that verifies whole SEGMENTS:
  * run(start, stop, max): split at stub addresses (stub steps run once, natively = the Python stub code); every stub-free stretch is ONE
    native run(pc, stop | stubs, max) call compared with the reference Python run on the same start state (stop / stubs not an exact set /
    dict -> per-step verification through the step wrapper);
  * step(): one native step compared with the reference Python step.
Reference = the pure-Python run / step / _step (instance-dict overrides for the reference run only) with the ld / st installed before the
Rust core (plain or mem.pagemem's: they reach memory through mem.get / mem[k] = v).
Memory:
  * RPageMem: Rust first, the undo journal restores memory, then the Python reference; the world continues on the PYTHON result;
  * exact dict (any size, every segment): the Python reference first on a copy-on-write overlay (`_Ov`: reads fall through to the real
    dict, writes stay in the overlay), state restored, then the Rust segment on the real dict; compared = every overlay key plus every
    address of the Rust write audit; the world continues on the RUST result.  No full copies, so every step is compared;
  * anything else: run natively, counted as skipped_mem.
Counters (OMH_DUAL_DIR or ./.omh_cache/dual / <OMH_PREFIX>dual.<pid>.txt): segments, steps_verified (steps compared), native_verified (native
engine steps inside compared Rust segments, from _rs_emu.stats()), mismatch, exc_same, stub, skipped_*.  OMH_DUAL_EVERY=k verifies a
seeded-random 1/k of the segments (default 1 = all).  OMH_DUAL_FAIL=1 raises AssertionError at the FIRST mismatch (otherwise it is
counted and lane.cmp_runs --dual fails the run afterwards: a mismatch is never silent either way)."""
import os
import sys
import time
import random
import atexit
from emu import rs_shim

_R = rs_shim._R
RPageMem = rs_shim.RPageMem
EVERY = int(os.environ.get("OMH_DUAL_EVERY", "1"))
RNG = random.Random(int(os.environ.get("OMH_DUAL_SEED", "12345")))
ST = dict(segments=0, steps_verified=0, native_verified=0, mismatch=0, exc_same=0, stub=0, skipped_mem=0, skipped_every=0, t0=time.time())
DET = []
_last = [time.time()]


def _out():
    d = os.environ.get("OMH_DUAL_DIR") or os.path.join(os.getcwd(), ".omh_cache", "dual")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "%sdual.%d.txt" % (os.environ.get("OMH_PREFIX", "ap_x_"), os.getpid()))


def _dump():
    try:
        ST["wall"] = round(time.time() - ST["t0"], 1)
        txt = "%s\n%s\nrust stats %s\n%s\n" % (" ".join(sys.argv)[:200], {k: v for k, v in ST.items() if k != "t0"}, _R.stats(), "\n".join(DET))
        fd = os.open(_out(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        os.write(fd, txt.encode("utf-8", "replace"))
        os.close(fd)
    except Exception:
        pass


def _t(x):
    return (type(x).__name__, x)


_MISS = object()


class _Ov(dict):
    """copy-on-write view of a dict memory for the Python reference: own items = the writes, reads fall through to `base`"""
    __slots__ = ("base",)

    def __init__(self, base):
        super().__init__()
        self.base = base

    def get(self, k, default=None):
        v = dict.get(self, k, _MISS)
        return self.base.get(k, default) if v is _MISS else v

    def __getitem__(self, k):
        v = dict.get(self, k, _MISS)
        return self.base[k] if v is _MISS else v

    def __contains__(self, k):
        return dict.__contains__(self, k) or k in self.base


def patch(E):
    P_run, P_step = E.CPU.run, E.CPU.step                       # pure-Python (or mem.pagemem's) run / step, captured before the Rust core
    rs_shim.patch(E)
    P = rs_shim._PATCHED[E.CPU]
    py_step, H_ld, H_st = P["py_step"], P["H_ld"], P["H_st"]
    R_run, R_step = E.CPU.run, E.CPU.step
    R_istep, R_ld, R_st = E.CPU._step, E.CPU.ld, E.CPU.st

    def snap(c):
        return ([*c.d], [*c.a], c.sp, c.pc, c.mdr, c.z, c.n, c.c, c.v, c.lar, c.steps, c._trap, len(c.writes), len(c.trace))

    def restore(c, s):
        c.d[:] = s[0]
        c.a[:] = s[1]
        c.sp, c.pc, c.mdr, c.z, c.n, c.c, c.v, c.lar, c.steps, c._trap = s[2:12]
        del c.writes[s[12]:]
        del c.trace[s[13]:]

    def outcome(c, s, res, mem):
        return dict(d=[_t(x) for x in c.d], a=[_t(x) for x in c.a], sp=_t(c.sp), pc=_t(c.pc), mdr=_t(c.mdr), z=_t(c.z), n=_t(c.n), c=_t(c.c),
                    v=_t(c.v), lar=_t(c.lar), steps=_t(c.steps), trap=_t(c._trap), writes=c.writes[s[12]:], trace=c.trace[s[13]:],
                    mem=mem, res=res)

    def call(f):
        try:
            return ("ok", _t(f())), None
        except Exception as ex:
            return ("exc", type(ex).__name__, str(ex)), ex

    def ref(self, f):
        """f() with the stock Python step / _step / ld / st (instance-dict overrides, removed afterwards)"""
        own = self.__dict__
        had = {k: own[k] for k in ("step", "_step", "ld", "st") if k in own}
        # only what is the Rust core on this class is replaced: a subclass's own step / _step / ld / st stay (they are the semantics)
        cls = type(self)
        ours = {"step": (R_step, step), "_step": (R_istep,), "ld": (R_ld,), "st": (R_st,)}
        for k, v in (("step", P_step), ("_step", py_step), ("ld", H_ld), ("st", H_st)):
            if k not in had and getattr(cls, k, None) in ours[k]:
                own[k] = v.__get__(self)
        try:
            return call(f)
        finally:
            for k in ("step", "_step", "ld", "st"):
                if k in had:
                    own[k] = had[k]
                else:
                    own.pop(k, None)

    def native():
        return _R.stats()["native"]

    def verify(self, rust, pyref):
        if EVERY > 1 and RNG.randrange(EVERY):
            ST["skipped_every"] += 1
            return rust()
        m = self.mem
        s = snap(self)
        if type(m) is RPageMem:
            m._jstart()
            n0 = native()
            rr, rex = call(rust)
            n1 = native()
            if self.mem is m:
                try:
                    md = ("R",) + tuple(m._jdelta())
                except RuntimeError as ex:
                    md = ("journal", str(ex))
            else:
                md = ("rebound",)
            ro = outcome(self, s, rr, md)
            restore(self, s)
            self.mem = m
            if m.journal_active():
                m._jrestore()
            m._jstart()
            pr, pex = ref(self, pyref)
            md = (("R",) + tuple(m._jdelta())) if self.mem is m else ("rebound",)
            po = outcome(self, s, pr, md)
            m._jstop()
            keep_ex = pex
        elif type(m) is dict:
            ov = _Ov(m)
            self.mem = ov
            try:
                pr, pex = ref(self, pyref)
            finally:
                rebound = self.mem is not ov
                self.mem = m
            pkeys = list(dict.keys(ov))
            po_mem = ("rebound",) if rebound else {k: ov.get(k) for k in pkeys}
            po = outcome(self, s, pr, None)
            restore(self, s)
            n0 = native()
            rr, rex = call(rust)
            n1 = native()
            if self.mem is not m:
                ro_mem = ("rebound",)
            else:
                # Python value of every key it wrote (captured above, before the Rust run touched the dict) vs the Rust value; an
                # address only the Rust audit names is a difference by itself (its write entry differs too)
                ro_mem = {k: m.get(k) for k in pkeys}
                for ad, w, _ in self.writes[s[12]:]:
                    if type(ad) is int and type(w) is int and 0 < w <= 8:
                        for k in range(ad, ad + w):
                            if k not in ro_mem:
                                ro_mem[k] = ("rust-only", m.get(k))
            ro = outcome(self, s, rr, ro_mem)
            po["mem"] = po_mem
            keep_ex = rex
        else:
            ST["skipped_mem"] += 1
            return rust()
        ST["segments"] += 1
        ST["native_verified"] += n1 - n0
        st0, st1 = s[10], self.steps
        if type(st0) is int and type(st1) is int:
            ST["steps_verified"] += max(st1 - st0, 0)
        if po != ro:
            ST["mismatch"] += 1
            if len(DET) < 40:
                DET.append("MISMATCH pc=%r steps=%r start d=%r a=%r sp=%r\n  diff %r\n  trace %r\n  writes %r\n  res %r / %r" % (
                    s[3], s[10], s[0], s[1], s[2], {k: (po[k], ro[k]) for k in po if po[k] != ro[k]}, ro["trace"][:6], ro["writes"][:6],
                    po["res"], ro["res"]))
            if os.environ.get("OMH_DUAL_FAIL") == "1":
                _dump()
                raise AssertionError("difftest.dual MISMATCH (see %s)\n%s" % (_out(), DET[-1] if DET else ""))
        elif keep_ex is not None:
            ST["exc_same"] += 1
        if time.time() - _last[0] > 30:
            _last[0] = time.time()
            _dump()
        if keep_ex is not None:
            raise keep_ex
        res = (po if type(m) is RPageMem else ro)["res"]
        return res[1][1]

    def nat(f):
        """f() with the plain native CPU.step on the class: the engine's run() treats any other class step as a Python step and would
        call this wrapper per step (nested verification, no multi-step native loop)"""
        def g():
            E.CPU.step = R_step
            try:
                return f()
            finally:
                E.CPU.step = step
        return g

    def step(self):
        st = self.stubs
        try:
            stub = self.pc in st
        except Exception:
            stub = False
        if stub:
            ST["stub"] += 1
            return R_step(self)
        return verify(self, nat(lambda: R_step(self)), lambda: P_step(self))

    def run(self, start, stop, max_steps=200000):
        self.pc = start
        stubs = self.stubs
        if type(stop) not in (set, frozenset) or type(stubs) is not dict:
            while self.pc not in stop:
                step(self)
                if self.steps > max_steps:
                    raise RuntimeError('max steps')
            return self.pc
        while True:
            if self.pc in stop:
                return self.pc
            if self.pc in stubs:
                ST["stub"] += 1
                R_step(self)
                if self.steps > max_steps:
                    raise RuntimeError('max steps')
                continue
            stop2 = set(stop)
            stop2.update(stubs)
            pc = self.pc
            verify(self, nat(lambda: R_run(self, pc, stop2, max_steps)), lambda: P_run(self, pc, stop2, max_steps))

    for k, v in (("step", step), ("run", run)):
        v.__qualname__ = "CPU." + k
        v.__module__ = "emu.rs_shim"
        setattr(E.CPU, k, v)
    atexit.register(_dump)
