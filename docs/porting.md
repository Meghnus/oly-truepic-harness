# Want to port to another body?

Porting means: bring a mod (colour engine tweaks, menu changes, a UI page) from the body it was built on to yours. The hard part is not
the code, it is *knowing it is right before it touches a camera*. This toolkit is the second half of that: fast, repeatable, negative-controlled
checks on your PC.

1. **Check the CPU** (`docs/COMPATIBILITY.md`, "Check a body in five minutes"). MN10300 main code: `ofh/emu/` applies. Otherwise only the lane,
   difftest scaffolding and the method (`docs/method.md`) apply.
2. **Get and decode your own firmware.** Download it from the vendor, decode the container with fishcu's tools
   (https://github.com/fishcu/oly_fw_analysis, `docs/format/CONTAINERS.md`) or any decoder you trust. Keep everything local.
3. **Fill a profile** (`docs/profile.md`): the load base of the main code, a disassembler command (or your own listing), `ret_kind` for
   out-of-image stubs.
4. **Run the examples** against nothing (`python examples/01_loop.py ...`) and then write a 10-line script that loads *your* image through the
   profile, sets up registers and calls one small function you understand (a getter, a table lookup). Compare with what the real code
   obviously does.
5. **Write your first negative-controlled check** (`examples/03_negative_control.py`, `ofh/lane/stage.py`): the check on your patch, and the same
   check on a mutant that must fail, judged by value.
6. **Scale up** with the lane (`docs/lane.md`): split your checks into stages, set `est_mb`, run `python -m ofh.lane.driver ...`. Turn on the
   Rust core (`python -m ofh.emu.build`, `--rust 1`) once the Python run is green and prove equivalence with `ofh.lane.ab` and `OMH_RUST=dual`.
7. **Before you share anything** run `tools/leakcheck.py --stock your.BIN`.

Coordination: open an **issue or PR** on this repository, or ask on the **Caduceus Discord**. Contributions are welcome: more mnemonics
(with a fuzz case in `ofh/difftest/cpufuzz.py`), disassembler line formats for other tools (a regex + a test), compatibility rows with sources, and
porting notes that contain no firmware data. Please do not post firmware, decodes or addresses-as-data in issues.
