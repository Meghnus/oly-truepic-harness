"""ofh.emu.rs_shim: OMH_RUST=1 selects the Rust core (_rs_emu, built from ofh/emu/rs_emu by `python -m ofh.emu.build`).  Default OFF.

What changes when it is on: pyemu.CPU.__init__ / _step / step / run / ld / st / load are replaced by thin wrappers around _rs_emu, and every
new CPU gets `mem = RPageMem()` (the Rust port of ofh.mem.pagemem.PageMem, same dict semantics).  Registers, flags, writes, trace and stubs stay
the CPU object's own Python attributes; the original Python _step runs every step the native code cannot reproduce exactly (stubs, unknown
operand forms, overridden helpers, odd register values).  Nothing a check computes changes: that is a CLAIM and the difftest package is
what tests it.

Selection: pyemu.configure() (profile.apply) calls install() when OMH_RUST is set.  OMH_RUST=dual is the lock-step differential mode of
ofh/difftest/dual.py (testing only: every native step is checked against the Python step).

Loud-failure rules (the point of this file's paranoia): a run that REQUESTED the Rust core must never silently be a pure-Python run.
  W1  OMH_RUST not in {unset, 0, 1, dual}                      -> fatal (exit 86)
  W2  the built module is missing, or its API / SRC_SHA differs from the sources on disk (stale build) -> fatal (exit 86)
  W3  patching pyemu fails                                      -> fatal (exit 86)
  W4  fatal() uses os._exit: no `except Exception`, atexit or finally in the harness can turn it into a green run
  W5  OMH_RS_STAT=1 writes one marker file per process (RS_CORE_INSTALLED / RS_CORE_LOADED + the engine counters) so a driver can PROVE
      afterwards that every stage ran natively (ofh.lane.cmp_runs --rs checks the markers)
The source hash ties a build to its sources: rs_srchash.src_sha() = sha256 over src/*.rs, Cargo.toml, Cargo.lock, rust-toolchain.toml, compiled
into the module as _rs_emu.SRC_SHA by `python -m ofh.emu.build` and recomputed here at import.
"""
import os
import sys
from collections.abc import MutableMapping

HERE = os.path.dirname(os.path.abspath(__file__))
RS_DIR = os.path.join(HERE, "rs_emu")
API = 3

from . import loud, rs_srchash                    # noqa: E402
from ofh.mem.pagemem import _View, PageMem as _PageMem, _ODD          # noqa: E402  (one implementation of the dict views / sentinel)

_SRC_SHA = rs_srchash.src_sha(RS_DIR)
try:
    from . import _rs_emu as _R                   # noqa: E402
except ImportError as _e:
    raise ImportError("ofh.emu.rs_shim: the Rust module is not built (%s): run `python -m ofh.emu.build` (needs cargo, see docs/emu.md)" % _e) from _e
if getattr(_R, "API", None) != API or getattr(_R, "SRC_SHA", None) != _SRC_SHA:
    raise ImportError("ofh.emu.rs_shim: stale or foreign _rs_emu (%s): module API %r SRC_SHA %r, sources API %r SRC_SHA %s -> rebuild with "
                      "`python -m ofh.emu.build`" % (getattr(_R, "__file__", "?"), getattr(_R, "API", None), getattr(_R, "SRC_SHA", None), API,
                                                  _SRC_SHA))

HELPERS = ("get", "put", "ea", "reg", "flags_sub", "flags_add", "flags_logic", "cond", "_chk")


def _rebuild(state):
    m = RPageMem()
    m._setstate(state)
    return m


class RPageMem(_R.MemCore, MutableMapping):
    """ofh.mem.pagemem.PageMem with its hot paths in Rust (MemCore); the methods below are PageMem's own code"""
    __slots__ = ()

    def __init__(self, init=None):
        if init:
            self.update(init)

    def _ordered_keys(self):
        ks = self._fast_keys()                                  # `not self.stale and not self.odd` -> list(self.log)
        if ks is not None:
            return ks
        log, oddlog, odd = self._log_list(), self.oddlog, self.odd
        last = {}
        oi = 0
        ev = []
        for i, a in enumerate(log):
            if a == _ODD:
                k = oddlog[oi]
                oi += 1
                ev.append(k)
                last[("o", k)] = i
            else:
                ev.append(a)
                last[("b", a)] = i
        out = []
        for i, k in enumerate(ev):
            if last.get(("b", k)) == i and k in self.pages_get_present(k):
                out.append(k)
            elif last.get(("o", k)) == i and k in odd and not (type(k) is int and k in self.pages_get_present(k)):
                out.append(k)
        return out

    def __iter__(self):
        return iter(self._ordered_keys())

    def keys(self):
        return _View(self, 0)

    def values(self):
        return _View(self, 1)

    def items(self):
        return _View(self, 2)

    def popitem(self):
        ks = self._ordered_keys()
        if not ks:
            raise KeyError("popitem(): dictionary is empty")
        k = ks[-1]
        v = self[k]
        del self[k]
        return k, v

    def copy(self):
        """copy-on-write clone (pages shared until a side writes)"""
        c = RPageMem()
        c._cow_from(self)
        return c

    __copy__ = copy

    def __eq__(self, other):
        if isinstance(other, (dict, _PageMem, RPageMem)):
            return len(self) == len(other) and all(k in other and other[k] == v for k, v in self.items())
        return NotImplemented

    __hash__ = None

    def __reduce__(self):
        """pickles as the plain dict it stands in for (loadable by any interpreter, Rust core or not)"""
        return (dict, (list(self.items()),))

    def __deepcopy__(self, memo):
        """deep copy that stays an RPageMem with its page structure intact"""
        import copy
        c = _rebuild(copy.deepcopy(self._getstate(), memo))
        memo[id(self)] = c
        return c


_PATCHED = {}


def _trapmsg(what, n, ad, pc):
    return 'misaligned %s u%d at %#x (pc=%#x)' % (what, 8 * n, ad, pc)


# Extension point: reviewed range-split Image.dis overrides the engine may shortcut.  {(module, qualname): (source text, 'glob'|'attr', lo name,
# side-cache attribute)}.  A function qualifies ONLY if its compiled code equals the reviewed text, so an edited copy is simply called through
# Python (correct, slower).  Empty by default: every Image.dis is called through Python unless it is pyemu.Image.dis itself.
REVIEWED_DIS = {}


def _dis_kind(func):
    """None, or (lo_spec, size, side_attr) for a reviewed range-split dis override (see REVIEWED_DIS); called once per function by the engine"""
    return None


def patch(E, cls=None):
    """install the Rust core on `cls` (default pyemu.CPU).  The Python semantics it delegates to (the original _step, the ld / st / load
    installed before it - plain or ofh.mem.pagemem's) are captured from E.CPU now."""
    C = E.CPU
    cls = cls or C
    if cls in _PATCHED:
        return
    H_init, H_ld, H_st, H_load, py_step = C.__init__, C.ld, C.st, C.load, C._step

    def __init__(self, img):
        H_init(self, img)
        self.mem = RPageMem()

    def _step(self):
        return _R.step_inner(self)

    def step(self):
        return _R.step(self)

    def run(self, start, stop, max_steps=200000):
        return _R.run(self, start, stop, max_steps)

    def ld(self, ad, n):
        return _R.ld(self, ad, n)

    def st(self, ad, n, v):
        return _R.st(self, ad, n, v)

    def load(self, ad, data):
        return _R.load(self, ad, data)

    if not _R.configured():
        _R.configure(base=E.BASE, img_lo=E.IMG_LO, align_fault=E.AlignFault, trapmsg=_trapmsg, py_step=py_step, h_ld=H_ld, h_st=H_st,
                     h_load=H_load, helpers={h: getattr(C, h) for h in HELPERS}, our_ld=ld, our_st=st, our_step=step, our_istep=_step,
                     img_dis=E.Image.dis, image_cls=E.Image, rpm_type=RPageMem, dis_kind=_dis_kind)
    else:
        raise RuntimeError("rs_shim.patch: _rs_emu already configured for another class")
    for k, v in (("__init__", __init__), ("_step", _step), ("step", step), ("run", run), ("ld", ld), ("st", st), ("load", load)):
        v.__qualname__ = "CPU." + k
        v.__module__ = "ofh.emu.rs_shim"
        setattr(cls, k, v)
    _PATCHED[cls] = dict(H_init=H_init, H_ld=H_ld, H_st=H_st, H_load=H_load, py_step=py_step)
    if os.environ.get("OMH_RS_STAT") == "1":
        import atexit
        _stats("loaded")                                        # the 'core loaded' marker exists even if the process dies later
        atexit.register(_stats, "exit")


def _stats(when):
    """OMH_RS_STAT=1: <OMH_RS_STAT_DIR or ./.omh_cache/rs_stat>/<OMH_PREFIX>rs_stat.<pid>.txt = 'RS_CORE_LOADED <src_sha> <when> <argv>' + the
    engine counters (ofh.lane.cmp_runs --rs reads them)"""
    try:
        d = os.environ.get("OMH_RS_STAT_DIR") or os.path.join(os.getcwd(), ".omh_cache", "rs_stat")
        os.makedirs(d, exist_ok=True)
        fd = os.open(os.path.join(d, "%srs_stat.%d.txt" % (os.environ.get("OMH_PREFIX", "ap_x_"), os.getpid())), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        head = "RS_CORE_INSTALLED" if when == "installed" else "RS_CORE_LOADED"
        os.write(fd, ("%s %s %s %s\n%s\n" % (head, _SRC_SHA[:16], when, " ".join(sys.argv)[:150], _R.stats())).encode("utf-8", "replace"))
        os.close(fd)
    except Exception:
        pass


fatal = loud.fatal


def install(E, mode):
    """called by pyemu.configure() when OMH_RUST is set; mode is the raw env value (validated by the caller, again here)"""
    if mode not in ("0", "1", "dual"):
        fatal("OMH_RUST=%r is not 0, 1 or dual" % mode)
    if mode == "0":
        return
    if os.environ.get("OMH_RS_STAT") == "1":
        _stats("installed")                                     # every OMH_RUST process gets a marker; patch() turns it into 'loaded'
    try:
        if mode == "dual":
            from ofh.difftest import dual
            dual.patch(E)
        else:
            patch(E)
    except BaseException:
        fatal("patching pyemu failed")
