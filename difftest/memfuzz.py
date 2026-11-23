"""difftest.memfuzz: random-operation differential tests of the memories.

  fuzz_mem(trials, seed)      mem.pagemem.PageMem (the reference) and emu.rs_shim.RPageMem (the Rust MemCore) side by side (+ a plain dict
        where PageMem is dict-exact).  After EVERY op: the result / raised exception (type + args), len, full ORDERED items / keys / values /
        iteration, and for a sample of keys get / in.  Covers odd keys (str, float, tuple, huge / negative ints, bool, numpy ints when
        installed), odd values, delete / re-add, shadowing, pop / popitem / setdefault / update / clear, copy-on-write copies (both sides
        written afterwards), store / put_bytes / load helpers, pickle + deepcopy round trip, ==.
  fuzz_journal(trials, seed)  the undo journal difftest.dual relies on: random op batches between _jstart and _jrestore leave the memory
        exactly as before; two memories given the same batch report equal _jdelta; a batch run under the journal and then _jstop() ends in
        the same state as the batch without a journal.

    python -m difftest.memfuzz [trials=400] [seed=11]
"""
import copy
import pickle
import random
import sys

from mem.pagemem import PageMem
from emu.rs_shim import RPageMem

try:
    import numpy as np
except ImportError:
    np = None


def _call(f):
    try:
        return ("ok", f())
    except Exception as e:
        return ("exc", type(e).__name__, repr(e.args))


def _norm(x):
    if isinstance(x, tuple) and x and x[0] == "ok":
        v = x[1]
        if isinstance(v, (PageMem, RPageMem)):
            return ("ok", "MEM", list(v.items()))
        return ("ok", v)
    return x


def _state(m):
    return (len(m), list(m.items()), list(m.keys()), list(m.values()), list(m))


def fuzz_mem(trials=400, seed=11):
    r = random.Random(seed)
    nops = 0
    for trial in range(trials):
        base = r.choice([0, 0x12344000, 0x30004000, 0xffffff00, 0x1000, -300, (1 << 62) - 400])     # arbitrary page-boundary / range probes
        odd_mode = r.random() < 0.4
        P, Q = PageMem(), RPageMem()
        D = {} if not odd_mode else None
        for _ in range(r.randint(1, 500)):
            op = r.random()
            a = base + r.randint(0, 700)
            if odd_mode and r.random() < 0.25:
                a = r.choice([str(a % 7), float(a % 5), (1, a % 3), 1 << 63, -(1 << 63) - 1, True, False, None, b"k"] +
                             ([np.int64(a)] if np is not None else []))
            if op < 0.45:
                v = r.choice([r.randint(0, 255), r.randint(0, 255), 0, 255] + ([300, -1, True, "s", None, 2.0] if odd_mode else []))
                res = [_norm(_call(lambda m=m: m.__setitem__(a, v))) for m in (P, Q)]
                if D is not None:
                    D[a] = v
            elif op < 0.55:
                res = [_norm(_call(lambda m=m: m.__delitem__(a))) for m in (P, Q)]
                if D is not None:
                    D.pop(a, None)
            elif op < 0.63:
                n = r.choice([0, 1, 2, 4, 8])
                v = r.choice([r.randint(-5, 2**33), -(1 << 70) + 3, r.randint(0, 255)])
                res = [_norm(_call(lambda m=m: m.store(a, n, v))) for m in (P, Q)] if isinstance(a, int) and type(a) is not bool else None
                if res and D is not None:
                    for i in range(n):
                        D[a + i] = (v >> (8 * i)) & 0xff
            elif op < 0.67:
                data = r.choice([bytes(r.randrange(256) for _ in range(r.randint(0, 300))), bytearray(b"\x01\x02\x03"), b""])
                res = [_norm(_call(lambda m=m: m.put_bytes(a, data))) for m in (P, Q)] if isinstance(a, int) and type(a) is not bool else None
                if res and D is not None:
                    for i, b in enumerate(data):
                        D[a + i] = b
            elif op < 0.70:
                n = r.choice([0, 1, 2, 4, 8, 16])
                res = [_norm(_call(lambda m=m: m.load(a, n))) for m in (P, Q)] if type(a) is int else None
            elif op < 0.74:
                k = r.choice(list(P) or [a])
                res = [_norm(_call(lambda m=m: m.pop(k))) for m in (P, Q)]
                if D is not None:
                    D.pop(k, None)
            elif op < 0.76:
                res = [_norm(_call(lambda m=m: m.popitem())) for m in (P, Q)]
                if D is not None and D:
                    D.popitem()
            elif op < 0.79:
                res = [_norm(_call(lambda m=m: m.setdefault(a, 7))) for m in (P, Q)]
                if D is not None:
                    D.setdefault(a, 7)
            elif op < 0.82:
                res = [_norm(_call(lambda m=m: m.update({a: 1, a + 1 if type(a) is int else "u": 2}))) for m in (P, Q)]
                if D is not None:
                    D.update({a: 1, a + 1 if type(a) is int else "u": 2})
            elif op < 0.88:
                res = [_norm(_call(lambda m=m: (m.get(a), a in m, m.get(a, "dflt")))) for m in (P, Q)]
                res += [_norm(_call(lambda m=m: m[a])) for m in (P, Q)]
                res = [res[0], res[1]] if res[0] != res[1] else [res[2], res[3]]
            elif op < 0.93:
                P2, Q2 = P.copy(), Q.copy()
                assert _state(P2) == _state(Q2)
                if r.random() < 0.5:
                    P, Q = P2, Q2
                else:
                    for m in (P2, Q2):
                        _call(lambda m=m: m.__setitem__(a, 9))
                    assert _state(P2) == _state(Q2)
                res = None
            elif op < 0.95:
                res = [_norm(_call(lambda m=m: m.clear())) for m in (P, Q)]
                if D is not None:
                    D.clear()
            elif op < 0.97:
                # deepcopy keeps the whole page structure.  pickle gives the plain dict the memory stands in for (documented): that is
                # lossless only when no key aliases another under Python equality (0 / False / 0.0), so the odd-key trials skip it
                if odd_mode or r.random() < 0.5:
                    Q = copy.deepcopy(Q)
                else:
                    Q = RPageMem(pickle.loads(pickle.dumps(Q)))
                res = None
            else:
                res = [(P == Q, Q == P, P == dict(P.items()), Q == dict(P.items()))] * 2
            nops += 1
            if res is not None:
                assert res[0] == res[1], ("result", trial, op, a, res)
            sp, sq = _state(P), _state(Q)
            assert sp == sq, ("state", trial, op, a, sp[:2], sq[:2])
            if D is not None:
                assert list(D.items()) == sp[1], ("dict", trial, op, a)
    return nops


def _jst(m):
    return (len(m), list(m.items()), repr(m), [m.load(a, 4) for a in range(0x1000, 0x1400, 61)])


def _batch(seed):
    q = random.Random(seed)
    ops = []
    for _ in range(q.randint(0, 40)):
        k = q.random()
        a = 0x1000 + q.randint(0, 0x500)
        if k < 0.5:
            ops.append(("set", a, q.choice([q.randrange(256), 300, "x"]) if q.random() < 0.1 else q.randrange(256)))
        elif k < 0.65:
            ops.append(("del", a))
        elif k < 0.85:
            ops.append(("store", a, q.choice([0, 1, 2, 4]), q.randrange(1 << 32)))
        elif k < 0.95:
            ops.append(("put", a, bytes(q.randrange(256) for _ in range(q.randint(0, 9)))))
        else:
            ops.append(("set", q.choice(["s", 1.5, (1 << 63)]), 5))
    return ops


def _apply(m, ops):
    for o in ops:
        try:
            if o[0] == "set":
                m[o[1]] = o[2]
            elif o[0] == "del":
                del m[o[1]]
            elif o[0] == "store":
                m.store(o[1], o[2], o[3])
            else:
                m.put_bytes(o[1], o[2])
        except KeyError:
            pass


def fuzz_journal(trials=300, seed=21):
    r = random.Random(seed)
    n = 0
    for t in range(trials):
        m = RPageMem()
        _apply(m, _batch(r.random()))
        for _ in range(20):
            before = _jst(m)
            ops = _batch(r.random())
            twin = m.copy()
            m._jstart()
            _apply(m, ops)
            d1 = m._jdelta()
            twin._jstart()
            _apply(twin, ops)
            assert twin._jdelta() == d1, ("delta", t)
            twin._jstop()
            m._jrestore()
            assert _jst(m) == before, ("restore", t)
            if r.random() < 0.5:
                m._jstart()
                _apply(m, ops)
                m._jstop()
                assert _jst(m) == _jst(twin), ("stop", t)
            n += 1
    return n


if __name__ == "__main__":
    t = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    s = int(sys.argv[2]) if len(sys.argv) > 2 else 11
    print("memfuzz OK: %d ops (PageMem == RPageMem after every op), seed %d" % (fuzz_mem(t, s), s))
    print("journal OK: %d journaled batches" % fuzz_journal(max(1, t * 3 // 4), s + 10))
