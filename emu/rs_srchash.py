"""rs_srchash: the source hash that ties a built _rs_emu to its sources.

sha256 over src/*.rs, Cargo.toml, Cargo.lock, rust-toolchain.toml (relative to the crate root), sorted by relative path, each as
`<relpath>\\0<len>\\0<bytes>`.  `python -m emu.build` compiles it into the module (_rs_emu.SRC_SHA); emu.rs_shim recomputes it at import and
refuses a mismatch (a stale build can never run silently).
"""
import glob
import hashlib
import os

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rs_emu")


def files(root=HERE):
    fs = sorted(glob.glob(os.path.join(root, "src", "*.rs")))
    fs += [os.path.join(root, n) for n in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml")]
    return fs


def src_sha(root=HERE):
    h = hashlib.sha256()
    for f in files(root):
        rel = os.path.relpath(f, root).replace("\\", "/")
        with open(f, "rb") as fh:
            data = fh.read()
        h.update(rel.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return h.hexdigest()
