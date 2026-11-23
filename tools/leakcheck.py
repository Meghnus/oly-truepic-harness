#!/usr/bin/env python3
"""leakcheck - run before every commit / push of this repository.

This toolkit must contain ZERO firmware: no images, no decoded partitions, no dumps, no disassembly, no byte excerpts, no firmware-specific
tables.  The licence of the firmware forbids redistributing it or anything derived from it.  This script fails (exit 1) when it finds:

  * a forbidden file type (.bin .dec .part .dmp .dump .img .rom .elf .hex .srec .pkl .pickle .npy .npz .db .sqlite .zip .7z .gz .xz .tar ...)
    or a name containing "dump"
  * a file that starts like an OM firmware container (marker "OE" + a valid component chain)
  * a file larger than 1 MB
  * a long hex string (200+ hex digits) or base64-looking blob in any text file
  * a hex literal that looks like an address (0x + 5 or more digits) which is not covered by tools/addr_allowlist.txt - every remaining
    one must be listed THERE with a justification, so "firmware addresses as data" cannot creep in unnoticed
  * with --stock FILE (your own official image, kept OUTSIDE the repo): any run of 16 or more identical bytes shared between ANY file of the
    repository and the stock image, both the raw file and its decoded partitions (the container transform is implemented here, so no other
    tool is needed).  Padding-like windows (fewer than 6 distinct byte values, or one byte value filling 8 of the 16 positions, e.g. an
    indented line) are ignored.  --stock-dir DIR adds a directory of already decoded partitions.  A shared run that a human has looked at and
    found to be ordinary text (a common English phrase) is recorded by the SHA-1 of its bytes in tools/leak_reviewed.txt, never as bytes.

    python tools/leakcheck.py
    python tools/leakcheck.py --stock path/to/your_stock.BIN
    python tools/leakcheck.py --list-addresses          # print every address-like literal with file:line (to maintain the allowlist)

Only the standard library is used.  Nothing is written anywhere.  Build outputs (ofh/emu/rs_emu/target, *.pyd, .omh_cache) are skipped when the
repository is not a git checkout; in a git checkout exactly the files git would commit are checked (tracked + untracked, not ignored).
"""
import argparse
import fnmatch
import hashlib
import os
import re
import struct
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAD_EXT = {".bin", ".dec", ".part", ".dmp", ".dump", ".img", ".rom", ".elf", ".hex", ".srec", ".s19", ".pkl", ".pickle", ".npy", ".npz",
           ".db", ".sqlite", ".zip", ".7z", ".gz", ".xz", ".bz2", ".tar", ".rar", ".dis", ".lst"}
SKIP_DIRS = {".git", "target", ".omh_cache", "__pycache__", ".pytest_cache"}
SKIP_EXT = {".pyd", ".so", ".dll", ".dylib", ".pyc", ".rlib", ".exe", ".pdb"}
LIMIT = 1 << 20
HEX_RUN = re.compile(rb"[0-9a-fA-F]{200,}")
B64_RUN = re.compile(rb"[A-Za-z0-9+/]{300,}={0,2}")
ADDR = re.compile(r"0[xX][0-9a-fA-F]{5,}")
TEXT_EXT = {".md", ".py", ".txt", ".bat", ".sh", ".command", ".yml", ".yaml", ".cfg", ".toml", ".html", ".json", ".rs", ".lock", ".gitignore",
            ".gitattributes", ""}
PERM = (1, 3, 0, 2, 6, 4, 7, 5, 11, 10, 9, 8, 13, 14, 12, 15)    # container payload transform: out[i] = in[PERM[i]] ^ 0xff, per 16-byte group
WIN = 16
MIN_DISTINCT = 6
MAX_REPEAT = 8


def list_files():
    try:
        out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, check=True).stdout
        names = [n for n in out.decode("utf-8", "surrogateescape").split("\0") if n]
        return sorted(set(n for n in names if os.path.isfile(os.path.join(ROOT, n))))
    except (OSError, subprocess.CalledProcessError):
        names = []
        for d, dirs, files in os.walk(ROOT):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
            for f in files:
                if os.path.splitext(f)[1].lower() not in SKIP_EXT:
                    names.append(os.path.relpath(os.path.join(d, f), ROOT).replace(os.sep, "/"))
        return sorted(names)


def looks_like_container(head, size):
    if head[:2] != b"OE" or len(head) < 16:
        return False
    n = struct.unpack_from("<I", head, 12)[0]
    return 32 + n + 32 <= size


def load_allowlist():
    """tools/addr_allowlist.txt lines: `<glob-of-files> <regex-of-literal>   # justification` (a justification is REQUIRED)"""
    rules = []
    p = os.path.join(ROOT, "tools", "addr_allowlist.txt")
    if not os.path.exists(p):
        return rules
    with open(p, encoding="utf-8") as f:
        for n, ln in enumerate(f, 1):
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            body, _, why = s.partition("#")
            parts = body.split()
            if len(parts) != 2 or not why.strip():
                raise SystemExit("tools/addr_allowlist.txt:%d: expected '<file glob> <literal regex>  # justification'" % n)
            rules.append((parts[0], re.compile("^" + parts[1] + "$", re.I), why.strip()))
    return rules


def scan_addresses(names, rules, verbose=False):
    """(problems, all occurrences) of address-like literals in text files that no allowlist rule covers"""
    probs, seen = [], []
    for name in names:
        ext = os.path.splitext(name.lower())[1]
        if ext not in TEXT_EXT or name == "tools/addr_allowlist.txt":
            continue
        try:
            with open(os.path.join(ROOT, name), encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for i, ln in enumerate(lines, 1):
            for m in ADDR.finditer(ln):
                lit = m.group(0)
                why = next((w for g, rx, w in rules if fnmatch.fnmatch(name, g) and rx.match(lit)), None)
                seen.append((name, i, lit, why))
                if why is None:
                    probs.append("%s:%d: address-like literal %s is not in tools/addr_allowlist.txt (justify it there or remove it)" % (name, i, lit))
    return probs, seen


def check_files(problems):
    names = list_files()
    for name in names:
        path = os.path.join(ROOT, name)
        low = name.lower()
        ext = os.path.splitext(low)[1]
        size = os.path.getsize(path)
        base = os.path.basename(low)
        if ext in BAD_EXT:
            problems.append("%s: forbidden file type" % name)
        if "dump" in base:
            problems.append("%s: name contains 'dump'" % name)
        if size > LIMIT:
            problems.append("%s: %d bytes exceeds the %d byte limit" % (name, size, LIMIT))
        with open(path, "rb") as f:
            head = f.read(64)
            if looks_like_container(head, size):
                problems.append("%s: starts like an OM firmware container" % name)
            if ext in TEXT_EXT and size <= LIMIT:
                f.seek(0)
                data = f.read()
                if HEX_RUN.search(data):
                    problems.append("%s: contains a hex string of 200+ digits (byte excerpt?)" % name)
                if B64_RUN.search(data):
                    problems.append("%s: contains a long base64-like blob" % name)
    return names


# ------------------------------------------------------------------------------------------------------------------ stock comparison
def decode_container(data):
    """yield (offset, decoded payload) for every component of an OE container (header 32 B, payload, trailer 32 B)"""
    off = 0
    while off + 64 <= len(data) and data[off:off + 2] == b"OE":
        n = struct.unpack_from("<I", data, off + 12)[0]
        if n % 16 or off + 32 + n + 32 > len(data):
            break
        pay = bytearray(n)
        src = memoryview(data)[off + 32:off + 32 + n]
        for g in range(0, n, 16):
            blk = src[g:g + 16]
            for i in range(16):
                pay[g + i] = blk[PERM[i]] ^ 0xff
        yield off, bytes(pay)
        off += 32 + n + 32


def load_reviewed():
    """tools/leak_reviewed.txt lines: `<sha1 hex of the shared run>   # file, why it is harmless`"""
    p = os.path.join(ROOT, "tools", "leak_reviewed.txt")
    out = {}
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            for ln in f:
                body, _, why = ln.strip().partition("#")
                if body.strip():
                    if not why.strip():
                        raise SystemExit("tools/leak_reviewed.txt: every entry needs a '# reason'")
                    out[body.strip().lower()] = why.strip()
    return out


def windows_of(data):
    """{16-byte window: first offset} of `data`, skipping padding-like windows"""
    out = {}
    for i in range(0, len(data) - WIN + 1):
        w = data[i:i + WIN]
        s = set(w)
        if len(s) >= MIN_DISTINCT and max(map(w.count, s)) < MAX_REPEAT:
            out.setdefault(w, i)
    return out


def scan_stock(names, buffers):
    """buffers = [(label, bytes)]; returns problems (every maximal shared run >= 16 bytes, per repo file)"""
    probs = []
    repo = {}                                    # window -> [(file, offset)]
    for name in names:
        with open(os.path.join(ROOT, name), "rb") as f:
            d = f.read()
        for w, o in windows_of(d).items():
            repo.setdefault(w, []).append((name, o))
    print("leakcheck: %d distinct 16-byte windows from %d repository files; scanning %.1f MB of stock data"
          % (len(repo), len(names), sum(len(b) for _, b in buffers) / 2**20), flush=True)
    hits = {}
    for label, buf in buffers:
        get = repo.get
        for i in range(len(buf) - WIN + 1):
            r = get(buf[i:i + WIN])
            if r is not None:
                for name, o in r:
                    hits.setdefault((name, label), []).append((o, i))
    reviewed = load_reviewed()
    cache = {}
    for (name, label), hs in sorted(hits.items()):
        hs.sort()
        runs, cur = [], None
        for o, i in hs:
            if cur and o - cur[1] <= 1 and i - cur[3] == o - cur[1]:
                cur[1], cur[3] = o, i
            else:
                cur = [o, o, i, i]
                runs.append(cur)
        for r in runs:
            if name not in cache:
                with open(os.path.join(ROOT, name), "rb") as f:
                    cache[name] = f.read()
            n = r[1] - r[0] + WIN
            h = hashlib.sha1(cache[name][r[0]:r[0] + n]).hexdigest()
            if h in reviewed:
                continue
            probs.append("%s: %d-byte run at file offset %d is also in the stock data (%s offset %d) sha1=%s"
                         % (name, n, r[0], label, r[2], h))
    return probs


def stock_buffers(stock, stock_dir):
    bufs = []
    if stock:
        with open(stock, "rb") as f:
            raw = f.read()
        bufs.append(("raw " + os.path.basename(stock), raw))
        for off, pay in decode_container(raw):
            bufs.append(("decoded partition @%#x of %s" % (off, os.path.basename(stock)), pay))
    if stock_dir:
        for f in sorted(os.listdir(stock_dir)):
            p = os.path.join(stock_dir, f)
            if os.path.isfile(p):
                with open(p, "rb") as fh:
                    bufs.append((f, fh.read()))
    return bufs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--stock", help="your own official image (outside the repo)")
    ap.add_argument("--stock-dir", help="a directory of already decoded partitions to compare against as well")
    ap.add_argument("--list-addresses", action="store_true")
    a = ap.parse_args(argv)
    problems = []
    names = check_files(problems)
    rules = load_allowlist()
    aprobs, seen = scan_addresses(names, rules)
    if a.list_addresses:
        for name, i, lit, why in seen:
            print("%s:%d %s  %s" % (name, i, lit, "OK: " + why if why else "NOT ALLOWED"))
    problems += aprobs
    print("leakcheck: %d files checked; %d address-like literals (%d covered by the allowlist)" % (len(names), len(seen), sum(1 for s in seen if s[3])))
    if a.stock or a.stock_dir:
        bufs = stock_buffers(a.stock, a.stock_dir)
        if not bufs:
            problems.append("--stock / --stock-dir given but nothing to compare against")
        else:
            problems += scan_stock(names, bufs)
            print("leakcheck: stock comparison done (%d buffers)" % len(bufs))
    if problems:
        print("\nLEAKCHECK FAILED (%d):" % len(problems))
        for p in problems:
            print("  " + p)
        return 1
    print("leakcheck: OK (0 problems)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
