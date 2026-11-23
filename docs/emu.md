# emu: the interpreter and its Rust core

## pyemu (the reference)

`emu/pyemu.py` is a mnemonic-level MN10300/AM33 interpreter: it asks `Image.dis(va)` for `(mnemonic, [operands], size)` and executes it.
The decode step is separate (`emu/disasm.py`), so the core knows nothing about a particular disassembler or body.

```python
from emu import pyemu as E, profile
prof = profile.load("my-body.json").apply()          # configures base, read floor, ret_kind (and the Rust core if OMH_RUST is set)
img = prof.open_image(patches={0x...: b"\x..."})    # image bytes + patches + your disassembler
cpu = E.CPU(img)
cpu.sp = ...                                         # you set up the world: stack, registers, memory (cpu.load(addr, bytes))
cpu.stubs[addr] = E.stub_ret(lambda c: 0, ['d2', 'd3'], 8)
cpu.run(entry, {stop_address})
print(cpu.d, cpu.writes)                             # every store is recorded: the write audit
```

What it implements: `mov/movbu/movhu`, `clr`, `ext*`, `add/sub/cmp` with full z/n/c/v, `and/or/xor/not`, shifts, `inc/inc4`, multiply
(signed/unsigned, extension `udf*`), `div/divu`, `btst`, `bset/bclr`, signed and unsigned conditional branches, `setlb` + `Lcc` loops,
`jmp`, `call` / `calls` / `ret` / `retf` / `rets` with the real register-list and imm8 conventions, `movm`. Indexed `(di,an)`, absolute
`(0x...)`, register-indirect and displacement addressing. It does **not** implement the whole ISA, MMIO, interrupts, caches or timing;
unknown mnemonics raise `NotImplementedError` with the address. Misaligned accesses by emitted code raise `AlignFault` when `OMH_ALIGN=1`.

Memory: unknown bytes read as 0; image bytes at or above `image.read_floor` read as data; everything else is what you stored.

### Where instructions come from (`emu/disasm.py`)

* `ListingDisassembler({addr: (mn, ops, size)})`: a prepared map (the examples and tests use it with `emu/asm.py`).
* `ExternalDisassembler(command, line_regex, cache_dir)`: runs *your* tool once per 0x400-byte window and parses its text output with a
  regex with the named groups `addr`, `mn`, `ops`, `bytes`. The default regex matches `addr  mnemonic operands ; hexbytes`. Results are
  cached on disk keyed by the address and the sha1 of the bytes the tool saw. GNU binutils (the `mn10300` target, GPL) can be that tool;
  it is only ever run as an external program, never bundled or linked.

### Configuration (`profile.json`, `docs/profile.md`)

`pyemu.configure(base, img_lo=None, ret_kind=None)`: image base (required), read floor, and `ret_kind` for stubs *outside* the image. The
Rust core reads the same values; changing them after the first CPU is refused.

## The Rust core (`emu/rs_emu`, PyO3)

Same semantics, native speed. It operates on the **same Python CPU object** (registers stay Python lists, `writes` / `trace` stay Python
lists) so every caller, subclass and stub sees what it always saw. The engine keeps the state in Rust between Python callbacks and writes it
back before every call into Python and on every exit. **Exactness rules**: an operand or instruction form the decoder cannot mirror
exactly, a stub address, a non-u32 register or pc, an overridden helper (`get/put/ea/reg/flags_*/cond/_chk`), an unknown `Image.dis` ->
that step is executed by the Python `_step` (decided before any side effect). Memory: `RPageMem`, the Rust port of `mem/pagemem.PageMem`.

Build (Rust toolchain pinned by `emu/rs_emu/rust-toolchain.toml`, crates pinned by `Cargo.lock`, built `--locked`):

```
python -m emu.build            # cargo build --release --locked --features extension-module; copies emu/_rs_emu<ext>
cd emu/rs_emu && cargo test    # unit tests of the operand decoder (no Python module needed)
```

Use: set `OMH_RUST=1` (or `dual`, see `docs/difftest.md`) in the environment of any process that calls `pyemu.configure()`.

| Variable | Meaning |
|---|---|
| `OMH_RUST` | unset / `0` pure Python; `1` Rust core; `dual` lock-step against Python. Anything else: exit 86. |
| `OMH_MEM` | `page`: `PageMem` fast paths (sparse 256-byte pages, exact dict semantics) |
| `OMH_ALIGN` | `1`: alignment trap on for emitted code (the lane sets it) |
| `OMH_RS_STAT`, `OMH_RS_STAT_DIR` | write a per-process marker with the engine counters (the lane sets them with `--rust 1`) |

### Loud-failure rules (W1-W6)

W1 a bad `OMH_RUST` value is fatal; W2 a missing or stale build is fatal (API + source hash checked at import); W3 a failed patch is
fatal; W4 fatal means `os._exit(86)`, which no `except`/`atexit`/`finally` can swallow; W5 the markers let a driver prove afterwards that
the core ran (`lane.cmp_runs --rs`: `native > 0` for every emulating stage); W6 a stage that cannot run native by design must say so in the
lane spec (`"rust": false` + reason), it is recorded in `rs_off.json` and shown by the comparison.

### Source hash handshake

`emu/rs_srchash.py`: sha256 over `src/*.rs`, `Cargo.toml`, `Cargo.lock`, `rust-toolchain.toml` (relative path, length, bytes). `emu.build`
passes it to cargo as `RS_SRC_SHA`; it is compiled into `_rs_emu.SRC_SHA`; `emu.rs_shim` recomputes and compares at import.
`emu/_rs_emu.build.json` records the module sha1, `rustc -Vv`, Python and time of the build.
