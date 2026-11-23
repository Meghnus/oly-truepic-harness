"""emu.profile: everything body-specific lives in a JSON profile that YOU fill from YOUR analysis of YOUR firmware.

The toolkit ships `profiles/template.json` with every value empty (null / [] / {}): no address, table or name of any camera is in this
repository.  Copy it next to your decoded image, fill it, and keep it out of git (it is your analysis data).

    {
      "name":   "my-body-v1.0",
      "isa":    "mn10300",
      "image":  {"base": "0x...", "read_floor": null, "file": "path/to/decoded-main-code.bin"},
      "ret_kind": {"0x...": "retf"},                  # stubbed callees OUTSIDE the image: how the real code returns (ret | retf)
      "disassembler": {"command": [...], "line_regex": null, "cache_dir": ".omh_cache/dis"},
      "stubs":  {"note": "free-form, your own bookkeeping; the toolkit never reads it"}
    }

  image.base        address of image byte 0 (required for anything that executes)
  image.read_floor  lowest address at which image bytes read as data (default = base)
  image.file        the decoded image on disk (relative paths are relative to the profile file)
  ret_kind          see emu.pyemu (hex-string keys are accepted)
  disassembler      see emu.disasm.ExternalDisassembler: command with {file} {wslfile} {base} {start} {end}; line_regex needs the named
                    groups addr, mn, ops, bytes (null = the default "addr  mnemonic ops ; bytes" layout)

load(path) never guesses: unknown keys are an error (a typo must not silently fall back to a default), and apply() lists every required field
that is still empty.
"""
import json
import os

from . import disasm, pyemu

KNOWN = {
    "name": str, "isa": str, "image": dict, "ret_kind": dict, "disassembler": dict, "stubs": dict,
}
KNOWN_IMAGE = {"base", "read_floor", "file"}
KNOWN_DIS = {"command", "line_regex", "cache_dir"}


class ProfileError(ValueError):
    pass


def _int(v, what):
    if v is None:
        return None
    try:
        return int(v, 0) if isinstance(v, str) else int(v)
    except (TypeError, ValueError):
        raise ProfileError("%s: %r is not an integer" % (what, v)) from None


class Profile:
    def __init__(self, raw, path=None):
        self.raw, self.path = raw, path
        for k in raw:
            if k not in KNOWN and not k.startswith("_"):
                raise ProfileError("unknown profile key %r (known: %s)" % (k, ", ".join(sorted(KNOWN))))
        for k, t in KNOWN.items():
            if k in raw and not isinstance(raw[k], t):
                raise ProfileError("profile key %r must be a %s" % (k, t.__name__))
        img = raw.get("image", {})
        bad = set(img) - KNOWN_IMAGE
        if bad:
            raise ProfileError("unknown image key(s) %s (known: %s)" % (sorted(bad), sorted(KNOWN_IMAGE)))
        dis = raw.get("disassembler", {})
        bad = set(dis) - KNOWN_DIS
        if bad:
            raise ProfileError("unknown disassembler key(s) %s (known: %s)" % (sorted(bad), sorted(KNOWN_DIS)))
        self.name = raw.get("name") or ""
        self.isa = raw.get("isa") or "mn10300"
        if self.isa != "mn10300":
            raise ProfileError("isa %r: only mn10300 is implemented (the lane / difftest / method are ISA-independent, the core is not)" % self.isa)
        self.base = _int(img.get("base"), "image.base")
        self.read_floor = _int(img.get("read_floor"), "image.read_floor")
        f = img.get("file")
        self.image_file = None if not f else (f if os.path.isabs(f) or not path else os.path.join(os.path.dirname(os.path.abspath(path)), f))
        self.ret_kind = {_int(k, "ret_kind key"): v for k, v in raw.get("ret_kind", {}).items()}
        for k, v in self.ret_kind.items():
            if v not in ("ret", "retf") and not (isinstance(v, list) and len(v) == 3):
                raise ProfileError("ret_kind[%#x] = %r: expected 'ret', 'retf' or [kind, regs, imm8]" % (k, v))
        self.dis_command = dis.get("command") or []
        self.dis_regex = dis.get("line_regex") or disasm.DEFAULT_LINE
        self.dis_cache = dis.get("cache_dir")

    def missing(self, need_image_file=False, need_disassembler=False):
        miss = []
        if self.base is None:
            miss.append("image.base")
        if need_image_file and not self.image_file:
            miss.append("image.file")
        if need_disassembler and not self.dis_command:
            miss.append("disassembler.command")
        return miss

    def disassembler(self):
        if not self.dis_command:
            raise ProfileError("profile has no disassembler.command: either fill it or pass your own disassembler to Image(...)")
        return disasm.ExternalDisassembler(self.dis_command, self.dis_regex, self.dis_cache)

    def apply(self, need_image_file=False, need_disassembler=False):
        """configure emu.pyemu (and with it the Rust core / PageMem if OMH_RUST / OMH_MEM ask for them); returns self"""
        miss = self.missing(need_image_file, need_disassembler)
        if miss:
            raise ProfileError("profile %s: fill %s before use (see profiles/template.json)" % (self.path or self.name or "<in memory>", ", ".join(miss)))
        pyemu.configure(self.base, self.read_floor, self.ret_kind)
        return self

    def open_image(self, patches=None, shared=False):
        """pyemu.Image of image.file with the profile's external disassembler"""
        self.apply(need_image_file=True, need_disassembler=True)
        return pyemu.Image.from_file(self.image_file, patches, self.disassembler(), shared=shared)


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        try:
            raw = json.load(f)
        except ValueError as e:
            raise ProfileError("%s: not valid JSON (%s)" % (path, e)) from None
    if not isinstance(raw, dict):
        raise ProfileError("%s: the top level must be an object" % path)
    return Profile(raw, path)


def from_env():
    """the profile named by $OMH_PROFILE, or None"""
    p = os.environ.get("OMH_PROFILE")
    return load(p) if p else None
