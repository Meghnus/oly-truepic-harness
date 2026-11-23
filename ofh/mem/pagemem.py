"""ofh.mem.pagemem: PageMem = a drop-in, exact-semantics replacement of the emulator's `cpu.mem` dict (address -> byte).

Why: ofh.emu.pyemu.CPU.mem is a dict with one entry per byte ever stored / loaded (key int + slot ~ 90-110 B per byte).  PageMem keeps 256-byte
pages (data bytearray + a presence bitmask, an int, so that "absent" - the image / zero fallback of CPU.ld - stays distinct from "present
with value 0") plus an insertion-order log (array('q'), 8 B per byte) so that iteration order == the order a dict would give.
Anything a byte page cannot hold exactly (a key that is not an int / has no __index__, a key outside +-2^62, a value that is not an int in
0..255) lives in `odd` (a plain dict) with its own events in the same log (sentinel) -> dict semantics for EVERY key and value; `odd` is
empty in every real use.  Known divergence (never hit by the harness, listed for the record): overwriting a PRESENT byte key with an
out-of-range value moves that key to the end of the iteration order.

MutableMapping API, dict-exact: get / [] / []= / del / in / len / iter / keys / values / items / pop / popitem / setdefault / update /
clear / copy / ==.  Deleted + re-added key goes to the end.  copy() is copy-on-write (pages shared until a side writes).
"""
from array import array
from collections.abc import MutableMapping

PB = 8                      # page = 256 B
PS = 1 << PB
PM = PS - 1
_LO, _HI = -(1 << 62), (1 << 62)
_ODD = -(1 << 63)           # log sentinel: "the next odd-key event"
_ID = [0]


def _newid():
    _ID[0] += 1
    return _ID[0]


def _key(a):
    """normalise a key: int-like (numpy ints, bool) -> int; anything else unchanged"""
    if type(a) is int:
        return a
    if isinstance(a, (float, str, bytes, tuple)) or a is None:
        return a
    ix = getattr(a, "__index__", None)
    if ix is not None:
        try:
            return ix()
        except Exception:
            return a
    return a


class PageMem(MutableMapping):
    __slots__ = ("pages", "log", "odd", "oddlog", "n", "owner", "stale")
    # pages  : {pno: [data bytearray(256), presence mask (int), owner id]}
    # log    : array('q'); one event per fresh insertion; a present byte key is valid at its LAST event, earlier ones are stale
    # odd    : {key: value}; oddlog = [key, ...] in event order (one per _ODD sentinel in log)
    # stale  : number of stale events in log (deletes); 0 -> the log is exactly the dict order

    def __init__(self, init=None):
        self.pages = {}
        self.log = array("q")
        self.odd = {}
        self.oddlog = []
        self.n = 0
        self.owner = _newid()
        self.stale = 0
        if init:
            self.update(init)

    # ---- core --------------------------------------------------------------------------------------------------------------
    def _page(self, pno):
        p = self.pages.get(pno)
        if p is None:
            p = [bytearray(PS), 0, self.owner]
            self.pages[pno] = p
        elif p[2] != self.owner:
            p = [bytearray(p[0]), p[1], self.owner]
            self.pages[pno] = p
        return p

    def _bytepresent(self, a):
        p = self.pages.get(a >> PB)
        return p is not None and (p[1] >> (a & PM)) & 1

    def get(self, a, default=None):
        if type(a) is not int:
            a = _key(a)
        if self.odd:
            r = self.odd.get(a, self)
            if r is not self:
                return r
        if type(a) is int:
            p = self.pages.get(a >> PB)
            if p is not None:
                o = a & PM
                if (p[1] >> o) & 1:
                    return p[0][o]
        return default

    def __getitem__(self, a):
        r = self.get(a, self)
        if r is self:
            raise KeyError(a)
        return r

    def __contains__(self, a):
        return self.get(a, self) is not self

    def __setitem__(self, a, v):
        if type(a) is not int:
            a = _key(a)
        isint = type(a) is int and _LO <= a < _HI
        if isint and type(v) is int and 0 <= v < 256:
            if self.odd and a in self.odd:
                if self._bytepresent(a):
                    del self.odd[a]                   # a shadowing odd value is replaced by the byte
                else:
                    self.odd[a] = v                   # an odd-only key keeps its place (and stays odd)
                    return
            p = self._page(a >> PB)
            o = a & PM
            bit = 1 << o
            if not p[1] & bit:
                p[1] |= bit
                self.n += 1
                self.log.append(a)
            p[0][o] = v
        elif isint and self._bytepresent(a):
            self.odd[a] = v                           # shadow: the byte entry keeps the key's dict position
        else:
            if a not in self.odd:
                self.oddlog.append(a)
                self.log.append(_ODD)
                self.n += 1
            self.odd[a] = v

    def __delitem__(self, a):
        a = _key(a)
        found = False
        if type(a) is int and _LO <= a < _HI:
            pno, o = a >> PB, a & PM
            p = self.pages.get(pno)
            if p is not None and (p[1] >> o) & 1:
                p = self._page(pno)
                p[1] &= ~(1 << o)
                p[0][o] = 0
                self.n -= 1
                self.stale += 1
                found = True
        if a in self.odd:
            del self.odd[a]
            if not found:
                self.n -= 1
                self.stale += 1
            found = True
        if not found:
            raise KeyError(a)

    def __len__(self):
        return self.n

    # ---- iteration (dict order) ------------------------------------------------------------------------------------------
    def _ordered_keys(self):
        if not self.stale and not self.odd:
            return list(self.log)
        last = {}
        oi = 0
        ev = []
        for i, a in enumerate(self.log):
            if a == _ODD:
                k = self.oddlog[oi]
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
            elif last.get(("o", k)) == i and k in self.odd and not (type(k) is int and k in self.pages_get_present(k)):
                out.append(k)
        return out

    def pages_get_present(self, k):
        """helper for _ordered_keys: a one-element container that has k iff the byte key k is present in a page"""
        if type(k) is int:
            p = self.pages.get(k >> PB)
            if p is not None and (p[1] >> (k & PM)) & 1:
                return (k,)
        return ()

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

    def clear(self):
        self.pages = {}
        self.log = array("q")
        self.odd = {}
        self.oddlog = []
        self.n = 0
        self.stale = 0
        self.owner = _newid()

    def copy(self):
        """copy-on-write clone: the pages are shared and BOTH sides stop owning them (the first writer clones a page)"""
        c = PageMem.__new__(PageMem)
        c.pages = dict(self.pages)
        c.log = array("q", self.log)
        c.odd = dict(self.odd)
        c.oddlog = list(self.oddlog)
        c.n = self.n
        c.stale = self.stale
        self.owner = _newid()
        c.owner = _newid()
        return c

    __copy__ = copy

    def __eq__(self, other):
        if isinstance(other, (dict, PageMem)):
            return len(self) == len(other) and all(k in other and other[k] == v for k, v in self.items())
        return NotImplemented

    __hash__ = None

    def __repr__(self):
        return "PageMem(%d keys, %d pages)" % (self.n, len(self.pages))

    # ---- helpers for the fast CPU paths -----------------------------------------------------------------------------------
    def store(self, ad, n, v):
        """exact equivalent of `for i in range(n): mem[ad + i] = (v >> 8*i) & 0xff` for an int address ad >= 0"""
        if not (_LO <= ad and ad + n < _HI) or self.odd:
            for i in range(n):
                self[ad + i] = (v >> (8 * i)) & 0xff
            return
        o = ad & PM
        if o + n <= PS:
            p = self._page(ad >> PB)
            m = p[1]
            fresh = ((1 << n) - 1) << o
            nb = fresh & ~m
            if nb:
                if nb == fresh:
                    self.log.extend(range(ad, ad + n))
                    self.n += n
                else:
                    self.log.extend(x for x in range(ad, ad + n) if not (m >> (x & PM)) & 1)
                    self.n += bin(nb).count("1")
                p[1] = m | fresh
            p[0][o:o + n] = (v & ((1 << (8 * n)) - 1)).to_bytes(n, "little")
        else:
            for i in range(n):
                self[ad + i] = (v >> (8 * i)) & 0xff

    def put_bytes(self, ad, data):
        """exact equivalent of `for i, b in enumerate(data): mem[ad + i] = b` for bytes / bytearray `data` and an int address"""
        n = len(data)
        if not n:
            return
        if not (_LO <= ad and ad + n < _HI) or self.odd:
            for i in range(n):
                self[ad + i] = data[i]
            return
        i = 0
        while i < n:
            a = ad + i
            o = a & PM
            k = min(n - i, PS - o)
            p = self._page(a >> PB)
            m = p[1]
            fresh = ((1 << k) - 1) << o
            nb = fresh & ~m
            if nb:
                if nb == fresh:
                    self.log.extend(range(a, a + k))
                    self.n += k
                else:
                    self.log.extend(x for x in range(a, a + k) if not (m >> (x & PM)) & 1)
                    self.n += bin(nb).count("1")
                p[1] = m | fresh
            p[0][o:o + k] = data[i:i + k]
            i += k

    def load(self, ad, n):
        """(value, all_present): the little-endian value of n bytes at ad when every byte is present, else (None, False)"""
        p = self.pages.get(ad >> PB)
        if p is not None:
            o = ad & PM
            if o + n <= PS:
                need = ((1 << n) - 1) << o
                if (p[1] & need) == need:
                    return int.from_bytes(p[0][o:o + n], "little"), True
        return None, False


class _View:
    """keys / values / items view of a PageMem (a snapshot-free live view, iterable repeatedly; enough of the dict-view API for the harness)"""
    __slots__ = ("m", "k")

    def __init__(self, m, k):
        self.m, self.k = m, k

    def __iter__(self):
        m = self.m
        if self.k == 0:
            return iter(m._ordered_keys())
        if self.k == 1:
            return (m[a] for a in m._ordered_keys())
        return ((a, m[a]) for a in m._ordered_keys())

    def __len__(self):
        return len(self.m)

    def __contains__(self, x):
        if self.k == 0:
            return x in self.m
        if self.k == 2:
            try:
                return self.m[x[0]] == x[1]
            except (KeyError, TypeError, IndexError):
                return False
        return any(v == x for v in self)

    def __eq__(self, o):
        if self.k == 1:
            return list(self) == list(o)
        return set(self) == set(o)

    def __and__(self, o):
        return set(self) & set(o)

    def __or__(self, o):
        return set(self) | set(o)

    def __sub__(self, o):
        return set(self) - set(o)

    def __rsub__(self, o):
        return set(o) - set(self)

    def __xor__(self, o):
        return set(self) ^ set(o)

    __hash__ = None

    def __repr__(self):
        return repr(list(self))


def install(E):
    """patch the (configured) ofh.emu.pyemu module E: new CPUs get `mem = PageMem()`, and ld / st / load get PageMem fast paths (same alignment
    trap, write audit and image fall-back as the dict versions).  Opt in with OMH_MEM=page (ofh.emu.pyemu does it after configure())."""
    M32 = 0xffffffff
    BASE, IMG_LO = E.BASE, E.IMG_LO
    AlignFault = E.AlignFault
    _init, _ld, _st = E.CPU.__init__, E.CPU.ld, E.CPU.st

    def __init__(self, img):
        _init(self, img)
        self.mem = PageMem()

    def ld(self, ad, n):
        mem = self.mem
        if type(mem) is not PageMem:
            return _ld(self, ad, n)
        if self._trap and n > 1 and (ad & M32) & (n - 1):
            raise AlignFault('misaligned %s u%d at %#x (pc=%#x)' % ('load', 8 * n, ad & M32, self.pc))
        ad &= M32
        v, ok = mem.load(ad, n)
        if ok:
            return v
        v = 0
        d = self.img.d
        ld_ = len(d)
        for i in range(n):
            a = ad + i
            b = mem.get(a)
            if b is None:
                o = a - BASE
                b = d[o] if 0 <= o < ld_ and a >= IMG_LO else 0
            v |= b << (8 * i)
        return v

    def st(self, ad, n, v):
        mem = self.mem
        if type(mem) is not PageMem:
            return _st(self, ad, n, v)
        if self._trap and n > 1 and (ad & M32) & (n - 1):
            raise AlignFault('misaligned %s u%d at %#x (pc=%#x)' % ('store', 8 * n, ad & M32, self.pc))
        ad &= M32
        mem.store(ad, n, v)
        self.writes.append((ad, n, self.pc))

    def load(self, ad, data):
        mem = self.mem
        if type(mem) is not PageMem:
            for i, b in enumerate(data):
                mem[ad + i] = b
        elif isinstance(data, (bytes, bytearray)):
            mem.put_bytes(ad, data)
        else:
            for i, b in enumerate(data):
                mem[ad + i] = b

    E.CPU.__init__, E.CPU.ld, E.CPU.st, E.CPU.load = __init__, ld, st, load
