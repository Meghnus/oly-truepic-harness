"""python -m emu.build : cargo build --release --locked --features extension-module, then copy the cdylib to emu/_rs_emu<EXT_SUFFIX>.

Needs: Python 3.10+, the pinned Rust toolchain (rustup reads rust-toolchain.toml), network on the first build to fetch the crates in
Cargo.lock (--locked: the lock file is law; a build that would change it fails).  Local only, nothing is uploaded.
The source hash (rs_srchash.src_sha) is compiled in as _rs_emu.SRC_SHA; emu/_rs_emu.build.json records the module sha1, rustc -Vv, python, time.
"""
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig

HERE = os.path.dirname(os.path.abspath(__file__))
CRATE = os.path.join(HERE, "rs_emu")


def lib_name():
    if sys.platform.startswith("win"):
        return "_rs_emu.dll"
    if sys.platform == "darwin":
        return "lib_rs_emu.dylib"
    return "lib_rs_emu.so"


def main():
    from emu import rs_srchash
    sha = rs_srchash.src_sha(CRATE)
    env = dict(os.environ, PYO3_PYTHON=sys.executable, RS_SRC_SHA=sha)
    subprocess.run(["cargo", "build", "--release", "--locked", "--features", "extension-module"], cwd=CRATE, env=env, check=True)
    if rs_srchash.src_sha(CRATE) != sha:
        sys.exit("emu.build: sources changed during the build; run it again")
    src = os.path.join(CRATE, "target", "release", lib_name())
    dst = os.path.join(HERE, "_rs_emu" + sysconfig.get_config_var("EXT_SUFFIX"))
    tmp = dst + ".tmp"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
    with open(dst, "rb") as f:
        pyd_sha1 = hashlib.sha1(f.read()).hexdigest()
    info = dict(src_sha=sha, module=os.path.basename(dst), module_sha1=pyd_sha1,
                rustc=subprocess.run(["rustc", "-Vv"], cwd=CRATE, capture_output=True, text=True).stdout.strip().splitlines(),
                python=sys.version, time=datetime.datetime.now().isoformat(timespec="seconds"))
    with open(os.path.join(HERE, "_rs_emu.build.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, indent=1)
    print("built", dst, os.path.getsize(dst), "SRC_SHA", sha[:16])


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(HERE))
    main()
