"""emu.asm: a tiny hand assembler at pyemu's own level, for tests and examples that must run WITHOUT any firmware.

pyemu executes `(mnemonic, [operands], size)` tuples, not machine code, so "assembling" is: give every instruction an address and a size and
resolve labels.  The result is a Program whose listing feeds emu.disasm.ListingDisassembler.  The sizes are arbitrary (default 3) because
nothing here emits real MN10300 encodings; a `call`/`ret` pair still follows the real stack discipline.

    ; comment (also # ...)
    start:
        mov 5,d0
        add d0,d0
    loop:
        sub 1,d0
        bne loop            ; label operands become hex addresses (also inside  jmp (label)  /  call label,[d2],0 )
        movhu (a0),d1 @2    ; '@N' sets this instruction's size in bytes
        ret [d2,d3],8

    prog = assemble(text, base=0x1000)
    prog.labels['loop'] -> address;  prog.listing -> {address: (mn, ops, size)};  prog.end -> first free address
"""
import re

from .disasm import ListingDisassembler, split_ops

_LABEL = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*):\s*(.*)$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_REGS = {"d0", "d1", "d2", "d3", "a0", "a1", "a2", "a3", "sp", "mdr", "psw"}


class Program:
    def __init__(self, base, listing, labels, end):
        self.base, self.listing, self.labels, self.end = base, listing, labels, end

    def disassembler(self):
        return ListingDisassembler(self.listing)


def _sub(op, labels):
    """replace a label inside one operand: `L`, `(L)`, `(L,a0)`, `(8,L)`"""
    if _IDENT.match(op) and op not in _REGS and op in labels:
        return "0x%x" % labels[op]
    if op.startswith("(") and op.endswith(")"):
        inner = [t.strip() for t in op[1:-1].split(",")]
        inner = ["0x%x" % labels[t] if t in labels and t not in _REGS else t for t in inner]
        return "(" + ",".join(inner) + ")"
    return op


def assemble(text, base, default_size=3):
    lines = []
    for raw in text.splitlines():
        s = re.split(r"[;#]", raw, maxsplit=1)[0].strip()
        if not s:
            continue
        m = _LABEL.match(s)
        while m:
            lines.append(("label", m.group(1)))
            s = m.group(2).strip()
            m = _LABEL.match(s)
        if s:
            size = default_size
            ms = re.search(r"\s@(\d+)\s*$", s)
            if ms:
                size = int(ms.group(1))
                s = s[:ms.start()].strip()
            parts = s.split(None, 1)
            lines.append(("ins", parts[0], split_ops(parts[1]) if len(parts) > 1 else [], size))
    labels, va = {}, base
    for ln in lines:
        if ln[0] == "label":
            if ln[1] in labels:
                raise ValueError("duplicate label %r" % ln[1])
            labels[ln[1]] = va
        else:
            va += ln[3]
    listing, va = {}, base
    for ln in lines:
        if ln[0] == "ins":
            listing[va] = (ln[1], [_sub(o, labels) for o in ln[2]], ln[3])
            va += ln[3]
    return Program(base, listing, labels, va)
