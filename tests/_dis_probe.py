"""subprocess probe for test_rust.DisShortcut: python _dis_probe.py reg|noreg|edited  (run with OMH_RUST=1).  Prints one JSON line."""
import json
import os
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ofh.emu import asm, pyemu as E          # noqa: E402

mode = sys.argv[1]
BASE, WIN = 0x400000, 0x900000
E.configure(BASE)
from ofh.emu import rs_shim                  # noqa: E402

SRC = """def dis(self, va):
    if not (WIN_LO <= va < WIN_LO + 0x1000):
        return E.Image.dis(self, va)
    if va not in self.wcache:
        for a, mn, ops, sz in self.window.fetch(self, va):
            self.wcache.setdefault(a, (mn, ops, sz))
    return self.wcache[va]"""
main = asm.assemble("start:\n mov 200,d1\n mov 0,d0\n jmp 0x%x\nback:\n nop\n" % WIN, BASE)
window = asm.assemble("w:\n add d1,d0\n sub 1,d1\n bne w\n jmp 0x%x\n" % main.labels["back"], WIN)
text = SRC.replace("0x1000", "0x1001") if mode == "edited" else SRC          # 'edited': same behaviour here, different code
g = {"E": E, "WIN_LO": WIN, "__name__": "winmod"}
exec("class Img(E.Image):\n" + textwrap.indent(text, "    ") + "\n", g)
if mode in ("reg", "edited"):
    rs_shim.register_reviewed_dis("winmod", "Img.dis", SRC, lo=("glob", "WIN_LO"), size=0x1000, side_attr="wcache")
img = g["Img"](bytes(0x100), disasm=main.disassembler())
img.window, img.wcache = window.disassembler(), {}
cpu = E.CPU(img)
cpu.sp = 0x10010000
cpu.run(main.labels["start"], {main.labels["back"]})
st = rs_shim._R.stats()
print(json.dumps(dict(d0=cpu.d[0], steps=cpu.steps, native=st["native"], dis_call=st["dis_call"], fb_route=st["fb_route"], py_steps=st["py_steps"])))
