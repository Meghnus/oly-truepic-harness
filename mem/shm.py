"""mem.shm: shared read-only / copy-on-write images through a content-addressed store on disk.

Problem: N parallel stage processes each load the same decoded firmware image (tens of MB) and each owns a private copy.  Solution: write the
bytes ONCE into a store file named by its sha1, mark it read-only (0o444), and map it:

    shared_read(path)   -> ROBytes   read-only mmap (ACCESS_READ): bytes-like, shared by every process through the OS page cache
    shared_copy(path)   -> CowBytes  copy-on-write mmap (ACCESS_COPY): mutable like a bytearray, untouched pages stay shared, a process
                                     owns only the pages it writes (patching 40 bytes of a 20 MB image costs one page)
    share_bytes(b) / cow_of(data)    the same for bytes already in memory

The original file is never mapped (a mapped file cannot be rewritten on Windows and some stages rewrite their inputs): the store copy is.
Store files are immutable; opening one for writing raises PermissionError instead of corrupting every process.

The store directory is `$OMH_STORE`, default `./.omh_cache/store` (git-ignored).  It holds copies of YOUR firmware: never publish it.
Explicit API only (no open() hook, no gc sweep): call shared_copy / Image.from_file(shared=True) where you load an image.

CLI: python -m mem.shm gc [days] [--dry]     delete store files not read for `days` days (default 3); they are re-created on demand.
"""
import builtins
import hashlib
import mmap
import os
import sys
import threading

STORE = os.environ.get("OMH_STORE") or os.path.join(os.getcwd(), ".omh_cache", "store")
BIG = 1 << 20                      # cow_of() maps data >= 1 MB; smaller data is just copied
_real_open = builtins.open
_stat = {"blobs": 0}


class _Buf(mmap.mmap):
    """bytes-like over an mmap: the comparison / hash / concat / text helpers bytes and bytearray have and mmap lacks"""
    __slots__ = ()

    def __eq__(self, o):
        if isinstance(o, (bytes, bytearray, memoryview, mmap.mmap)):
            return len(self) == len(o) and memoryview(self) == memoryview(o)
        return NotImplemented

    def __ne__(self, o):
        r = self.__eq__(o)
        return r if r is NotImplemented else not r

    __hash__ = None

    def __add__(self, o):
        return bytes(self[:]) + bytes(o)

    def __radd__(self, o):
        return bytes(o) + bytes(self[:])

    def __contains__(self, x):
        return self.find(x) >= 0 if isinstance(x, (bytes, bytearray, memoryview)) else x in self[:]

    def hex(self, *a):
        return self[:].hex(*a)

    def startswith(self, p, *a):
        return self[:].startswith(p, *a)

    def endswith(self, p, *a):
        return self[:].endswith(p, *a)

    def count(self, *a):
        return self[:].count(*a)

    def index(self, *a):
        return self[:].index(*a)

    def decode(self, *a, **k):
        return self[:].decode(*a, **k)

    def __reduce__(self):                    # pickling / copying a mapping = its bytes
        return (bytes, (self[:],))

    def __bytes__(self):
        return self[:]

    def __getattr__(self, k):                # any other bytes method: through a materialised copy (never for dunder lookups: those must raise)
        if k.startswith("__"):
            raise AttributeError(k)
        return getattr(self[:], k)


class ROBytes(_Buf):
    """a read-only shared mapping (stand-in for the `bytes` an open(f, 'rb').read() returns)"""
    __slots__ = ()

    def __hash__(self):
        return hash(self[:])


class CowBytes(_Buf):
    """a copy-on-write private mapping (stand-in for `bytearray(open(f, 'rb').read())`): untouched pages stay shared"""
    __slots__ = ()


_blob_of = {}                                  # (abspath, size, mtime_ns) -> store path


def _map(path, cls, access):
    f = _real_open(path, "rb")
    try:
        return cls(f.fileno(), 0, access=access)
    finally:
        f.close()


def store_blob(data):
    """write `data` once into the content-addressed store (atomic, safe across parallel processes) and return its path"""
    h = hashlib.sha1(data).hexdigest()
    p = os.path.join(STORE, h + ".bin")
    if not os.path.exists(p):
        os.makedirs(STORE, exist_ok=True)
        tmp = "%s.%d.%d.tmp" % (p, os.getpid(), threading.get_ident())
        with _real_open(tmp, "wb") as f:
            f.write(data)
        try:
            os.chmod(tmp, 0o444)                  # immutable
            os.replace(tmp, p)
        except OSError:
            if os.path.exists(tmp):
                os.chmod(tmp, 0o666)
                os.remove(tmp)
    return p


def blob_of_file(path):
    """the store copy of a (big) file"""
    ap = os.path.abspath(path)
    st = os.stat(ap)
    k = (ap, st.st_size, st.st_mtime_ns)
    p = _blob_of.get(k)
    if p is None or not os.path.exists(p):
        with _real_open(ap, "rb") as f:
            data = f.read()
        p = _blob_of[k] = store_blob(data)
        _stat["blobs"] += 1
    return p


def shared_read(path):
    """the whole file as a read-only shared mapping of its store copy"""
    return _map(blob_of_file(path), ROBytes, mmap.ACCESS_READ)


def shared_copy(path):
    """the whole file as a copy-on-write mapping (mutable like a bytearray; writes stay private)"""
    return _map(blob_of_file(path), CowBytes, mmap.ACCESS_COPY)


def share_bytes(b):
    """an immutable big `bytes` as the read-only mapping of its store copy"""
    return _map(store_blob(b), ROBytes, mmap.ACCESS_READ)


def cow_of(data):
    """a mutable private copy of `data` (bytes / bytearray / ROBytes / CowBytes) backed by a shared store file"""
    if isinstance(data, (bytes, bytearray, mmap.mmap)) and len(data) >= BIG:
        return _map(store_blob(data), CowBytes, mmap.ACCESS_COPY)
    return bytearray(data)


def trim_store(max_gb=None):
    """keep the store under `max_gb` (default $HM_STORE_MAX_GB or 12): drop the least recently used blobs (a blob another process has mapped
    cannot be deleted on Windows: skipped)"""
    import glob
    mx = float(max_gb if max_gb is not None else os.environ.get("OMH_STORE_MAX_GB", "12")) * 2**30
    fs = [(max(os.stat(f).st_atime, os.stat(f).st_mtime), os.stat(f).st_size, f) for f in glob.glob(os.path.join(STORE, "*.bin"))]
    tot = sum(x[1] for x in fs)
    if tot <= mx:
        return
    for _, sz, f in sorted(fs):
        if tot <= 0.8 * mx:
            break
        try:
            os.chmod(f, 0o666)
            os.remove(f)
            tot -= sz
        except OSError:
            try:
                os.chmod(f, 0o444)
            except OSError:
                pass


def gc_store(days=3.0, dry=False):
    """delete store files not read for `days` days; they are re-created on demand"""
    import glob
    import time
    n = tot = 0
    for f in glob.glob(os.path.join(STORE, "*.bin")) + glob.glob(os.path.join(STORE, "*.tmp")):
        st = os.stat(f)
        if time.time() - max(st.st_atime, st.st_mtime) > days * 86400:
            tot += st.st_size
            n += 1
            if not dry:
                try:
                    os.chmod(f, 0o666)
                    os.remove(f)
                except OSError:
                    pass
    print("gc: %d files, %.0f MB %s" % (n, tot / 2**20, "would be removed" if dry else "removed"))


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "gc":
    gc_store(float(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else 3.0, "--dry" in sys.argv)
