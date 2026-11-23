# difftest: how the fast core is held to the reference

| Module | What it tests | Run |
|---|---|---|
| `difftest/cpufuzz.py` `fuzz_lockstep` | Random programs (every mnemonic, every operand form incl. malformed, stubs with real exits, odd register types, alignment on/off, subclasses with overridden `ld`/`st`/helpers, plain-dict memory, `run()`): complete state compared after **every step** | `python -m difftest.cpufuzz [n] [seed]` |
| `difftest/cpufuzz.py` `fuzz_runcut` | The Rust side is run in segments cut at random pcs (`run` to a stop, `step` over it, `run` again) against ONE uninterrupted Python run: the native state cache across every Python re-entry | same |
| `difftest/memfuzz.py` `fuzz_mem` | `PageMem` vs `RPageMem` vs `dict`: after every op results, exceptions, ordered items/keys/values; odd keys and values; COW copies; pickle/deepcopy | `python -m difftest.memfuzz` |
| `difftest/memfuzz.py` `fuzz_journal` | The undo journal `dual` depends on: batches between `_jstart` and `_jrestore` leave memory unchanged; equal batches give equal `_jdelta` | same |
| `difftest/dual.py` | **Lock-step dual stepper for real worlds**: `OMH_RUST=dual`. Every native `step` / stub-free `run` segment is compared with the Python reference started from the same state | any process with `OMH_RUST=dual` |

## dual mode

The native `step`/`run` stay installed as under `OMH_RUST=1`; each call is wrapped:

* `run(start, stop, max)` is split at stub addresses; every stub-free stretch is ONE native `run` compared with the Python `run`;
* memory **RPageMem**: Rust first, the undo journal restores memory, then the Python reference runs; the world continues on the **Python**
  result;
* memory **exact dict** (any size): the Python reference first on a copy-on-write overlay (`_Ov`, reads fall through, writes stay in the
  overlay), state restored, then the Rust segment on the real dict; compared = every overlay key and every address in the Rust write audit
  (**dict-delta compare**: no full copies, so every step is compared); the world continues on the **Rust** result;
* compared: d/a (value + type), sp, pc, mdr, z/n/c/v, lar, steps, `_trap`, writes, trace, memory delta, result or exception type + message.

`OMH_DUAL_EVERY=k` verifies a seeded 1/k sample (default all); `OMH_DUAL_SEED`; `OMH_DUAL_FAIL=1` raises at the first mismatch. Counters in
`<OMH_DUAL_DIR>/<prefix>dual.<pid>.txt`; `lane.cmp_runs --dual` fails a run with 0 verified segments or any mismatch.

The fuzzers install the Rust core on a **subclass** of `pyemu.CPU`, so they need `OMH_RUST` unset.
