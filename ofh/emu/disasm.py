"""ofh.emu.disasm: where pyemu gets its instructions from.

pyemu is a MNEMONIC-level interpreter: it asks `Image.dis(va)` for `(mnemonic, [operand strings], size)` and executes that.  A
disassembler backend supplies those tuples.  Three are provided; they all return the same thing, so everything above them (the Python
core, the Rust core, the difftest, the lane) is independent of which one is used.

  ListingDisassembler   a ready-made {address: (mn, ops, size)} map.  Used by the examples / tests (see ofh.emu.asm) and by anybody who
                        decodes their image with their own tool and loads the result.
  ExternalDisassembler  runs an external command once per window and parses its text output with a configurable regex.  The command and
                        the regex come from your profile; nothing is assumed about the tool.  A GNU binutils `mn10300` objdump-style tool
                        works if you give a regex for its line format; the default regex is the "address  mnemonic operands ; bytes"
                        layout (see DEFAULT_LINE).  binutils is GPL: it is only ever run as an external program, never bundled.
  (any object with .fetch(image, va) -> [(addr, mn, ops, size), ...] works too.)

Window cache: ExternalDisassembler remembers results on disk (JSON, atomic writes, safe across parallel processes), keyed by the address
and the sha1 of the 0x410 bytes the tool sees, so a changed byte can never serve a stale decode.  The cache directory is local, outside git
(`.omh_cache/` is in .gitignore) and holds decoded firmware: NEVER publish it.
"""
import hashlib
import json
import os
import re
import subprocess

DEFAULT_LINE = r"^(?P<addr>[0-9a-fA-F]{8})\s+(?P<mn>\S+)\s*(?P<ops>.*?)\s*;\s*(?P<bytes>[0-9a-fA-F ]+)$"
WINDOW = 0x400          # bytes decoded per external call
KEYSPAN = 0x410         # bytes hashed into the cache key (window + one longest instruction)


class DisassemblyError(KeyError):
    """no instruction is known at this address (outside the listing / the external tool printed nothing for it)"""


def split_ops(s):
    """'a,(b,c),[d2,d3]' -> ['a', '(b,c)', '[d2,d3]'] (commas inside () and [] do not split)"""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([":
            depth += 1
        if ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


class ListingDisassembler:
    """a prepared {address: (mnemonic, operand list, size)} map"""

    def __init__(self, listing):
        self.listing = dict(listing)

    def fetch(self, image, va):
        v = self.listing.get(va)
        if v is None:
            raise DisassemblyError("no instruction at %#x in the listing" % va)
        return [(va, v[0], list(v[1]), v[2])]


class ExternalDisassembler:
    """command: argv list with the placeholders {file} (a temp copy of the image bytes), {wslfile} (the same path as /mnt/<drive>/...),
    {base}, {start}, {end} (all hex with 0x prefix).  line_regex needs the named groups addr, mn, ops, bytes."""

    def __init__(self, command, line_regex=DEFAULT_LINE, cache_dir=None, window=WINDOW):
        if not command:
            raise ValueError("ExternalDisassembler: empty command (fill profile.disassembler.command)")
        self.command = list(command)
        self.rx = re.compile(line_regex)
        for g in ("addr", "mn", "ops", "bytes"):
            if g not in self.rx.groupindex:
                raise ValueError("line_regex lacks the named group %r" % g)
        self.cache_dir = cache_dir
        self.window = window
        self.calls = 0

    def _key(self, image, va):
        o = va - image.base
        if 0 <= o < len(image.d):
            return "%08x_%s" % (va, hashlib.sha1(bytes(image.d[o:o + KEYSPAN])).hexdigest()[:20])
        return None

    def fetch(self, image, va):
        key = self._key(image, va) if self.cache_dir else None
        if key:
            fn = os.path.join(self.cache_dir, key + ".json")
            if os.path.exists(fn):
                try:
                    with open(fn, "r", encoding="utf-8") as f:
                        return [(a, mn, ops, sz) for a, mn, ops, sz in json.load(f)]
                except (OSError, ValueError):
                    pass
        path = image.path
        wsl = "/mnt/" + path[0].lower() + path[2:].replace("\\", "/") if len(path) > 2 and path[1] == ":" else path
        argv = [a.format(file=path, wslfile=wsl, base="0x%x" % image.base, start="0x%x" % va, end="0x%x" % (va + self.window))
                for a in self.command]
        self.calls += 1
        r = subprocess.run(argv, capture_output=True, text=True)
        ent = []
        for ln in r.stdout.splitlines():
            m = self.rx.match(ln.strip())
            if m:
                ent.append((int(m.group("addr"), 16), m.group("mn"), split_ops(m.group("ops")), len(m.group("bytes").split())))
        if key and ent:
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                tmp = os.path.join(self.cache_dir, "%s.%d.tmp" % (key, os.getpid()))
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(ent, f)
                os.replace(tmp, os.path.join(self.cache_dir, key + ".json"))
            except OSError:
                pass
        return ent
