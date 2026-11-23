"""Benchmark with NO firmware: a synthetic MN10300 workload, Python core vs Rust core, and the memory effect of the shm store.

    python examples/bench/bench.py                 # 1 MB workload (6.3 M steps), 4 processes x 32 MB image   (a few minutes with the Python core)
    python examples/bench/bench.py --mb 0.25 --step-count 100000 --image-mb 16     # quick
    python examples/bench/bench.py --skip-shm | --skip-run

WORKLOAD  a byte-increment loop over --mb MB of memory:  movbu (a0),d0 ; add 1,d0 ; movbu d0,(a0) ; inc a0 ; sub 1,d1 ; bne loop
          (6 steps per byte: a load, an ALU op, a store with write audit, flags, a taken branch).  Every byte is read once and written once.
TIMING    one fresh process per configuration (OMH_RUST / OMH_MEM are process-global), time.perf_counter around
            run()    : one cpu.run(...) over the whole workload (the engine's own loop; for the Rust core the multi-step native loop)
            step()   : --step-count calls of cpu.step() from a Python loop (what a harness that single-steps pays per call)
          Configurations: py-dict (stock dict memory), py-page (PageMem), rust (Rust core + Rust PageMem).  Steps/s = steps / seconds.
MEMORY    peak working set of each timing process (VmHWM / peak_wset), and the shm store: N processes each hold the same image, loaded plain
          (every process owns a copy) versus Image.from_file(shared=True) (one read-only store file mapped copy-on-write, a process owns only the
          pages it writes).  Per-process private memory (psutil USS) summed, plus the shared pages once.  Needs psutil for the shm part.
Numbers depend on the machine: run it on an IDLE machine, and quote your own.
"""
import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = 0x400000
DATA = 0x10000000


def peak_mb():
    try:
        import psutil
        mi = psutil.Process().memory_info()
        return getattr(mi, "peak_wset", mi.rss) / 2**20
    except ImportError:
        pass
    try:
        with open("/proc/self/status") as f:
            for ln in f:
                if ln.startswith("VmHWM:"):
                    return int(ln.split()[1]) / 1024
    except OSError:
        pass
    return float("nan")


def worker(a):
    sys.path.insert(0, ROOT)
    from ofh.emu import asm, pyemu as E
    E.configure(BASE)
    n = int(a.mb * 2**20)
    src = ("start:\n mov 0x%x,a0\n mov %d,d1\nloop:\n movbu (a0),d0\n add 1,d0\n movbu d0,(a0)\n inc a0\n sub 1,d1\n bne loop\ndone:\n nop\n" % (DATA, n))
    prog = asm.assemble(src, BASE)

    def fresh():
        cpu = E.CPU(E.Image(bytes(0x100), disasm=prog.disassembler()))
        cpu.sp = 0x7ff00000
        return cpu
    out = dict(config=a.config, bytes=n)
    t = time.perf_counter()
    cpu = fresh()
    cpu.run(prog.labels["start"], {prog.labels["done"]}, 1 << 40)
    out["run_s"], out["run_steps"] = time.perf_counter() - t, cpu.steps
    assert cpu.mem.get(DATA) == 1 and cpu.mem.get(DATA + n - 1) == 1, "workload did not run correctly"
    out["peak_mb_run"] = peak_mb()
    cpu = fresh()
    cpu.pc = prog.labels["start"]
    k = a.step_count
    t = time.perf_counter()
    for _ in range(k):
        cpu.step()
    out["step_s"], out["step_steps"] = time.perf_counter() - t, k
    out["type"] = type(cpu.mem).__name__
    print(json.dumps(out))


def shm_worker(a):
    sys.path.insert(0, ROOT)
    from ofh.emu import pyemu as E
    E.configure(BASE)
    if a.kind == "shm":
        img = E.Image.from_file(a.path, patches={BASE + 16: b"\x01" * 40}, shared=True)
    else:
        with open(a.path, "rb") as f:
            img = E.Image(f.read(), patches={BASE + 16: b"\x01" * 40})
    s = 0
    for i in range(0, len(img.d), 4096):                 # touch every page (a plain copy already owns them all)
        s += img.d[i]
    print("READY", flush=True)
    sys.stdin.read()                                      # hold the memory until the parent has measured


def run_worker(cfg, mb, step_count):
    env = dict(os.environ, PYTHONPATH=ROOT)
    env.pop("OMH_RUST", None)
    env.pop("OMH_MEM", None)
    if cfg == "py-page":
        env["OMH_MEM"] = "page"
    elif cfg == "rust":
        env["OMH_RUST"] = "1"
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "--worker", cfg, "--mb", str(mb), "--step-count", str(step_count)],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        return dict(config=cfg, error=(r.stderr.strip().splitlines() or ["?"])[-1][:200])
    return json.loads(r.stdout.strip().splitlines()[-1])


def bench_run(a):
    rows = [run_worker(c, a.mb, a.step_count) for c in ("py-dict", "py-page", "rust")]
    base = next((r for r in rows if r["config"] == "py-dict" and "error" not in r), None)
    print("\nrun()/step() timing: %.2f MB workload = %d bytes, %s steps by run(), %d steps by step()" % (
        a.mb, int(a.mb * 2**20), "{:,}".format(next((r["run_steps"] for r in rows if "run_steps" in r), 0)), a.step_count))
    print("%-9s %10s %12s %9s | %10s %12s %9s | %9s" % ("config", "run() s", "steps/s", "speed-up", "step() s", "steps/s", "speed-up", "peak MB"))
    for r in rows:
        if "error" in r:
            print("%-9s %s" % (r["config"], r["error"]))
            continue
        rs, ss = r["run_steps"] / r["run_s"], r["step_steps"] / r["step_s"]
        b_rs = base["run_steps"] / base["run_s"] if base else rs
        b_ss = base["step_steps"] / base["step_s"] if base else ss
        print("%-9s %10.2f %12s %8.1fx | %10.2f %12s %8.1fx | %9.0f" % (r["config"], r["run_s"], "{:,.0f}".format(rs), rs / b_rs, r["step_s"],
                                                                      "{:,.0f}".format(ss), ss / b_ss, r["peak_mb_run"]))
    return rows


def bench_shm(a):
    try:
        import psutil
    except ImportError:
        print("\nshm memory: needs psutil (pip install psutil); skipped")
        return
    import tempfile
    d = tempfile.mkdtemp(prefix="omh_bench_")
    img = os.path.join(d, "image.bin")
    with open(img, "wb") as f:
        f.write(os.urandom(a.image_mb * 2**20))
    store = os.path.join(d, "store")
    res = {}
    for kind in ("plain", "shm"):
        env = dict(os.environ, PYTHONPATH=ROOT, OMH_STORE=store)
        ps = [subprocess.Popen([sys.executable, os.path.abspath(__file__), "--shm-worker", kind, "--path", img], env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, text=True) for _ in range(a.procs)]
        for p in ps:
            assert p.stdout.readline().strip() == "READY"
        uss = rss = 0.0
        for p in ps:
            mi = psutil.Process(p.pid).memory_full_info()
            uss += mi.uss / 2**20
            rss += mi.rss / 2**20
        for p in ps:
            p.stdin.close()
            p.wait()
        res[kind] = (uss, rss)
    print("\nshm store: %d processes, each holding the same %d MB image and patching 40 bytes of it" % (a.procs, a.image_mb))
    print("%-22s %16s %18s %22s" % ("loading", "sum private (MB)", "sum resident (MB)", "private + shared once"))
    pu, pr = res["plain"]
    su, sr = res["shm"]
    print("%-22s %16.0f %18.0f %22.0f" % ("plain read()", pu, pr, pu))
    print("%-22s %16.0f %18.0f %22.0f" % ("shm shared_copy (CoW)", su, sr, su + a.image_mb))
    print("saving: %.0f MB (%.1fx less); 'sum resident' double-counts the shared pages, 'private + shared once' is the physical estimate" % (
        pu - (su + a.image_mb), pu / max(su + a.image_mb, 1e-9)))
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mb", type=float, default=1.0)
    ap.add_argument("--step-count", type=int, default=300000)
    ap.add_argument("--image-mb", type=int, default=32)
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--skip-run", action="store_true")
    ap.add_argument("--skip-shm", action="store_true")
    ap.add_argument("--worker")
    ap.add_argument("--shm-worker", dest="kind")
    ap.add_argument("--path")
    a = ap.parse_args()
    if a.worker:
        a.config = a.worker
        return worker(a)
    if a.kind:
        return shm_worker(a)
    sys.path.insert(0, ROOT)
    print("bench on %s, Python %s (idle machine recommended)" % (sys.platform, sys.version.split()[0]))
    if not a.skip_run:
        bench_run(a)
    if not a.skip_shm:
        bench_shm(a)


if __name__ == "__main__":
    main()
