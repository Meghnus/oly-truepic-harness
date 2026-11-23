# Performance: what was kept, what was dropped, what was measured

The internal harness this toolkit was extracted from had several accelerations. This page says exactly which ones are in the public toolkit.

| Acceleration (internal) | In this toolkit | Notes |
|---|---|---|
| Rust core for the interpreter (`CPU.step/_step/run/ld/st/load`, PyO3) | **kept** (`ofh/emu/rs_emu`) | same exactness rules, source-hash handshake, loud failures; differentially fuzzed against `pyemu` |
| `PageMem` (sparse 256-byte pages, exact `dict` semantics, copy-on-write copies) and its Rust port `RPageMem` | **kept** (`ofh/mem/pagemem.py`, Rust `MemCore`) | opt-in for the Python core (`OMH_MEM=page`), automatic with the Rust core |
| Shared read-only / copy-on-write image store (`shm`: content-addressed store, `ROBytes`, `CowBytes`) | **kept** (`ofh/mem/shm.py`) | explicit API: `shm.shared_copy`, `Image.from_file(..., shared=True)` |
| Disassembly window cache shared between processes | **kept**, in a safer form | JSON files keyed by address and the sha1 of the bytes the tool saw (was pickles) |
| Reviewed `Image.dis()` shortcut (Rust handles a range-split dis override without calling Python) | **restored in a generic, opt-in form** | `rs_shim.register_reviewed_dis(...)`: only for a function you registered whose compiled code equals the reviewed source; tested (`tests/test_rust.py::DisShortcut`). No firmware names or paths |
| Automatic `open()` hook + periodic gc sweep that swapped big `bytes`/`bytearray` values for shared maps | **dropped** | it monkey-patches `builtins.open` and rewrites live objects: too invasive for a public tool. You call the explicit API where you load an image. Scripts that `open(f,'rb').read()` a big file get no automatic sharing |
| Precedent index of the internal assembler (a mapped table of hundreds of thousands of entries) | **dropped** | it belonged to an assembler that is not part of this toolkit |
| Profiling / statistics scripts of the internal harness | **dropped** | the engine counters (`_rs_emu.stats()`, per-process markers with `OMH_RS_STAT=1`) are kept |

## Measured (`examples/bench/bench.py`, firmware-free)

Windows 11, Python 3.14.2, rustc 1.95.0, one idle machine, one run (not a statistical study; run it yourself).
Workload: a byte-increment loop over 1 MB (`movbu (a0),d0 ; add 1,d0 ; movbu d0,(a0) ; inc a0 ; sub 1,d1 ; bne`), 6,291,458 steps by `run()`, 300,000 steps
by a Python loop of `step()` calls.

| config | `run()` s | steps/s | speed-up | `step()` s | steps/s | speed-up | peak MB |
|---|---|---|---|---|---|---|---|
| py-dict (stock memory) | 13.47 | 467,018 | 1.0x | 0.62 | 483,555 | 1.0x | 250 |
| py-page (`PageMem`) | 15.64 | 402,140 | 0.9x | 0.73 | 408,731 | 0.8x | 205 |
| rust (Rust core + Rust `PageMem`) | 0.97 | 6,490,945 | **13.9x** | 0.35 | 853,011 | **1.8x** | 188 |

Shm store: 4 processes each holding the same 64 MB image and patching 40 bytes of it:

| loading | sum of private memory | private + shared pages once |
|---|---|---|
| plain `read()` | 303 MB | 303 MB |
| `shm.shared_copy` (copy-on-write mapping) | 46 MB | 110 MB (**2.7x less**, 192 MB saved) |

How to read this honestly:
* The Rust core is fast when **steps stay native**. `run()` keeps the whole loop in Rust (13.9x here). Calling `step()` one at a time from Python is
  limited by the Python call itself (1.8x): single-stepping harnesses gain little.
* On real batteries the gain depends on how many steps are routed to the Python `_step`: stubs, unknown operand forms, overridden helpers and a `dis()`
  override that is not registered all go through Python (the engine counters tell you: `native`, `fb_stub`, `fb_route`, `py_steps`). The random-program
  fuzz in `examples/04_random_programs.py` routes about a third of its steps to Python on purpose. Our own real-battery wall-clock comparison is not
  published because the only measurement we have was taken while another heavy job was running; **measure your own on an idle machine** with `lane.ab`.
* `PageMem` on the **Python** core costs about 10-20% speed in exchange for memory (205 vs 250 MB for 1 MB of touched data here; the gap grows with the
  touched bytes: a plain dict spends roughly 100+ bytes per byte).
* The shm saving is the per-process copy of the image. It grows with the number of processes and the image size; a process still pays for the pages it
  writes (one page for a 40-byte patch).
* We have not compared against other public emulators (for example om-emu, a JavaScript lab with a different scope: device models and a full UI). A
  fair comparison needs the same workload on both, which this repository cannot supply without firmware.
