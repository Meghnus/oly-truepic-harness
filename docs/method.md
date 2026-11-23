# The method

Tools do not make a firmware mod safe; a method does. This is the one this toolkit was built to support. Each rule exists because
skipping it once produced a result that looked fine and was wrong.

Nothing here is camera-specific. Examples use synthetic code (`examples/`).

## 1. Negative controls, judged by value

A check that cannot fail proves nothing. For every check that says PASS, run the same check on a deliberately broken input (a
**mutant**: the patch with one byte changed, the previous build, the stock image where the stock image must fail) and require it to FAIL
there. Judge the control **by value**: "the mutant returned 250, the original 100", not "the run printed FAIL somewhere".

* Keep the control in the same stage as the check (`ctx.neg(...)` in `lane/stage.py`), named `NEG ...`, so a run table shows which controls ran
  and a missing control is visible.
* A control must differ from the check by *one thing*. If the mutant also changes the harness, the verdict means nothing.
* A control that is not detected is the most valuable result you can get: it says your check is blind to that class of error.
  `examples/03_negative_control.py` and `examples/stages/s_loop.py` show the shape.
* Also run the check against the *true stock input* when a "new/changed" expectation is claimed: the check must say "not new" there.

## 2. Real-path emulation over stubs

A stub is a promise that the service you replaced behaves the way you assumed. Every field failure we hit lived in a stubbed service
or in a path nobody emulated.

* Emulate the **real code path** end to end where you can: the function that selects the page, builds the list, opens the dialog,
  writes the setting. Stub only hardware and services whose effect you can state in one line.
* A stub must return the way the real callee returns: its register list, its stack adjustment, MDR or the stack slot. `pyemu` takes the exit
  from the real callee's code (and asserts the frame matches); a stub outside the image must declare its return (`ret_kind` in the profile).
  This kills the class of bug where a stubbed call silently changes the caller's stack or callee-saved registers.
* Write down each stub and what it hides ("returns success; hides any failure of the service"). A stub list is part of the evidence.
* "It worked in the emulator" is only as strong as the weakest stub on the path. Say which stubs were on the path.

## 3. Dual audit: the fast core must never be trusted on its own say-so

The Rust core is only useful if it is *exactly* the Python core, just faster. Establish that with three independent layers, in this order:

1. **Differential fuzz** (`difftest/cpufuzz.py`, `difftest/memfuzz.py`): random programs and random memory operations, compared after
   every step including exception type and message, value *and type* of every register, flags, write audit, trace, memory order.
2. **Lock-step on your real worlds** (`OMH_RUST=dual`, `difftest/dual.py`): every native step or run segment of your real battery is
   re-run by the Python core from the same start state and compared (registers, flags, writes, memory delta, exception). A mismatch is
   counted and fails the run (`lane.cmp_runs --dual`), or raises at once with `OMH_DUAL_FAIL=1`.
3. **A/B of the whole battery** (`lane.ab`): the same battery with the Rust core and with pure Python; `lane.cmp_runs` must report 0
   differences after token normalisation, and every stage that emulates must carry a marker proving the native core executed
   (`native > 0`).

Why three: fuzz finds what you did not think of, dual covers your actual code, A/B covers the harness around it. An A/B where both
sides secretly ran Python agrees perfectly and proves nothing; that is why the markers and the loud-failure rules exist.

## 4. The harness fails loudly or not at all

* A run that **requested** the fast core and did not get it (bad value, missing build, stale build, patch failure) must **end with exit
  86**, not fall back (`emu/loud.py`). A driver treats 86 as FAILED, never as skipped.
* **A stale build must not run**: the sources are hashed (`emu/rs_srchash.py`), the hash is compiled into the module, and the shim
  recomputes it at import. Edit a `.rs` file and forget to rebuild: import fails.
* A stage that checks nothing is FAIL. A stage whose process exits non-zero is FAIL even if its rows say PASS.
* An unknown mnemonic raises `NotImplementedError` with the address; an unknown address raises `KeyError`; an unconfigured core refuses to
  start. No operand form is guessed. In the Rust core an operand form it cannot mirror *exactly* is handed to the Python `_step`
  *before* any side effect.
* Unknown profile keys are errors (a typo must not silently mean "default").

## 5. Parallel runs need isolation, and proof of it

* One process per stage, one BLAS/OpenMP thread, its own scratch dir (`OMH_SCRATCH`), a name prefix for temp files (`OMH_PREFIX`).
* Scripts you did not write rewrite shared output files next to themselves. Redirect them (`scratch` in the lane spec) instead of
  editing the scripts; then **prove** with the I/O audit that no file was written by one stage and touched by another.
* Cap by memory, not just by process count: the RAM gate starts a stage only if free memory stays above a floor and a buffer after every
  running stage has grown to its estimate. Calibrate the estimates from the "peak MB" column once.
* Do not time a run while another heavy job runs on the machine: the number is meaningless (we had a "slower" Rust run that was just
  another build running next to it).

## 6. Lessons learned (each one cost a day)

* **Free space may be a runtime buffer.** A region that is erased (all `0xff`/`0x00`) in the flash image can be RAM at runtime, filled
  by a painter or loader that runs later. "It is empty in the image" is not evidence. Prove a region free by tracing writes and reads to it
  in the real path (including late initialisation), or do not use it.
* **Stubbed services hide field bugs.** The stub returned success; the real service refused. The emulator passed, the camera did not. See
  rule 2.
* **An id or index you inferred is not an id you verified.** Page ids, catalog keys, string ids: when two namespaces look alike (a page
  number versus a catalog key), a patch that uses one where the other is expected "works" in the lab if the lab never opens the page.
  Emulate the open path, and check the new value against *every* stock pairing, not the one you looked at.
* **"Provably unused" needs a census.** Count references to an id, a string, a table row across the whole image (data and code, direct and
  computed) before reusing it, and re-count after your own patch. A check that "no one references this" must itself be negative-controlled
  (does it find a reference you plant?).
* **Alignment.** The ISA requires natural alignment; a permissive emulator accepts misaligned accesses that the hardware rejects. Run with
  `OMH_ALIGN=1` (the lane sets it) so a misaligned access by emitted code raises `AlignFault`.
* **An expectation that you edit to make a run pass is a bug.** When a census count changes, find out *why* and change the expectation
  in the same commit that explains it, with the new value derived, not copied from the failing output.
* **Normalise only what may differ.** `lane.cmp_runs` removes wall-clock numbers, temp names and run-dir paths and nothing else; every
  pattern is printed with `--show-norm`. Each new pattern is a place a real difference can hide: add them reluctantly.
* **Memory diet is optional, correctness is not.** `PageMem` and the shared store (`mem/`) exist to fit N stages in RAM. They are
  tested to behave like a `dict` (fuzzed against it), and they are opt-in (`OMH_MEM=page`, `Image.from_file(..., shared=True)`).
* **Never publish what you decoded.** Decoded images, dumps, disassembly, caches (`.omh_cache/`), patch lists and per-body tables are
  derived from the vendor's firmware. Run `tools/leakcheck.py --stock YOUR.BIN` before every push (`docs/licence-and-leaks.md`).
