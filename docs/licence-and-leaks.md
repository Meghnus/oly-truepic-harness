# Licence and leaks

**This repository contains zero firmware.** The vendor's licence forbids redistributing the images or anything derived from them. That
means: no `.BIN`, no decoded partitions, no dumps, no disassembly listings, no byte excerpts, no patch lists, no per-body tables or
addresses used as data, no run directories, no decode caches. The code here is our own (Apache-2.0, see `LICENSE` and `NOTICE`); you run it
on **your own** downloaded firmware, locally.

What is where: your profile (`profiles/*.json`), decoded images, the decode cache (`.omh_cache/`) and the shared image store are
**yours** and are git-ignored. Do not commit them, attach them to issues, or paste them into chats.

## `tools/leakcheck.py`

Run before every commit and push:

```
python tools/leakcheck.py                              # file types, sizes, containers, hex/base64 blobs, address-like literals
python tools/leakcheck.py --stock path/to/stock.BIN    # + any run >= 16 bytes shared with your stock image (raw and decoded)
python tools/leakcheck.py --list-addresses             # every 0x-literal with 5+ digits and why it is allowed
```

* forbidden file types (`.bin .dec .dmp .elf .hex .pkl .zip ...`), names with "dump", files that start like an `OE` container, files over 1 MB,
  200+ hex digits or 300+ base64 characters in a text file;
* address-like literals (`0x` + 5 or more digits) must each be covered by `tools/addr_allowlist.txt` **with a justification**; the shipped
  list contains only ISA constants and synthetic test addresses. A firmware address is never a valid justification;
* `--stock`: the container transform is implemented in the tool (cross-checked against an independent decoder), so the raw file and every
  decoded partition are compared with **every file** of the repository, 16-byte windows, padding-like windows ignored; a human-reviewed
  harmless coincidence (a common English phrase) is recorded by SHA-1 in `tools/leak_reviewed.txt`, never as bytes.

A pre-push hook is a good idea: `python tools/leakcheck.py --stock "$OMH_STOCK"`.

## Credits (facts and ideas, no code copied)

* **fishcu / oly_fw_analysis** (Apache-2.0): published facts about the container format and checksum, the MN103 instruction-level analysis
  of PEN-F, and the practice of keeping firmware local. Facts only; nothing here depends on its tools.
* **cmdada** (em10-fw and related work): E-M10 Mark I firmware facts. Facts only.
* **flatpanda / om-emu** (MIT): the multi-body survey cited in `docs/COMPATIBILITY.md`. Facts only.
* GNU binutils: only as an optional *external* disassembler you install yourself; nothing of it is included.
