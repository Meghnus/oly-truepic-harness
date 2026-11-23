# Which cameras can this help with?

Short answer: **the emulator core (`emu/`) applies to firmware whose code you care about is MN10300/AM33 machine code; the lane
(`lane/`), the differential tests (`difftest/` as a method) and the verification method (`docs/method.md`) apply to any firmware or
any other project.** This page lists what is known about which Olympus / OM System bodies are in the first group. It is deliberately
conservative: a row says what a cited source reports, and what *we* checked ourselves is marked.

No firmware bytes, addresses or tables are in this document or in this repository.

## Labels

| Label | Meaning |
|---|---|
| **confirmed** | The CPU/ISA of this body's firmware was checked by us (E-M10 Mark I, E-M10 Mark II) or by a cited public project, **named in the row**. "Depth" says how far: *reset-head* = the main code block starts with the expected MN10300/AM33 start-up sequence (a survey, no execution); *runs* = a public emulator executes selected firmware code of that body. We did **not** re-verify rows we do not mark "ours". |
| **likely** | Same TruePic generation (or product line) as a confirmed body according to a cited source, **not verified** for this body's firmware. |
| **unknown** | Nothing citable. Treat as "check first". |

"Confirmed" is about the **ISA of the main application code**. It does not mean this toolkit has been run on that body (only the
E-M10 Mark I has been), that addresses, hooks or patches carry over (they do not: every body has its own build and its own load
address), or that the hardware behind the code is modelled (MMIO, image engine, sensor, interrupts are not emulated here; you stub the
services your code calls).

## Bodies

TruePic generation is from [S4] unless stated; the code in brackets is the firmware model code used by the public projects.

| Body (code) | TruePic (source) | CPU / ISA evidence | Label | Notes for this toolkit |
|---|---|---|---|---|
| OM-D **E-M10 Mark I** (E113) | VII [S4] | Ours: the decoded main code was disassembled and helper code and patch hooks are executed on `pyemu` / the Rust core. Also [S1]: reset-head match | **confirmed (ours; runs)** | The body this toolkit was developed on. |
| OM-D **E-M10 Mark II** (E120) | VII [S4] | Ours: static analysis only (the same CPU and container on both bodies; no emulation by us). [S1] [S2]: its public emulator ports run the UI of this firmware on an MN10300 interpreter | **confirmed (ours, static; runs in [S2])** | Different load address, different code layout (about 2.4 MB larger main code than the Mark I); the method carries, addresses do not. |
| OM-D **E-M5 Mark II** (E118) | VII [S4], spec sheet [S9] | [S1] [S2]: port runs menus and shooting UI on an MN10300 interpreter; same board family as E120/PEN-F | **confirmed ([S2]; runs)** | Not run with this toolkit. |
| **PEN-F** (E122) | VII [S4] | [S3]: an MN103 instruction-level emulator and native-call harness executes original PEN-F code; deepest public analysis of any body | **confirmed ([S3]; runs)** | Closest public prior art; a profile for it would be a good first port. |
| OM-D **E-M1** (E109), PEN **E-PL7** (E115), **E-PL8** (E126), **A01** (E117) | VII [S4] | [S1]: file walked, reset head matches the AM33 start-up sequence; "surveyed, not tried" | **confirmed (reset-head only [S1])** | E-PL7/E-PL8/A01 belong to the same run-time-base family as the E-M10 Mark I per [S1], which is the best public predictor of "built like the Mark I". |
| PEN **E-P5** (E108), **E-PL5** (E100), **E-PM2** (E101), **E-PL6** (E112), Stylus 1 (E111) | VI or older (E-PL5 listed as VI [S4]; the others not listed there) | [S1]: reset head matches, "surveyed, not tried" | **confirmed (reset-head only [S1])** | Older generation: smaller code (13.5-15 MB main code in [S1]), different MMIO family from the newer bodies. |
| OM-D **E-M10 Mark III** (E130), **Mark IV** (E140), PEN **E-PL9** (E133) | VIII [S4] | [S1] [S2]: ports run menus and shooting UI on an MN10300 interpreter (main core) | **confirmed ([S2]; runs)** | TruePic VIII is described as a multi-core design [S4]: other cores may run other code, see below. |
| PEN **E-P7** (E142) | not listed in [S4] | [S1] [S2]: port runs UI on an MN10300 interpreter. The Caduceus E-P7 project [S6] says every patch is checked in "an emulator of the camera's CPU" but does not name the ISA in the pages we read | **confirmed ([S2]; runs)** | Caduceus E-P7 / OM-3 are the Discord community's projects. |
| OM-D **E-M1 Mark II** (E125), **E-M5 Mark III** (E137), **E-M1X** (E134), **E-M1 Mark III** (E139) | VIII (E-M1 II, E-M5 III, E-M1X) / IX (E-M1 III) [S4] | [S1] [S2]: ports run UI on an MN10300 interpreter | **confirmed ([S2]; runs)** | As above. |
| **OM-5** (E144), **OM-5 II** (E148), **OM-1** (E143), **OM-1 II** (E146), **OM-3** (E147) | newer than [S4] | [S1] [S2]: ports run UI; [S5]: OM-3 reported to run MN10300/AM33 with the same container and update path as the E-M10 Mark I | **confirmed ([S2], [S5]; runs)** — **main core only** | [S7]: the OM-1 main block runs **four MN10300-family cores** and a fifth processor, an **ARM Cortex-A53**, handles communications. Code on a core that is not MN10300 is out of scope for `pyemu` (see "Different ISA" below). |
| OM-D **E-M5** (original), PEN **E-P3**, **E-PL3**, **E-PM1** and other TruePic VI bodies not in [S1] | VI [S4] | none specific; [S4] describes TruePic as based on the MN103 architecture in general | **likely** | Check the reset head first (below). |
| TG-series compacts (E106, E107, E110, E121, E129, E132, E145) | n/a | [S1]: reset head matches (E121, E106, E107, E110), ports run on a few | **confirmed (survey/runs [S1] [S2])** | Different product line and sensor class; listed for completeness, not a target. |
| Older DSLRs and compacts (TruePic III, V, V+ ...), E-5 | III - V+ [S4] | [S4] says TruePic in general is MN103-based; nothing specific to firmware | **unknown** | Not in [S1]. |
| Any other body | | | **unknown** | Run the reset-head check. |

## Sensors: what the pixel size does and does not tell you

Bodies with the same sensor class (16 MP / 20 MP Four Thirds) often share the colour pipeline *concepts* (tone curves, matrices, hue
sectors) but the toolkit does **not** emulate the image engine: it runs the CPU code around it, with the engine's registers stubbed.
"Same sensor" is therefore a weak predictor. A better predictor of how much of your Mark I work will transfer is:

1. the **run-time base family** and **MMIO family** of the firmware ([S1] tabulates both for 32 bodies: two MMIO families, split where the
   newest processor generation begins; eleven run-time bases),
2. how many instructions pair up between the two builds ([S1] reports this against its OM-1 reference build: about 1.4 M paired
   instructions for the E-M10 Mark IV and the E-P7, about 0.8 M for the E-M5 II / E-M10 II / PEN-F family: a measure of "built alike", not
   of "patches port"), and
3. whether your code of interest is **UI / settings / menu code** (portable by method, large address differences) or **image-engine setup
   code** (depends on the engine generation).

Same sensor resolution is not evidence of compatible firmware; e.g. [S9] documents the 16.1 MP E-M5 Mark II with TruePic VII, the same
engine generation [S4] gives the E-M10 Mark I, and the two builds still differ in layout.

## Check a body in five minutes (you need your own copy of its firmware)

1. Decode the container with a public tool ([S3] documents the container and its checksum; the transform is simple).
2. Find the main code block (the largest one that starts executing); disassemble its first instructions with your own MN10300-capable
   disassembler. The expected start-up sequence, per [S1], is: mask interrupts in the PSW (`and imm,psw`), two `nop`, `add -4,sp`, then a
   `calls`. If you see that, the main block is AM33 code and `pyemu` can execute it.
3. Fill `profiles/template.json` (`docs/profile.md`) with the load base you found and run `examples/` against your profile.
4. Unimplemented mnemonics raise `NotImplementedError` with the address: the core implements the integer subset firmware helper code
   uses (see the header of `emu/pyemu.py`), not the whole ISA. Add what you need to `pyemu` first, then to the Rust core, and let
   `difftest` tell you if they disagree.

## Different ISA, or code on another core

If the code you care about is **not** MN10300 (for example the ARM core of the newest bodies [S7], or another vendor's camera):

* `emu/` does not apply. Do not point it at ARM code: it will fail loudly (unknown mnemonic) rather than guess.
* `lane/` still applies: the driver runs any command as a stage (RAM gate, per-stage scratch, I/O audit, run comparison).
* `difftest/` as a *method* still applies: any two implementations of one interpreter (a Python reference and a fast core) can be run in
  lock-step; the fuzz scaffolding in `difftest/cpufuzz.py` is MN10300-specific but short.
* `docs/method.md` (negative controls by value, real-path emulation over stubs, dual audit) applies to every firmware-modification
  project.

## Sources (accessed 2026-11-23)

* **[S1]** flatpanda, *om-emu*, `docs/model-survey.md`: survey of 32 firmware files, reset heads, MMIO families, run-time bases, per-body status.
  https://github.com/flatpanda/om-emu/blob/main/docs/model-survey.md (MIT-licensed project; nothing is copied from it here, only facts are cited)
* **[S2]** flatpanda, *om-emu*, `README.md`: the table of bodies the lab runs through ports (E-M10 II/III/IV, E-M5 II/III, PEN-F, E-P7, E-PL9, OM-1/3/5 families ...).
  https://github.com/flatpanda/om-emu
* **[S3]** fishcu, *oly_fw_analysis*, `docs/tooling/EMULATOR.md` and `docs/format/CONTAINERS.md`: MN103 emulator and native-call harness on PEN-F firmware; container layout and payload transform.
  https://github.com/fishcu/oly_fw_analysis (Apache-2.0)
* **[S4]** HandWiki, *Engineering:TruePic*: TruePic generations by body; "based on the Panasonic MN103 architecture".
  https://handwiki.org/wiki/Engineering:TruePic
* **[S5]** matozoid, *em10_1*, `docs/mods/related-projects.md`: notes on the OM-3 (MN10300/AM33, same container and update procedure as the E-M10 Mark I) and on the Caduceus projects.
  https://github.com/matozoid/em10_1/blob/main/docs/mods/related-projects.md
* **[S6]** cakelesscoder, *caduceus-ep7*, `README.md` and `docs/BODIES.md`: the E-P7 project and the list of ports on other bodies.
  https://github.com/cakelesscoder/caduceus-ep7
* **[S7]** flatpanda, *om-emu*, `docs/om1-cores-spec.md`: the OM-1's four MN10300-family cores and its ARM Cortex-A53 communications processor.
  https://github.com/flatpanda/om-emu/blob/main/docs/om1-cores-spec.md
* **[S8]** cmdada, *em10-fw*: E-M10 (Mark I) firmware work tagged `mn10300`. https://github.com/cmdada/em10-fw
* **[S9]** OM Digital Solutions, *OM-D E-M5 Mark II specification sheet* (16.1 MP Live MOS, TruePic VII). https://m.media-amazon.com/images/I/81f4bl7tfUL.pdf
  and the OM System support page *What differentiates the E-M10 Mark III from the E-M10 Mark II?* (TruePic VIII vs VII),
  https://learnandsupport.getolympus.com/support/e-m10-mark-iii
* **Ours:** E-M10 Mark I: decoded and disassembled by the owner of this toolkit; E-M10 Mark II: static comparison note (same CPU family and container, different load address and layout). No data from either is published.

Caveats on the sources: [S1]/[S2]/[S7] are single-author public projects; their surveys are reset-head or UI-level, not proofs of full
ISA coverage. [S4] is a wiki page last edited in 2021 and predates the newest bodies. Corrections welcome (issues / PRs).
