# oly-truepic-harness

**Why this exists.** (1) To make firmware-mod testing on a PC much faster and much more trustworthy, so that a mod is checked, with
negative controls, long before anything touches a camera, and so that this works across Olympus / OM System bodies, not just one.
(2) To speed up porting: if you want to bring mods (the colour engine, menus, UI pages, ...) to another body, this gives you the emulator
core, the parallel test lane and the method to verify your port. Contributions and ports are welcome.

**Scope.** Designed to work across TruePic bodies that use the MN10300/AM33 core family; each body needs its own profile (addresses and tables
differ per body and per firmware version). The lane, difftest and method apply to any body, including newer ARM-core bodies. See
[docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) for what is known per body.

**Zero firmware is in this repository.** You run everything on your own, locally downloaded firmware; see
[licence and leaks](docs/licence-and-leaks.md). **Caduceus developers: start at [docs/FOR-CADUCEUS.md](docs/FOR-CADUCEUS.md).**

Layout: everything importable lives under one package, `ofh` (`ofh.emu`, `ofh.mem`, `ofh.lane`, `ofh.difftest`), so nothing collides with other
Python packages. Run from the repository root (the examples and tests add it to `sys.path` themselves); there is no pip package yet.

## Origin and related projects

This toolkit comes from [om10mi-mods](https://github.com/Meghnus/om10mi-mods), the firmware mod for the OM-D E-M10 Mark I. Many people asked for
ports to other OM / Olympus cameras, so these are the tools we use ourselves: please use them for your own ports. The LUT / `.cube` / `.xmp`
import (a web app and a builder, in development) will be linked from both projects when it is published.

This repository: https://github.com/Meghnus/oly-truepic-harness

## What is in it

| Directory | What |
|---|---|
| `ofh/emu/` | `pyemu.py`: a mnemonic-level **MN10300/AM33 interpreter** (the reference). `rs_emu/`: the same in **Rust** (PyO3), with a source-hash handshake and loud-failure rules. `profile.py` + `profiles/template.json`: the **empty** profile you fill from your own analysis. `disasm.py`: plug in *your* disassembler. `asm.py`: a tiny assembler so examples need no firmware. |
| `ofh/mem/` | `PageMem` (sparse memory with exact `dict` semantics, copy-on-write copies) and `shm` (a content-addressed store with read-only and copy-on-write `mmap` images shared between processes). |
| `ofh/lane/` | A **parallel stage driver** with a **RAM gate**, per-stage scratch, an I/O audit, `cmp_runs` (token-normalised run comparison) and an **A/B** of the Rust core against Python. |
| `ofh/difftest/` | **Lock-step dual stepper** (with dict-delta compare), run()-cut fuzz and random-instruction fuzz, memory and undo-journal fuzz. |
| `docs/` | [method](docs/method.md), [emu](docs/emu.md), [lane](docs/lane.md), [difftest](docs/difftest.md), [profile](docs/profile.md), [porting](docs/porting.md), [performance](docs/performance.md), [compatibility](docs/COMPATIBILITY.md), [licence and leaks](docs/licence-and-leaks.md). |
| `examples/` | Six runnable examples with **no firmware** (synthetic snippets and random programs). |
| `tests/`, `ofh/emu/rs_emu` | `python -m unittest discover -s tests` and `cargo test`; both pass with no firmware present. |
| `tools/leakcheck.py` | The pre-push guard: forbidden files, blobs, address-like literals, and (`--stock FILE`) any 16-byte run shared with *your* stock image. |

## Which cameras?

Details, sources and caveats: **[docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)**. `confirmed` = the CPU of that body's firmware was checked by
us or by the cited public project (not necessarily re-verified by us; "reset-head" = start-up sequence only); `likely` = same TruePic
generation as a confirmed body per a cited source, not verified; `unknown` = nothing citable. The emulator needs MN10300/AM33 code; the lane,
the difftest method and the verification method apply to any firmware.

| Body | Label | Note |
|---|---|---|
| OM-D E-M10 Mark I | **confirmed** (ours) | developed and run on this body |
| OM-D E-M10 Mark II, E-M5 Mark II | **confirmed** (ours static for the Mark II; public emulator ports run both) | same family, different layout and load address |
| PEN-F | **confirmed** (fishcu/oly_fw_analysis: MN103 emulator on PEN-F code) | the deepest public prior art |
| E-M1, E-PL7, E-PL8, E-P5, E-PL5/6, E-PM2 | **confirmed, reset-head only** (om-emu survey) | surveyed, not tried by anyone as far as we know |
| E-M10 III / IV, E-PL9, E-P7, E-M1 II / III / X, E-M5 III | **confirmed** (om-emu ports; E-P7 also Caduceus) | newer, multi-core generation: other cores may run other code |
| OM-5 / OM-5 II, OM-1 / OM-1 II, OM-3 | **confirmed, main core only** (om-emu; OM-3 per a Caduceus-community note) | the OM-1 family also has an **ARM Cortex-A53** communications core: not MN10300 |
| E-M5 (I), E-P3, E-PL3, E-PM1 and other TruePic VI bodies not surveyed | **likely** | TruePic described as MN103-based in general |
| Older DSLRs / compacts, anything else | **unknown** | check the reset head first |

A different ISA (for example ARM) means `ofh/emu/` does not apply, but `ofh/lane/`, the difftest approach and `docs/method.md` still do.

## Quick start (no firmware needed)

```
python -m unittest discover -s tests            # ~1 minute; Rust tests are skipped (and named) if the core is not built
python examples/01_loop.py                      # 02_call_stub, 03_negative_control: the core, stubs and the method
python examples/05_lane_demo.py                 # the lane on three synthetic stages: parallel, RAM gate, io audit, summary table
python -m ofh.emu.build                             # optional: the Rust core (cargo, network on first build for the pinned crates)
OMH_RUST=1 python examples/01_loop.py           # the same example on the Rust core
python examples/04_random_programs.py           # random programs in lock-step Rust vs Python
python examples/06_dual_audit.py                # OMH_RUST=dual: every native step compared with Python
python examples/bench/bench.py                  # firmware-free benchmark: Python vs Rust core (run() and step()), PageMem, shm store memory
python examples/05_lane_demo.py --ab            # Rust vs Python A/B of a battery + run comparison
(cd ofh/emu/rs_emu && cargo test)                   # Rust unit tests
```

## Quick start on your own firmware

1. Decode your firmware locally with your own decoder, or with fishcu's [oly_fw_analysis](https://github.com/fishcu/oly_fw_analysis) (the container
   format and checksum are published there; this toolkit does not depend on it). Keep the result outside git.
2. `cp profiles/template.json profiles/my-body.json` and fill `image.base` (load address of the main code), `image.file`, and either
   `disassembler.command` (+ `line_regex`) or build a `ListingDisassembler` yourself. See [docs/profile.md](docs/profile.md).
3. ```python
   from ofh.emu import profile, pyemu as E
   prof = profile.load("profiles/my-body.json").apply()
   cpu = E.CPU(prof.open_image())
   cpu.sp = ...; cpu.d[0] = ...                  # build the world your function expects
   cpu.run(entry, {stop})                        # then look at cpu.d, cpu.writes
   ```
4. Turn the check into a lane stage with a negative control ([docs/method.md](docs/method.md), `ofh/lane/stage.py`), run
   `python -m ofh.lane.driver spec.json --run runs/1 --pool 4`.
5. `python tools/leakcheck.py --stock your.BIN` before you share anything.

## The method in five lines

Negative controls judged **by value** (the check must fail on a mutant). **Real-path emulation over stubs** (say which stubs were on the path).
**Dual audit** of the fast core (fuzz, lock-step on your real worlds, A/B of the whole battery, with proof the native core ran). The harness
**fails loudly** (exit 86, stale builds refused) instead of falling back. Parallel runs are **isolated and the isolation is proven**.
Lessons learned (e.g. "free space may be a runtime buffer", "stubbed services hide field bugs"): [docs/method.md](docs/method.md).

## Want to port to another body?

1. Check the CPU of your body's main code ([compatibility](docs/COMPATIBILITY.md#check-a-body-in-five-minutes-you-need-your-own-copy-of-its-firmware)).
2. Decode **your own** firmware with your own decoder, or with fishcu's oly_fw_analysis; keep it local.
3. Fill the profile from your own analysis.
4. Run the examples, then load your image and call one small function you understand.
5. Write your first **negative-controlled** check; scale up with the lane; prove the fast core with `ofh.lane.ab` / `OMH_RUST=dual`.

Coordinate through **issues / PRs here** or the **Caduceus Discord** (invite in the Caduceus project READMEs). More in
[docs/porting.md](docs/porting.md). Please never post firmware, decodes or addresses-as-data.

## Licence and credits

Apache-2.0 ([LICENSE](LICENSE), [NOTICE](NOTICE)); copyright 2026 Meghnus, contributors keep the copyright of their contributions under
Apache-2.0. Credits for facts and ideas (no code copied):
* **fishcu / oly_fw_analysis** (Apache-2.0): published facts about the update container and checksum, and MN103 instruction-level analysis of
  PEN-F firmware with a native-call harness. We depend on none of its tools or code; the model for keeping firmware local.
* **cmdada** (em10-fw and related E-M10 work): facts about the E-M10 Mark I firmware (facts only).
* **flatpanda / om-emu** (MIT): an independent JavaScript emulator lab that runs MN10300 firmware of many bodies (CPU interpreter, modelled
  devices, the firmware's own kernel and GUI, patch overlays) and a 32-file firmware survey; we cite its survey and per-body status in
  [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) (facts only, none of its code is used).
* GNU binutils is only ever an optional external tool.

Unofficial, unaffiliated with OM Digital Solutions; "Olympus", "OM System", "OM-D", "PEN" and "TruePic" are trademarks of their owners. Do not
flash anything because an emulator said so: an emulator passing is evidence, not proof.
