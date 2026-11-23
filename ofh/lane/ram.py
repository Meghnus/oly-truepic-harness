"""ofh.lane.ram: the RAM gate.

Emulation stages are memory hungry and their peaks are unpredictable; starting one too many in parallel makes the OS page or kill them and
turns a verification run into noise.  The gate decides whether the NEXT pending stage may start NOW:

    free_now          >= ram_gb                       (never start under pressure: absolute floor, GB)
    free_now - growth - est >= ram_buf                (after every RUNNING stage has grown to its own estimate and the new stage has grown
                                                       to ITS estimate, still keep a buffer, GB)

where growth = sum over running stages of max(0, estimate - current RSS).  A stage that does not fit is skipped for now; a smaller pending
stage may start instead.  gate_ok() is a pure function (unit-tested); free_gb() / tree_rss() read the machine.  psutil is used when installed;
otherwise Windows (ctypes) and Linux (/proc) fallbacks provide free memory, and per-process RSS falls back to /proc on Linux, else 0.0
(then only the absolute floor and the estimates protect you: the driver prints that once).
"""
import os
import sys


def gate_ok(free_now_gb, growth_gb, est_gb, ram_gb, ram_buf_gb):
    """True if a stage with estimate `est_gb` may start"""
    return free_now_gb >= ram_gb and (free_now_gb - growth_gb - est_gb) >= ram_buf_gb


def growth_gb(running, estimates_mb, current_mb, mem_scale=1.0):
    """GB the running stages still have to grow to reach their estimates (running = iterable of names)"""
    return sum(max(0.0, estimates_mb[m] * mem_scale - current_mb.get(m, 0.0)) for m in running) / 1024.0


def free_gb():
    try:
        import psutil
        return psutil.virtual_memory().available / 2 ** 30
    except ImportError:
        pass
    if sys.platform.startswith("win"):
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong), ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
        ms = MS()
        ms.dwLength = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
        return ms.ullAvailPhys / 2 ** 30
    try:
        with open("/proc/meminfo") as f:
            for ln in f:
                if ln.startswith("MemAvailable:"):
                    return int(ln.split()[1]) / 2 ** 20
    except OSError:
        pass
    raise RuntimeError("ofh.lane.ram.free_gb: no way to read free memory on this platform (install psutil)")


_warned = []


def tree_rss(proc):
    """RSS in MB of the stage process + its children; a process that vanishes / denies access mid-poll is skipped, not the whole sample"""
    pid = proc.pid
    try:
        import psutil
        try:
            pr = psutil.Process(pid)
            procs = [pr] + pr.children(recursive=True)
        except Exception:
            return 0.0
        tot = 0
        for x in procs:
            try:
                tot += x.memory_info().rss
            except Exception:
                pass
        return tot / 2 ** 20
    except ImportError:
        pass
    if os.path.isdir("/proc/%d" % pid):
        tot = 0
        for p in [pid] + _children_proc(pid):
            try:
                with open("/proc/%d/statm" % p) as f:
                    tot += int(f.read().split()[1]) * (os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096)
            except (OSError, ValueError, IndexError):
                pass
        return tot / 2 ** 20
    if not _warned:
        _warned.append(1)
        print("ofh.lane.ram: per-process RSS unknown on this platform (install psutil); the gate uses the estimates only", flush=True)
    return 0.0


def _children_proc(pid):
    out = []
    try:
        for d in os.listdir("/proc"):
            if d.isdigit():
                try:
                    with open("/proc/%s/stat" % d) as f:
                        if int(f.read().rsplit(")", 1)[1].split()[1]) == pid:
                            out.append(int(d))
                except (OSError, ValueError, IndexError):
                    pass
    except OSError:
        pass
    for c in list(out):
        out += _children_proc(c)
    return out
