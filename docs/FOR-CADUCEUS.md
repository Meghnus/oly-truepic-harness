# For Caduceus developers

Hi! This is **olympuscam-fw-harness**: a small, firmware-free toolkit for checking Olympus / OM System firmware mods **on a PC, before a
camera is involved**, and for speeding up ports to other bodies. It grew out of the E-M10 Mark I mod work, but nothing in it is specific to
one body, and it contains **no firmware** (you use your own download, locally).

It is meant to complement what Caduceus already does (every patch is checked in an emulator of the camera's CPU before it goes on a camera),
not to replace it. Take what is useful.

## What it gives you

| Piece | In plain words |
|---|---|
| `ofh.emu` | A small MN10300/AM33 interpreter in Python (the reference) and the same in Rust (much faster). It runs a function or a patch hook on *your* decoded image with the services it calls stubbed. |
| `ofh.mem` | Memory that fits many parallel runs in RAM (sparse pages; shared read-only / copy-on-write images). |
| `ofh.lane` | Runs a whole battery of checks as parallel processes, only when RAM allows, with private scratch dirs and a proof that no two stages touched each other's files. Compares two runs (for example Rust vs Python) with only wall-clock noise normalised. |
| `ofh.difftest` | How the Rust core is held to the Python one: fuzzers, plus a lock-step mode (`OMH_RUST=dual`) that re-checks every step of *your* real runs. |
| `tools/leakcheck.py` | Run before you push anything: flags firmware-looking files, big blobs, address-like numbers, and (with your stock image) any 16-byte run shared with it. |
| [docs/method.md](method.md) | The method: negative controls by value, real-path emulation over stubs, dual audit, and the lessons learned. |

## What applies to which body

See [COMPATIBILITY.md](COMPATIBILITY.md) for the full table with sources. In short:

* **MN10300/AM33 main code** (the E-M10 family, E-M5 II, PEN-F, and, per public surveys, most bodies up to the OM-3 on their main core): the
  emulator applies. E-M10 Mark I is the only body we ran it on; E-M10 Mark II we only analysed statically; PEN-F has the deepest public prior
  art (fishcu's work). For every other body the CPU label is "confirmed by a cited project", not by us.
* **Another core or another ISA** (for example the ARM Cortex-A53 communications core of the OM-1 family): the emulator does not apply, but
  the lane, the difftest approach and the method still do.
* **Never assume addresses, hooks or patches carry over.** Every body has its own build and load address. The profile is where *your* body's
  values go.

## How to start (15 minutes, no firmware)

```
git clone <this repository> && cd olympuscam-fw-harness
python -m unittest discover -s tests          # about a minute
python examples/01_loop.py                    # the interpreter
python examples/02_call_stub.py               # a stubbed call with the real stack discipline
python examples/03_negative_control.py        # the method in 30 lines
python examples/05_lane_demo.py               # the parallel lane on synthetic stages
python -m ofh.emu.build                       # optional: the Rust core (needs cargo)
python examples/05_lane_demo.py --ab          # Rust vs Python on the same battery, then the run comparison
```

Then, with your own firmware: decode it (fishcu's tools document the container), copy `profiles/template.json`, fill in the load base and a
disassembler command, load your image and call one small function you understand. [docs/porting.md](porting.md) has the steps.

## How to contribute

* **Ports and notes:** open an issue or PR here, or talk to us on the **Caduceus Discord** (invite in the Caduceus project READMEs).
* **Code:** new mnemonics (add a case to `ofh/difftest/cpufuzz.py` so the Rust core is checked against it), disassembler line formats for other
  tools (a regex and a test), lane features, docs.
* **Facts:** corrections to [COMPATIBILITY.md](COMPATIBILITY.md) with a source link are very welcome; we would rather say "unknown" than guess.
* **House rules:** no firmware, decodes, dumps, disassembly, patch lists or addresses-as-data in issues, PRs or chat. Run
  `python tools/leakcheck.py` (and `--stock your.BIN`) before you push. Contributors keep the copyright of their contributions under
  Apache-2.0.

Thank you to the Caduceus authors and to fishcu, cmdada and flatpanda, whose public work this stands on (facts and ideas only, see NOTICE).
