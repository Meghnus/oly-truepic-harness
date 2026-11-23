"""ofh.difftest.cpufuzz: random-instruction lock-step differential test, ofh.emu.pyemu.CPU (the reference) vs the Rust core, in one process.

  fuzz_lockstep(nprog, seed)   every program is a random instruction stream placed in a synthetic Image's decode cache: every mnemonic
        _step knows plus unknown / malformed ones, every operand form incl. malformed strings, branch targets inside the program, stubs with
        a real `ret` exit, random initial registers (sometimes non-u32 / bool values), flags (sometimes ints), alignment trap on / off,
        pre-filled memory.  After EVERY step the exception (type + message), d / a (value AND type), sp, pc, mdr, z n c v, lar, steps,
        _trap, the full writes and trace lists and the full ordered memory items are compared.  Variants: plain classes; subclasses with an
        overridden ld (logging) and an instance-level st override; a class-level helper override (get) -> full Python fallback; run() mode;
        an Image subclass with its own dis; a Rust CPU on a plain dict memory.
  fuzz_runcut(nprog, seed)     the same programs, but the Rust side is run in SEGMENTS: run(pc, stop | {random cut pcs}) repeatedly until the
        real stop, against ONE uninterrupted Python run.  This exercises the native multi-step loop's state cache across every
        Python-boundary re-entry (state written back before the stop, re-read on the next call).

Needs: the Rust module built, and OMH_RUST unset/0 (this module installs the Rust core on a SUBCLASS itself; the process-wide switch would
configure it twice).  No firmware, no profile: pyemu is configured with a synthetic base.

    python -m ofh.difftest.cpufuzz [nprog=2000] [seed=1]
"""
import os
import random
import sys

from ofh.emu import asm, pyemu as E          # noqa: F401

M32 = 0xffffffff
TEST_BASE = 0x400000
DATA = 0x10001000
STACK = 0x10008000
REGS = ['d0', 'd1', 'd2', 'd3', 'a0', 'a1', 'a2', 'a3', 'sp', 'mdr']
IMMS = ['0', '1', '-1', '0x10', '0xff', '0x7fffffff', '0x80000000', '0xffffffff', '-0x8', '12', '-128', '0X1f', '-0X10', '0x100000000',
        '012', '0x', 'psw', ' 5', '+3', '00', '1_0', '31', '33', '4', '2']
BAD = ['d5', 'x1', 'psw', 'D0', '', 'a9', 'd0x']
CONDS = list(E.CONDS)
RL = ['[d2,a2]', '[d2,d3,a2,a3]', '[]', '[d3]', '[a2,a3]', '[d2,x]', '[d0]', '[ d2]']
MNL = ['mov', 'movbu', 'movhu', 'clr', 'ext', 'nop', 'setlb_', 'extb', 'extbu', 'exth', 'exthu', 'add', 'sub', 'cmp', 'and', 'or', 'xor',
       'not', 'asl', 'lsr', 'asr', 'asl2', 'inc', 'inc4', 'udf00', 'udf01', 'udfu00', 'udfu01', 'mul', 'mulu', 'div', 'divu', 'btst',
       'setlb', 'jmp', 'call', 'movm', 'ret', 'retf', 'calls', 'bset', 'bclr', 'rets', 'foo']


_RCPU = []


def setup(seed):
    """configure pyemu (idempotent for the same base) and return (rs_shim, python CPU class, rust CPU class); the Rust core is installed on ONE
    subclass per process (the engine's configuration is global), reused by every entry point"""
    if os.environ.get("OMH_RUST") not in (None, "", "0"):
        raise RuntimeError("ofh.difftest.cpufuzz installs the Rust core on its own subclass: run it with OMH_RUST unset")
    E.configure(TEST_BASE)
    from ofh.emu import rs_shim
    if not _RCPU:
        _RCPU.append(type("RCPU", (E.CPU,), {}))
        rs_shim.patch(E, _RCPU[0])
    return rs_shim, E.CPU, _RCPU[0]


class Gen:
    def __init__(self, r):
        self.r = r
        self.mems = ['(a0)', '(a1)', '(sp)', '(d0,a0)', '(8,a1)', '(-4,sp)', '(0x%x)' % DATA, '(0x%x)' % (TEST_BASE + 0x1000), '(a0+)',
                     '(0x10, a2)', '(d1,d2)', '(a0', '()', '(x,y,z)', '(-0x4,a3)', '(mdr)', '(0x%x)' % (DATA + 2), '(1,a0)', '(0x20,sp)', '(a3)']

    def opnd(self, kind):
        r = self.r
        if kind == 'r':
            return r.choice(REGS) if r.random() < 0.9 else r.choice(BAD)
        if kind == 'i':
            return r.choice(IMMS)
        if kind == 'm':
            return r.choice(self.mems)
        return r.choice(REGS + IMMS + self.mems[:4])

    def instr(self, addrs):
        r = self.r
        t = lambda: '0x%x' % r.choice(addrs)
        k = r.random()
        if k < 0.12:
            c = r.choice(CONDS)
            return ('b' + c, [t()] if r.random() < 0.95 else [r.choice(IMMS)])
        if k < 0.16:
            return ('l' + r.choice(CONDS), [] if r.random() < 0.95 else ['x'])
        mn = r.choice(MNL)
        o = self.opnd
        if mn in ('mov', 'movbu', 'movhu'):
            f = r.random()
            if f < 0.35:
                return (mn, [o('r' if r.random() < .8 else 'i'), o('m')])
            if f < 0.7:
                return (mn, [o('m'), o('r')])
            return (mn, [o('x'), o('r')] if r.random() < 0.97 else [o('r')])
        if mn in ('clr', 'ext', 'extb', 'extbu', 'exth', 'exthu', 'not', 'asl2', 'inc', 'inc4'):
            return (mn, [o('r')] if r.random() < 0.97 else [])
        if mn in ('add', 'sub', 'cmp', 'and', 'or', 'xor', 'udf00', 'udf01', 'udfu00', 'udfu01', 'mul', 'mulu', 'div', 'divu', 'btst'):
            return (mn, [o('r' if r.random() < 0.6 else 'i'), o('r')])
        if mn in ('asl', 'lsr', 'asr'):
            return (mn, [o('r' if r.random() < .5 else 'i'), o('r')] if r.random() < 0.7 else [o('r')])
        if mn in ('jmp', 'calls'):
            return (mn, [t()] if r.random() < 0.6 else [r.choice(['(a0)', '(a1)', '(d0,a0)', '(0x%x)' % (TEST_BASE + 0x1000), 'x'])])
        if mn == 'call':
            op = [t()]
            if r.random() < 0.8:
                op.append(r.choice(RL))
                if r.random() < 0.8:
                    op.append(r.choice(['0', '4', '8', '12', '16', '-4', 'x']))
            return (mn, op)
        if mn == 'movm':
            if r.random() < 0.5:
                return (mn, [r.choice(RL), '(sp)'])
            return (mn, ['(sp)', r.choice(RL)] if r.random() < 0.95 else ['(a0)', '[d2]'])
        if mn in ('ret', 'retf'):
            return (mn, r.choice([[r.choice(RL), r.choice(['4', '8', '12', '0'])], ['4'], [], [r.choice(RL)], ['x']]))
        if mn in ('bset', 'bclr'):
            return (mn, [r.choice(['d0', 'd1', '0x1', '0x80', '0xff', '0x101']), o('m')])
        return (mn, [])

    def regval(self):
        r = self.r
        k = r.random()
        if k < 0.03:
            return r.choice([-1, 1 << 33, True, -(1 << 40)])
        if k < 0.45:
            return DATA + r.choice([0, 4, 8, 0x10, 1, 2, 3, 0x40, 0x100])
        if k < 0.55:
            return TEST_BASE + 0x1000 + r.choice([0, 4, 1])
        return r.choice([0, 1, 2, 0x7f, 0x80, 0xff, 0x7fffffff, 0x80000000, 0xffffffff, r.randrange(1 << 32), 3, 31, 33])

    def build(self, prog_len):
        r = self.r
        code = TEST_BASE + 0x20000
        addrs, a = [], code
        for _ in range(prog_len):
            addrs.append(a)
            a += r.randint(1, 6)
        end = a
        stub_at = addrs[r.randrange(len(addrs))] if r.random() < 0.3 else None
        cache = {}
        for i, va in enumerate(addrs):
            sz = addrs[i + 1] - va if i + 1 < len(addrs) else end - va
            mn, o = self.instr(addrs + [end])
            cache[va] = (mn, o, sz)
        stub = None
        if stub_at is not None:
            cache[stub_at] = ('ret', ['[d2]', '8'], cache[stub_at][2])

            def val(c):                       # reads / writes memory through the cpu, like a real stubbed service
                c.st(DATA + 0x80, 4, (c.d[1] if type(c.d[1]) is int else 0) & M32)
                return c.ld(DATA + 0x80, 2) + 1
            stub = (stub_at, E.stub_ret(val, ['d2'], 8))
        return addrs, end, cache, stub


def snap(c):
    t = lambda x: (type(x).__name__, x)
    return dict(d=[t(x) for x in c.d], a=[t(x) for x in c.a], sp=t(c.sp), pc=t(c.pc), mdr=t(c.mdr), z=t(c.z), n=t(c.n), c=t(c.c), v=t(c.v),
                lar=t(c.lar), steps=c.steps, trap=t(c._trap), writes=list(c.writes), trace=list(c.trace), mem=list(c.mem.items()),
                ldlog=list(c.__dict__.get("ldlog", [])))


def exc(f):
    try:
        f()
        return None
    except Exception as e:
        return (type(e).__name__, str(e))


def _classes(base):
    def mk_sub(b):
        class Sub(b):
            def ld(self, ad, n):
                self.__dict__.setdefault("ldlog", []).append((ad, n))
                return super().ld(ad, n)
        return Sub

    def mk_hsub(b):
        class HSub(b):
            def get(self, s):
                return b.get(self, s)
        return HSub
    return mk_sub(base), mk_hsub(base)


class DImg(E.Image):
    def dis(self, va):
        return E.Image.dis(self, va)


def mk_img(r, cache, dsub):
    cls = DImg if dsub else E.Image
    rnd = random.Random(r.random())
    img = cls(bytes(rnd.randrange(256) for _ in range(0x22000)))
    img.cache = dict(cache)
    return img


def _init_pair(r, gen, P, Rc, addrs, stub, img):
    cp, cr = P(img), Rc(img)
    assert type(cr.mem).__name__ == "RPageMem", type(cr.mem)
    init = dict(d=[gen.regval() for _ in range(4)], a=[gen.regval() for _ in range(4)], sp=STACK + r.choice([0, 0, 0, 2, -0x20]),
                mdr=gen.regval(), flags=[r.choice([True, False]) if r.random() < 0.97 else r.choice([0, 1, 2]) for _ in range(4)],
                align=r.random() < 0.6)
    pre = [(DATA + r.randrange(0x200), r.randrange(256)) for _ in range(r.randint(0, 40))]
    for c in (cp, cr):
        c.d, c.a = list(init["d"]), list(init["a"])
        c.sp, c.mdr = init["sp"], init["mdr"]
        c.z, c.n, c.c, c.v = init["flags"]
        c.align = init["align"]
        for ad, b in pre:
            c.mem[ad] = b
        if stub:
            c.stubs[stub[0]] = stub[1]
        c.pc = addrs[0]
    return cp, cr


def fuzz_lockstep(nprog=2000, seed=1):
    rs_shim, PCPU, RCPU = setup(seed)
    _R = rs_shim._R
    r = random.Random(seed)
    gen = Gen(r)
    PSub, _ = _classes(PCPU)
    RSub, _ = _classes(RCPU)
    PH, RH = _classes(PCPU)[1], _classes(RCPU)[1]
    stats = dict(progs=0, steps=0, excs=0, run=0)
    for prog in range(nprog):
        addrs, end, cache, stub = gen.build(r.randint(3, 60))
        variant = r.random()
        if variant < 0.12:
            P, Rc = PSub, RSub
        elif variant < 0.18:
            P, Rc = PH, RH
        else:
            P, Rc = PCPU, RCPU
        img = mk_img(r, cache, r.random() < 0.1)
        cp, cr = _init_pair(r, gen, P, Rc, addrs, stub, img)
        if r.random() < 0.08:
            cr.st = cr.st                                   # an instance-level override (bound method) -> rust calls it through python
            cp.st = cp.st
        if r.random() < 0.1:                                # rust CPU on a plain dict memory (the native dict path)
            cr.mem = dict(cr.mem.items())
        if r.random() < 0.05:                               # a non-int stub key: the stub test goes through the dict, not the int set
            cp.stubs["x"] = cr.stubs["x"] = None
        assert snap(cp) == snap(cr)
        if r.random() < 0.15:
            stats["run"] += 1
            stop = r.choice([{end, 0}, (end, 0), frozenset({end}), {end: 1}])
            ms = r.choice([5, 50, 400])
            ep = exc(lambda: cp.run(addrs[0], stop, ms))
            er = exc(lambda: cr.run(addrs[0], stop, ms))
            sp_, sr_ = snap(cp), snap(cr)
            assert ep == er, ("run exc", prog, ep, er)
            assert sp_ == sr_, ("run state", prog, {k: (sp_[k], sr_[k]) for k in sp_ if sp_[k] != sr_[k]})
            stats["steps"] += cp.steps
            stats["progs"] += 1
            continue
        for k in range(400):
            if cp.pc not in img.cache and cp.pc not in cp.stubs:
                break
            ep = exc(cp.step)
            er = exc(cr.step)
            stats["steps"] += 1
            sp_, sr_ = snap(cp), snap(cr)
            assert ep == er, ("exc", prog, k, hex(cp.pc) if type(cp.pc) is int else cp.pc, ep, er, cache.get(sp_["pc"][1]))
            assert sp_ == sr_, ("state", prog, k, {kk: (sp_[kk], sr_[kk]) for kk in sp_ if sp_[kk] != sr_[kk]})
            if ep:
                stats["excs"] += 1
                break
        stats["progs"] += 1
    return stats, _R.stats()


def fuzz_runcut(nprog=1000, seed=2):
    rs_shim, PCPU, RCPU = setup(seed)
    _R = rs_shim._R
    r = random.Random(seed)
    gen = Gen(r)
    stats = dict(progs=0, cuts=0, steps=0, excs=0)
    for prog in range(nprog):
        addrs, end, cache, stub = gen.build(r.randint(4, 60))
        img = mk_img(r, cache, False)
        cp, cr = _init_pair(r, gen, PCPU, RCPU, addrs, stub, img)
        cr.mem = cr.mem                                    # keep RPageMem
        ms = r.choice([50, 400, 3000])
        ep = exc(lambda: cp.run(addrs[0], {end, 0}, ms))
        cuts = set(r.sample(addrs, min(len(addrs), r.randint(1, 6))))
        er = None
        pc = addrs[0]
        for _ in range(60):
            er = exc(lambda: cr.run(pc, {end, 0} | cuts, ms))
            if er is not None or cr.pc in (end, 0):
                break
            stats["cuts"] += 1
            er = exc(cr.step)                               # run() stops BEFORE the cut pc: execute it with step(), then re-enter run()
            if er is not None or cr.pc in (end, 0):
                break
            pc = cr.pc                                      # resume exactly where the cut stopped (state must survive the boundary)
        if cr.pc not in (end, 0) and er is None:
            continue                                        # ran out of the 60-segment budget (tight loop): not comparable
        sp_, sr_ = snap(cp), snap(cr)
        if ep is not None or er is not None:
            assert ep == er or (ep and er and ep[0] == er[0]), ("runcut exc", prog, ep, er)
            if ep != er:
                continue
        # the cut run executed the same instruction stream: steps differ only if a cut pc was visited twice, which re-enters at the same pc
        for k in ("d", "a", "sp", "mdr", "z", "n", "c", "v", "lar", "mem", "writes"):
            assert sp_[k] == sr_[k], ("runcut state", prog, k, sp_[k], sr_[k])
        stats["steps"] += cp.steps
        stats["excs"] += ep is not None
        stats["progs"] += 1
    return stats, _R.stats()


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    s = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    a, rs = fuzz_lockstep(n, s)
    print("cpufuzz lockstep OK seed %d: %s; rust stats %s" % (s, a, rs))
    b, rs = fuzz_runcut(max(1, n // 2), s + 1)
    print("cpufuzz runcut OK seed %d: %s" % (s + 1, b))
