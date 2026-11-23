"""pyemu: MN10300 (AM33) mnemonic-level emulator.  The reference semantics of the toolkit: the Rust core (emu/rs_emu) is differentially tested
against this file, and every other piece (lane, difftest, examples) runs on either.

What it is: a small interpreter for the integer subset that firmware helper code uses (mov/movbu/movhu, ALU with full z/n/c/v flags, signed
and unsigned branches, setlb/Lcc loops, indexed and absolute addressing, multiply/divide, movm, call/calls/ret/retf/rets with the real
register-list and imm8 conventions, bset/bclr), reading its instructions from an `Image` (see emu.disasm for where they come from).
It is NOT a system emulator: unknown memory reads as 0 (or as the image bytes at/above the configured read floor), no MMIO, no interrupts,
and every store is recorded in `cpu.writes` for the write audit.  Use it to run a function or a patch hook on stock data and look at
what it does; model the services it calls with stubs.

Return semantics (MN103 ISA): call / calls set MDR <- return address (and store it at the stack slot); ret returns PC <- mem32(SP); retf
returns PC <- MDR.  A stub returns the way its REAL callee returns: the callee's first return instruction is found by a linear scan of its
code (cached per image); stubs for addresses outside the image must be declared in RET_KIND (configure(ret_kind=...)), otherwise the
stub raises.  This keeps a stubbed call from silently changing the caller's stack / callee-saved registers.

Configuration (module-level, set once with configure(); the Rust core reads the same values):
    BASE      address of image byte 0                       (required)
    IMG_LO    lowest address at which image bytes are readable as data (default BASE; below it reads give 0)
    RET_KIND  {stub address: 'ret' | 'retf' | (kind, regs, imm8)} for stubbed callees outside the image
Nothing firmware-specific is in this file: every address comes from your profile (emu.profile).
"""
import os
import tempfile

from . import loud
from .disasm import DisassemblyError, ListingDisassembler, split_ops  # noqa: F401  (re-exported)

BASE = None
IMG_LO = None
RET_KIND = {}
M32 = 0xffffffff
CONDS = ('eq', 'ne', 'gt', 'ge', 'le', 'lt', 'hi', 'cc', 'ls', 'cs', 'ra')


def configure(base, img_lo=None, ret_kind=None):
    """set the image base (and optionally the read floor and the RET_KIND table).  Must run before the first Image / CPU is used;
    changing it afterwards raises (the Rust core captures the values once)."""
    global BASE, IMG_LO, RET_KIND
    base = int(base)
    img_lo = base if img_lo is None else int(img_lo)
    new = {int(k, 0) if isinstance(k, str) else int(k): (tuple(v) if isinstance(v, list) else v) for k, v in (ret_kind or {}).items()}
    if BASE is not None and (BASE, IMG_LO) != (base, img_lo):
        raise RuntimeError("pyemu.configure: already configured with base=%#x img_lo=%#x" % (BASE, IMG_LO))
    BASE, IMG_LO = base, img_lo
    RET_KIND.clear()
    RET_KIND.update(new)
    _install_backends()


def _need_cfg():
    if BASE is None:
        raise RuntimeError("pyemu is not configured: call pyemu.configure(base=...) or emu.profile.apply(profile) first")


def s32(v):
    v &= M32
    return v - (1 << 32) if v & 0x80000000 else v


def num(s):
    s = s.strip()
    return int(s, 16) if s.lower().lstrip('-').startswith('0x') and not s.startswith('-') else int(s, 0) if not s.startswith('-0x') else -int(s[1:], 16)


class Image:
    """byte image (`d`, any bytes-like: bytes / bytearray / the copy-on-write maps of mem.shm) overlaid with patches {va: bytes}.
    Patches below/above the image go to `extra` (kept for callers that want them)."""

    def __init__(self, data, patches=None, disasm=None, base=None):
        _need_cfg()
        if base is not None and base != BASE:
            raise ValueError("Image base %#x differs from the configured BASE %#x" % (base, BASE))
        self.base = BASE
        self.d = bytearray(data) if isinstance(data, (bytes, bytearray, memoryview)) else data
        self.extra = {}
        for va, b in (patches or {}).items():
            o = va - BASE
            if 0 <= o and o + len(b) <= len(self.d):
                self.d[o:o + len(b)] = b
            else:
                self.extra[va] = bytes(b)
        self.disasm = disasm if disasm is not None else ListingDisassembler({})
        self.cache = {}
        self._path = None

    @classmethod
    def from_file(cls, filename, patches=None, disasm=None, shared=False):
        """the decoded image in `filename`; shared=True maps it copy-on-write through mem.shm (pages shared between processes)"""
        if shared:
            from mem import shm
            return cls(shm.shared_copy(filename), patches, disasm)
        with open(filename, "rb") as f:
            return cls(f.read(), patches, disasm)

    @property
    def path(self):
        """a private temp copy of the image bytes, created on first use (only an external disassembler needs it)"""
        if self._path is None:
            tf = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
            tf.write(self.d)
            tf.close()
            self._path = tf.name
        return self._path

    @path.setter
    def path(self, v):
        self._path = v

    def dis(self, va):
        if va in self.cache:
            return self.cache[va]
        for a, mn, ops, sz in self.disasm.fetch(self, va):
            self.cache.setdefault(a, (mn, ops, sz))
        try:
            return self.cache[va]
        except KeyError:
            raise DisassemblyError("no instruction decoded at %#x" % va) from None

    def close(self):
        if self._path is not None:
            os.unlink(self._path)
            self._path = None


class AlignFault(RuntimeError):
    """opt-in (OMH_ALIGN=1 or cpu.align=True): u16 at odd / u32 at non-4-aligned address by emitted code (the MN103 manual requires natural
    alignment; a model that silently accepts misaligned accesses lets a patch pass offline that restarts the camera)"""


class CPU:
    def __init__(self, img):
        _need_cfg()
        self.img = img
        self.d = [0] * 4
        self.a = [0] * 4
        self.sp = 0
        self.pc = 0
        self.mdr = 0
        self.z = self.n = self.c = self.v = False
        self.lar = None
        self.mem = {}
        self.writes = []
        self.trace = []
        self.stubs = {}
        self.steps = 0
        self.align = os.environ.get('OMH_ALIGN') == '1'
        self._trap = False

    # memory -------------------------------------------------------------
    def _chk(self, ad, n, what):
        if self._trap and n > 1 and (ad & M32) & (n - 1):
            raise AlignFault('misaligned %s u%d at %#x (pc=%#x)' % (what, 8 * n, ad & M32, self.pc))

    def ld(self, ad, n):
        self._chk(ad, n, 'load')
        ad &= M32
        v = 0
        for i in range(n):
            b = self.mem.get(ad + i)
            if b is None:
                o = ad + i - BASE
                b = self.img.d[o] if 0 <= o < len(self.img.d) and ad + i >= IMG_LO else 0
            v |= b << (8 * i)
        return v

    def st(self, ad, n, v):
        self._chk(ad, n, 'store')
        ad &= M32
        for i in range(n):
            self.mem[ad + i] = (v >> (8 * i)) & 0xff
        self.writes.append((ad, n, self.pc))

    def load(self, ad, data):
        for i, b in enumerate(data):
            self.mem[ad + i] = b

    # operands ------------------------------------------------------------
    def reg(self, s):
        return s in ('d0', 'd1', 'd2', 'd3', 'a0', 'a1', 'a2', 'a3', 'sp', 'mdr')

    def get(self, s):
        s = s.strip()
        if s == 'sp':
            return self.sp
        if s == 'mdr':
            return self.mdr
        if self.reg(s):
            return (self.d if s[0] == 'd' else self.a)[int(s[1])]
        return num(s) & M32

    def put(self, s, v):
        v &= M32
        if s == 'sp':
            self.sp = v
        elif s == 'mdr':
            self.mdr = v
        else:
            (self.d if s[0] == 'd' else self.a)[int(s[1])] = v

    def ea(self, s):
        inner = s.strip()[1:-1]
        if ',' in inner:
            x, y = [t.strip() for t in inner.split(',')]
            dx = self.get(x) if self.reg(x) else num(x)
            return (dx + self.get(y)) & M32
        if self.reg(inner):
            return self.get(inner)
        return num(inner) & M32

    def flags_sub(self, b, a):            # b - a
        r = (b - a) & M32
        self.z = r == 0
        self.n = bool(r & 0x80000000)
        self.c = (b & M32) < (a & M32)
        self.v = ((b ^ a) & (b ^ r) & 0x80000000) != 0
        return r

    def flags_add(self, b, a):
        r = (b + a) & M32
        self.z = r == 0
        self.n = bool(r & 0x80000000)
        self.c = (b & M32) + (a & M32) > M32
        self.v = (~(b ^ a) & (b ^ r) & 0x80000000) != 0
        return r

    def flags_logic(self, r):
        self.z = (r & M32) == 0
        self.n = bool(r & 0x80000000)
        self.c = self.v = False

    def cond(self, c):
        z, n, cc, v = self.z, self.n, self.c, self.v
        return {'eq': z, 'ne': not z, 'gt': not (z or (n != v)), 'ge': n == v, 'le': z or (n != v),
                'lt': n != v, 'hi': not (cc or z), 'cc': not cc, 'ls': cc or z, 'cs': cc, 'ra': True}[c]

    # execution -------------------------------------------------------------
    def ret_info(self, va):
        """(kind, regs, imm8) of the first return instruction of the code at va (linear scan, cached per image); kind = 'ret' / 'retf' /
        'rets'.  RET_KIND overrides (code outside the image)."""
        if va in RET_KIND:
            k = RET_KIND[va]
            return k if isinstance(k, tuple) else (k, None, None)
        kinds = self.img.__dict__.setdefault('ret_kinds', {})
        if va not in kinds:
            if not (BASE <= va < BASE + len(self.img.d)):
                raise AssertionError('stub at %#x outside the image: declare its return kind in profile ret_kind / pyemu.RET_KIND' % va)
            a = va
            for _ in range(4000):
                mn, o, sz = self.img.dis(a)
                if mn in ('ret', 'retf', 'rets'):
                    kinds[va] = (mn,) + ret_operands(o)
                    break
                a += sz
            else:
                raise AssertionError('no return found for stub %#x' % va)
        return kinds[va]

    def ret_kind(self, va):
        return self.ret_info(va)[0]

    def step(self):
        """trap armed only while executing emitted instructions (not stubs, not harness ld/st)."""
        self._trap = self.align and self.pc not in self.stubs
        try:
            self._step()
        finally:
            self._trap = False

    def _step(self):
        if self.pc in self.stubs:
            # The stub computes the callee's effect (d0, stores); the EXIT is the real callee's: its own ret/retf register list and imm8, PC
            # from the stack slot (ret) or from MDR (retf).
            kind, rregs, rimm = self.ret_info(self.pc)
            sp0, mdr0 = self.sp, self.mdr
            keep = {r: self.get(r) for r in ('d2', 'd3', 'a2', 'a3')}
            self.stubs[self.pc](self)
            if rregs is None:                           # declared kind only (RET_KIND): trust the stub frame
                if kind == 'retf':
                    self.pc = mdr0
                return
            assert (self.sp - sp0) & M32 == rimm, ('stub frame != real callee ret imm8', hex(self.pc), rimm)
            for r in ('d2', 'd3', 'a2', 'a3'):           # callee-saved regs its ret does not restore: untouched
                if r not in rregs:
                    self.put(r, keep[r])
            for i, r in enumerate(rregs):                # HW frame order: first-listed register at the top
                self.put(r, self.ld(sp0 + rimm - 4 * (i + 1), 4))
            self.sp = (sp0 + rimm) & M32
            self.pc = self.ld(self.sp, 4) if kind != 'retf' else mdr0
            return
        mn, o, sz = self.img.dis(self.pc)
        self.steps += 1
        if len(self.trace) < 4000:
            self.trace.append((self.pc, mn, o))
        npc = (self.pc + sz) & M32
        if mn in ('mov', 'movbu', 'movhu'):
            w = {'mov': 4, 'movhu': 2, 'movbu': 1}[mn]
            src, dst = o[0], o[1]
            if dst.startswith('('):
                self.st(self.ea(dst), w, self.get(src))
            elif src.startswith('('):
                self.put(dst, self.ld(self.ea(src), w))
            else:
                self.put(dst, self.get(src))
        elif mn == 'clr':
            self.put(o[0], 0)
            self.z, self.n, self.c, self.v = True, False, False, False
        elif mn in ('ext', 'nop', 'setlb_'):
            if mn == 'ext':
                self.mdr = M32 if self.get(o[0]) & 0x80000000 else 0
        elif mn == 'extb':
            v = self.get(o[0]) & 0xff
            self.put(o[0], v - 256 if v & 0x80 else v)
        elif mn == 'extbu':
            self.put(o[0], self.get(o[0]) & 0xff)
        elif mn == 'exth':
            v = self.get(o[0]) & 0xffff
            self.put(o[0], v - 65536 if v & 0x8000 else v)
        elif mn == 'exthu':
            self.put(o[0], self.get(o[0]) & 0xffff)
        elif mn == 'add':
            r = self.flags_add(self.get(o[1]), self.get(o[0]))
            self.put(o[1], r)
        elif mn == 'sub':
            r = self.flags_sub(self.get(o[1]), self.get(o[0]))
            self.put(o[1], r)
        elif mn == 'cmp':
            self.flags_sub(self.get(o[1]), self.get(o[0]))
        elif mn in ('and', 'or', 'xor'):
            f = {'and': lambda x, y: x & y, 'or': lambda x, y: x | y, 'xor': lambda x, y: x ^ y}[mn]
            r = f(self.get(o[1]), self.get(o[0]))
            self.put(o[1], r)
            self.flags_logic(r)
        elif mn == 'not':
            r = ~self.get(o[0]) & M32
            self.put(o[0], r)
            self.flags_logic(r)
        elif mn in ('asl', 'lsr', 'asr'):
            k = self.get(o[0]) & 31 if len(o) > 1 else 1
            r_ = o[1] if len(o) > 1 else o[0]
            v = self.get(r_)
            r = (v << k) & M32 if mn == 'asl' else (v >> k) if mn == 'lsr' else (s32(v) >> k) & M32
            self.put(r_, r)
            self.flags_logic(r)
        elif mn == 'asl2':
            r = (self.get(o[0]) << 2) & M32
            self.put(o[0], r)
            self.flags_logic(r)
        elif mn in ('inc', 'inc4'):
            # MN103: INC Dn sets V/C/N/Z like ADD 1 (copy loops count a data register up to zero with `inc dN ; lne`); INC An / INC4 An
            # leave the flags
            v = self.get(o[0])
            if mn == 'inc' and o[0] in ('d0', 'd1', 'd2', 'd3'):
                self.put(o[0], self.flags_add(v, 1))
            else:
                self.put(o[0], v + (4 if mn == 'inc4' else 1))
        elif mn in ('udf00', 'udf01', 'udfu00', 'udfu01', 'mul', 'mulu'):
            a, b = self.get(o[0]), self.get(o[1])
            signed = mn in ('udf00', 'udfu00', 'mul')
            r = s32(a) * s32(b) if signed else a * b
            self.put(o[1], r)
            self.flags_logic(r & M32)
            if mn in ('mul', 'mulu'):
                self.mdr = (r >> 32) & M32
            # udf00/udf01 (extension-unit multiply) leave MDR alone
        elif mn in ('div', 'divu'):
            a, b = self.get(o[0]), self.get(o[1])
            if mn == 'div':
                a, b = s32(a), s32(b)
                q = int(b / a) if a else 0
                r = b - q * a if a else 0
            else:
                q = b // a if a else 0
                r = b % a if a else 0
            self.put(o[1], q)
            self.mdr = r & M32
            self.flags_logic(q & M32)
        elif mn == 'btst':
            self.flags_logic(self.get(o[1]) & self.get(o[0]))
        elif mn == 'setlb':
            self.lar = npc
        elif mn[0] == 'l' and mn[1:] in CONDS and not o:
            if self.cond(mn[1:]):
                npc = self.lar
        elif mn[0] == 'b' and mn[1:] in CONDS:
            if self.cond(mn[1:]):
                npc = num(o[0]) & M32
        elif mn == 'jmp':
            npc = self.ea(o[0]) if o[0].startswith('(') else num(o[0]) & M32
        elif mn in ('call',):
            tgt = num(o[0]) & M32
            regs = [r for r in o[1].strip('[]').split(',') if r] if len(o) > 1 else []
            imm8 = num(o[2]) if len(o) > 2 else 0
            csp = self.sp
            self.sp = (self.sp - imm8) & M32
            for i, r in enumerate(regs):                 # HW order (movm/call): first-listed register at the top
                self.st(self.sp + imm8 - 4 * (i + 1), 4, self.get(r))
            self.st(csp, 4, npc)
            self.mdr = npc
            npc = tgt
        elif mn == 'movm':
            # HW order: push stores the first-listed register at the highest address (d2 at sp-4, ..., a3 lowest)
            if o[1] == '(sp)':
                regs = [r for r in o[0].strip('[]').split(',') if r]
                assert all(r in ('d2', 'd3', 'a2', 'a3') for r in regs), ('movm list', o)
                for i, r in enumerate(regs):
                    self.st(self.sp - 4 * (i + 1), 4, self.get(r))
                self.sp = (self.sp - 4 * len(regs)) & M32
            else:
                assert o[0] == '(sp)', ('movm form', o)
                regs = [r for r in o[1].strip('[]').split(',') if r]
                assert all(r in ('d2', 'd3', 'a2', 'a3') for r in regs), ('movm list', o)
                n = len(regs)
                for i, r in enumerate(regs):
                    self.put(r, self.ld(self.sp + 4 * n - 4 * (i + 1), 4))
                self.sp = (self.sp + 4 * n) & M32
        elif mn in ('ret', 'retf'):
            regs, imm8 = ret_operands(o)
            for i, r in enumerate(regs):
                self.put(r, self.ld(self.sp + imm8 - 4 * (i + 1), 4))
            self.sp = (self.sp + imm8) & M32
            npc = self.ld(self.sp, 4) if mn == 'ret' else self.mdr
        elif mn == 'calls':
            self.st(self.sp, 4, npc)
            self.mdr = npc
            npc = self.ea(o[0]) if o[0].startswith('(') else num(o[0]) & M32
        elif mn in ('bset', 'bclr'):                     # byte op (atomic on HW): Z = ((mem8 & mask) == 0)
            mask = self.get(o[0]) & 0xff                 # mask = Dm or imm8; (An) / (d8,An) / (abs)
            ad = self.ea(o[1])
            t = self.ld(ad, 1)
            self.z, self.n, self.c, self.v = (t & mask) == 0, False, False, False
            self.st(ad, 1, (t | mask) if mn == 'bset' else (t & ~mask & 0xff))
        elif mn == 'rets':                               # return from a CALLS subroutine: PC <- (SP); SP and MDR unchanged
            npc = self.ld(self.sp, 4)
        else:
            raise NotImplementedError('%s %s @ %08x' % (mn, o, self.pc))
        self.pc = npc

    def run(self, start, stop, max_steps=200000):
        self.pc = start
        while self.pc not in stop:
            self.step()
            if self.steps > max_steps:
                raise RuntimeError('max steps')
        return self.pc


def ret_operands(o):
    """(regs, imm8) of 'ret / retf [regs],imm8' operand lists."""
    regs, imm8 = [], 0
    if o and o[0].startswith('['):
        regs = [r for r in o[0].strip('[]').split(',') if r]
        imm8 = num(o[1]) if len(o) > 1 else 0
    elif o:
        imm8 = num(o[0])
    return regs, imm8


def stub_ret(value, regs, imm8):
    """stub for 'call f,[regs],imm8' callees: d0 = value(cpu), then a ret [regs],imm8.  CPU.step replaces this exit with the REAL callee's
    exit (its ret/retf list, imm8, stack slot or MDR)."""
    def f(cpu):
        v = value(cpu)
        if v is not None:
            cpu.d[0] = v & M32
        for i, r in enumerate(regs):
            cpu.put(r, cpu.ld(cpu.sp + imm8 - 4 * (i + 1), 4))
        cpu.sp = (cpu.sp + imm8) & M32
        cpu.pc = cpu.ld(cpu.sp, 4)
    return f


_INSTALLED = [False]


def _install_backends():
    """once, right after configure(): OMH_MEM=page -> mem.pagemem fast paths; OMH_RUST=1|dual -> the Rust core (chained AFTER the memory
    patch, so the Rust memory wins).  A requested backend that cannot be installed is fatal (emu.rs_shim.fatal, exit 86): a run that
    asked for the Rust core must never silently be a pure-Python run."""
    if _INSTALLED[0]:
        return
    _INSTALLED[0] = True
    import sys
    E = sys.modules[__name__]
    if os.environ.get("OMH_MEM") == "page":
        from mem import pagemem
        pagemem.install(E)
    mode = os.environ.get("OMH_RUST")
    if mode not in (None, "", "0", "1", "dual"):
        loud.fatal("OMH_RUST=%r is not 0, 1 or dual" % mode)                      # rule W1
    if mode in ("1", "dual"):
        try:
            from . import rs_shim
        except BaseException:
            loud.fatal("the Rust core was requested but cannot be imported (not built / stale build)")      # rule W2
        rs_shim.install(E, mode)
