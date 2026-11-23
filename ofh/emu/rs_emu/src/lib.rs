//! _rs_emu: the pyemu CPU interpreter (CPU._step / step / run, ld / st / load) in Rust, operating on the SAME Python CPU object
//! (registers stay the Python lists `d` / `a`, flags / pc / sp / mdr / steps / lar / _trap stay instance attributes, `writes` /
//! `trace` stay Python lists), so every caller, subclass, stub and check sees exactly the objects it always saw.  Memory: MemCore.
//!
//! Exactness rules (docs/emu.md):
//!  * an instruction / operand form the decoder cannot mirror exactly, a stub address, a non-u32 register / sp / mdr / pc value, a
//!    non-bool flag read by a branch, an overridden helper method (get / put / ea / reg / flags_* / cond / _chk, on the class or the
//!    instance) -> the ORIGINAL Python CPU._step runs that step (decided before any side effect);
//!  * an overridden ld / st (class or instance) is CALLED (through Python) at the exact point the Python code would call it;
//!  * memory that is neither MemCore nor an exact dict (e.g. a PageMem) -> the previously installed Python ld / st / load run;
//!  * the engine (eng.rs) keeps the CPU state in Rust between Python callbacks and writes it back to the object before EVERY call into
//!    Python code (stub, fallback step, overridden ld / st, dis, ...) and on every return / raise, so Python code never observes a
//!    difference.

use pyo3::exceptions::{PyRuntimeError, PyTypeError};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyDict, PyInt, PyList, PyString, PyTuple, PyType};
use pyo3::{ffi, intern};
use rustc_hash::FxHashMap;
use std::cell::RefCell;
use std::sync::Arc;

mod decode;
mod eng;
mod mem;

use decode::*;
use mem::{exact_i64, pyint, MemCore};

unsafe extern "C" {
    fn _PyType_Lookup(t: *mut ffi::PyTypeObject, name: *mut ffi::PyObject) -> *mut ffi::PyObject;
}


const HELPERS: [&str; 9] = ["get", "put", "ea", "reg", "flags_sub", "flags_add", "flags_logic", "cond", "_chk"];

struct Cfg {
    base: i64,
    align_fault: Py<PyType>,
    trapmsg: Py<PyAny>,
    py_step: Py<PyAny>,
    h_ld: Py<PyAny>,
    h_st: Py<PyAny>,
    h_load: Py<PyAny>,
    helper_names: Vec<Py<PyString>>,
    helper_exp: Vec<usize>, // expected (original) function object pointers, kept alive by _helper_keep
    _helper_keep: Vec<Py<PyAny>>,
    our_ld: Py<PyAny>,
    our_st: Py<PyAny>,
    our_step: Py<PyAny>,
    our_istep: Py<PyAny>,
    img_dis: Py<PyAny>,
    img_lo: i64,           // lowest address at which the image bytes are readable as memory (below it reads give 0); default = base
    image_cls: Py<PyType>, // pyemu.Image: the reviewed split dis overrides call its `dis` dynamically -> the shortcut needs Image.dis is img_dis
    rpm_type: Py<PyType>,
    dis_kind: Py<PyAny>, // rs_shim._dis_kind(func) -> None | (lo_source, size, side_cache_attr) for reviewed range-split dis overrides
    attr_names: Vec<Py<PyString>>,
}

static CFG: PyOnceLock<Cfg> = PyOnceLock::new();

#[derive(Clone, Copy, Default)]
struct TInfo {
    helpers_ok: bool,
    ld_ours: bool,
    st_ours: bool,
    step_ours: bool,
    istep_ours: bool,
    fast_attrs: bool, // generic getattro / setattro and no class attribute named like a CPU state attribute -> the instance dict IS the attribute
    hooks: bool,      // non-generic getattro / setattro (__getattr__, __setattr__, ...) or a descriptor (property, slot, ...) named like a state attribute
}

/// the CPU instance attributes the engine reads / writes
const ATTRS: [&str; 18] = ["d", "a", "sp", "mdr", "pc", "z", "n", "c", "v", "lar", "steps", "_trap", "img", "mem", "writes", "trace", "stubs", "align"];

struct DEnt {
    _tup: Py<PyTuple>,
    o: Py<PyAny>,
    items: Vec<Py<PyAny>>,
    sz: Option<i64>,
    op: Arc<Op>,
}

thread_local! {
    static TCACHE: RefCell<FxHashMap<usize, (u32, TInfo)>> = RefCell::new(FxHashMap::default());
    static ICACHE: RefCell<FxHashMap<usize, (u32, usize)>> = RefCell::new(FxHashMap::default());
    static DCACHE: RefCell<FxHashMap<usize, DEnt>> = RefCell::new(FxHashMap::default());
    static DKIND: RefCell<FxHashMap<usize, (Py<PyAny>, Option<(Py<PyAny>, i64, Py<PyString>)>)>> = RefCell::new(FxHashMap::default());
    static STATS: RefCell<[u64; 20]> = const { RefCell::new([0; 20]) };
}

// stat slots
const S_NATIVE: usize = 0;
const S_FB_HELPER: usize = 1;
const S_FB_STUB: usize = 2;
const S_FB_PC: usize = 3;
const S_FB_DIS: usize = 4;
const S_FB_OP: usize = 5;
const S_FB_STATE: usize = 6;
const S_PY_LD: usize = 7;
const S_PY_ST: usize = 8;
const S_H_LD: usize = 9;
const S_H_ST: usize = 10;
const S_DIS_CALL: usize = 11;
const S_DECODE: usize = 12;
const S_CB: usize = 13;
const S_PYSTEP: usize = 14;
const S_FB_ROUTE: usize = 15; // aliased d / a / writes / trace lists or an unreviewed Image.dis: the whole step is the Python _step (no dis call first)
const S_PY_ENTRY: usize = 16; // step / _step / run-iteration routed to the Python code before any engine work (overridden helper, attribute hooks)

/// bumped whenever the Python <-> Rust contract of this module changes (rs_shim checks it)
const API: u32 = 3;

#[inline]
fn stat(i: usize) {
    STATS.with(|s| s.borrow_mut()[i] += 1);
}

fn cfg(py: Python<'_>) -> PyResult<&'static Cfg> {
    CFG.get(py).ok_or_else(|| PyRuntimeError::new_err("_rs_emu not configured"))
}

#[inline]
fn type_lookup(tp: *mut ffi::PyTypeObject, name: &Bound<'_, PyString>) -> usize {
    unsafe { _PyType_Lookup(tp, name.as_ptr()) as usize }
}

fn tinfo(py: Python<'_>, c: &Cfg, s: &Bound<'_, PyAny>) -> TInfo {
    let tp = unsafe { ffi::Py_TYPE(s.as_ptr()) };
    let key = tp as usize;
    let tag = unsafe { (*tp).tp_version_tag };
    if tag != 0 {
        if let Some(t) = TCACHE.with(|m| m.borrow().get(&key).and_then(|(g, t)| if *g == tag { Some(*t) } else { None })) {
            return t;
        }
    }
    let mut t = TInfo { helpers_ok: true, ..Default::default() };
    for (nm, exp) in c.helper_names.iter().zip(c.helper_exp.iter()) {
        if type_lookup(tp, nm.bind(py)) != *exp {
            t.helpers_ok = false;
        }
    }
    t.ld_ours = type_lookup(tp, intern!(py, "ld")) == c.our_ld.as_ptr() as usize;
    t.st_ours = type_lookup(tp, intern!(py, "st")) == c.our_st.as_ptr() as usize;
    t.step_ours = type_lookup(tp, intern!(py, "step")) == c.our_step.as_ptr() as usize;
    t.istep_ours = type_lookup(tp, intern!(py, "_step")) == c.our_istep.as_ptr() as usize;
    unsafe {
        let ga = (*tp).tp_getattro.map(|f| f as usize);
        let sa = (*tp).tp_setattro.map(|f| f as usize);
        // compare with object's own slots: on Windows `ffi::PyObject_GenericGetAttr as usize` is the import thunk, never equal to the slot
        let base = std::ptr::addr_of_mut!(ffi::PyBaseObject_Type);
        let (oga, osa) = ((*base).tp_getattro.map(|f| f as usize), (*base).tp_setattro.map(|f| f as usize));
        let generic = ga.is_some() && ga == oga && sa.is_some() && sa == osa;
        let mut plain = true;
        let mut desc = false;
        for nm in c.attr_names.iter() {
            let p = type_lookup(tp, nm.bind(py));
            if p != 0 {
                plain = false;
                let at = ffi::Py_TYPE(p as *mut ffi::PyObject);
                if (*at).tp_descr_get.is_some() || (*at).tp_descr_set.is_some() {
                    desc = true;
                }
            }
        }
        t.fast_attrs = generic && plain;
        t.hooks = !generic || desc;
    }
    let tag2 = unsafe { (*tp).tp_version_tag };
    if tag2 != 0 {
        TCACHE.with(|m| m.borrow_mut().insert(key, (tag2, t)));
    }
    t
}

/// instance __dict__ (None for objects without one)
fn inst_dict<'py>(py: Python<'py>, s: &Bound<'py, PyAny>) -> Option<Bound<'py, PyDict>> {
    unsafe {
        let d = ffi::PyObject_GenericGetDict(s.as_ptr(), std::ptr::null_mut());
        if d.is_null() {
            ffi::PyErr_Clear();
            return None;
        }
        Bound::from_owned_ptr(py, d).cast_into::<PyDict>().ok()
    }
}

#[inline]
fn dict_has(d: &Bound<'_, PyDict>, k: &Bound<'_, PyString>) -> bool {
    unsafe { ffi::PyDict_Contains(d.as_ptr(), k.as_ptr()) == 1 } // str keys: no error possible
}

/// which semantics apply to this CPU object right now
#[derive(Clone, Copy)]
struct Mode {
    full_py: bool, // overridden helper, or attribute hooks / state-named descriptors (TInfo::hooks): the Python step / _step / run body runs, no engine
    ld_nat: bool,
    st_nat: bool,
    step_ours: bool,
    istep_ours: bool,
    fast_attrs: bool,
}

/// CPython's PyMethodObject (Include/cpython/classobject.h): head, im_func, im_self
#[repr(C)]
struct PyMethodObject {
    ob_base: ffi::PyObject,
    im_func: *mut ffi::PyObject,
    im_self: *mut ffi::PyObject,
}

static METHOD_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();

/// `s.<name>` is the bound method of function `f` on `s` (i.e. not shadowed by an instance attribute)
fn bound_is(s: &Bound<'_, PyAny>, name: &Bound<'_, PyString>, f: &Py<PyAny>) -> bool {
    let py = s.py();
    let mt = match METHOD_TYPE.get_or_try_init(py, || -> PyResult<Py<PyType>> { Ok(py.import("types")?.getattr("MethodType")?.cast_into::<PyType>()?.unbind()) }) {
        Ok(t) => t.as_ptr() as *mut ffi::PyTypeObject,
        Err(_) => return false,
    };
    match s.getattr(name) {
        Ok(b) => unsafe {
            let p = b.as_ptr();
            ffi::Py_TYPE(p) == mt && (*(p as *mut PyMethodObject)).im_func == f.as_ptr() && (*(p as *mut PyMethodObject)).im_self == s.as_ptr()
        },
        Err(_) => false,
    }
}

fn mode(py: Python<'_>, c: &Cfg, s: &Bound<'_, PyAny>) -> Mode {
    let t = tinfo(py, c, s);
    let mut m = Mode { full_py: !t.helpers_ok || t.hooks, ld_nat: t.ld_ours, st_nat: t.st_ours, step_ours: t.step_ours, istep_ours: t.istep_ours, fast_attrs: t.fast_attrs };
    if m.full_py && !t.hooks {
        // Python semantics for every step anyway: decide step / _step by plain attribute lookup (no side effects without hooks) and never
        // touch __dict__ - materializing it makes every later Python attribute access of this object ~12 % slower (measured, 3.14)
        m.step_ours = m.step_ours && bound_is(s, intern!(py, "step"), &c.our_step);
        m.istep_ours = m.istep_ours && bound_is(s, intern!(py, "_step"), &c.our_istep);
        return m;
    }
    if let Some(d) = inst_dict(py, s) {
        for nm in c.helper_names.iter() {
            if dict_has(&d, nm.bind(py)) {
                m.full_py = true;
            }
        }
        if dict_has(&d, intern!(py, "ld")) {
            m.ld_nat = false;
        }
        if dict_has(&d, intern!(py, "st")) {
            m.st_nat = false;
        }
        if dict_has(&d, intern!(py, "step")) {
            m.step_ours = false;
        }
        if dict_has(&d, intern!(py, "_step")) {
            m.istep_ours = false;
        }
    }
    m
}

#[inline]
fn as_u32(o: &Bound<'_, PyAny>) -> Option<u32> {
    match exact_i64(o) {
        Some(v) if (0..=M32 as i64).contains(&v) => Some(v as u32),
        _ => None,
    }
}

#[inline]
fn pybool<'py>(py: Python<'py>, b: bool) -> Bound<'py, PyAny> {
    PyBool::new(py, b).to_owned().into_any()
}

#[inline]
fn exact_bool(o: &Bound<'_, PyAny>) -> Option<bool> {
    let p = o.as_ptr();
    unsafe {
        if p == ffi::Py_True() {
            Some(true)
        } else if p == ffi::Py_False() {
            Some(false)
        } else {
            None
        }
    }
}

fn changed<T>(what: &str) -> PyResult<T> {
    Err(PyRuntimeError::new_err(format!(
        "rs_emu: {} is not a u32 int after a Python callback inside a native step (pyemu would compute with it; run with OMH_RUST=0)",
        what
    )))
}

// ---- memory for the Python-level CPU.ld / CPU.st / CPU.load entry points ---------------------------------------------------------
fn trap_check(py: Python<'_>, c: &Cfg, s: &Bound<'_, PyAny>, adm: u64, n: i64, n_obj: &Bound<'_, PyAny>, what: &str) -> PyResult<()> {
    // `if self._trap and n > 1 and (ad & M32) & (n - 1): raise AlignFault('misaligned %s u%d at %#x (pc=%#x)' % (what, 8 * n, ad & M32, self.pc))`
    if s.getattr(intern!(py, "_trap"))?.is_truthy()? && n > 1 && (adm & (n as u64 - 1)) != 0 {
        let msg = c.trapmsg.bind(py).call1((what, n_obj, adm, s.getattr(intern!(py, "pc"))?))?;
        return Err(PyErr::from_value(c.align_fault.bind(py).call1((msg,))?));
    }
    Ok(())
}

struct ImgView<'py> {
    d: Option<Bound<'py, PyAny>>,
    buf: Option<pyo3::buffer::PyBuffer<u8>>,
}

impl<'py> ImgView<'py> {
    fn new() -> Self {
        ImgView { d: None, buf: None }
    }
    /// `d[o] if 0 <= o < len(d) and a >= img_lo else 0`, d = img.d; None -> not a byte (caller delegates to Python)
    fn byte(&mut self, py: Python<'py>, c: &Cfg, img: &Bound<'py, PyAny>, a: i64) -> PyResult<Option<u8>> {
        let o = a - c.base;
        if o < 0 {
            return Ok(Some(0));
        }
        if self.d.is_none() {
            let d = img.getattr(intern!(py, "d"))?;
            self.buf = pyo3::buffer::PyBuffer::<u8>::get(&d).ok().filter(|b| b.is_c_contiguous() && b.dimensions() == 1);
            self.d = Some(d);
        }
        if let Some(b) = &self.buf {
            let len = b.item_count() as i64;
            if !(o < len && a >= c.img_lo) {
                return Ok(Some(0));
            }
            let p = b.buf_ptr() as *const u8;
            return Ok(Some(unsafe { *p.add(o as usize) }));
        }
        let d = self.d.as_ref().unwrap();
        if !(o < d.len()? as i64 && a >= c.img_lo) {
            return Ok(Some(0));
        }
        let v = d.get_item(o)?;
        Ok(mem::byte_val(&v))
    }
}

/// native body of CPU.ld (self.mem a MemCore without odd keys, or an exact dict); Ok(None) -> delegate to the Python ld
fn ld_body(py: Python<'_>, c: &Cfg, s: &Bound<'_, PyAny>, adm: u64, n: i64) -> PyResult<Option<u64>> {
    let m = s.getattr(intern!(py, "mem"))?;
    let mut img = ImgView::new();
    let mut imgo: Option<Bound<'_, PyAny>> = None;
    let mut v: u64 = 0;
    let is_core = unsafe { ffi::Py_TYPE(m.as_ptr()) } == c.rpm_type.as_ptr() as *mut ffi::PyTypeObject;
    let core = if is_core { Some(m.cast::<MemCore>()?.borrow()) } else { None };
    let dict = if is_core { None } else { m.cast_exact::<PyDict>().ok() };
    if let Some(core) = &core {
        if !core.odd_empty(py) {
            return Ok(None);
        }
    } else if dict.is_none() {
        return Ok(None);
    }
    for i in 0..n {
        let a = adm as i64 + i;
        let have = if let Some(core) = &core {
            core.byte(a)
        } else {
            match dict.as_ref().unwrap().get_item(pyint(py, a))? {
                Some(x) if !x.is_none() => match mem::byte_val(&x) {
                    Some(b) => Some(b),
                    None => return Ok(None),
                },
                _ => None,
            }
        };
        let b = match have {
            Some(b) => b,
            None => {
                if imgo.is_none() {
                    imgo = Some(s.getattr(intern!(py, "img"))?);
                }
                match img.byte(py, c, imgo.as_ref().unwrap(), a)? {
                    Some(b) => b,
                    None => return Ok(None),
                }
            }
        };
        v |= (b as u64) << (8 * i);
    }
    Ok(Some(v))
}

/// native body of CPU.st (stores + the write audit entry); false -> delegate to the Python st (nothing done yet)
fn st_body(py: Python<'_>, s: &Bound<'_, PyAny>, c: &Cfg, adm: u64, n: i64, n_obj: &Bound<'_, PyAny>, v: u64) -> PyResult<bool> {
    let m = s.getattr(intern!(py, "mem"))?;
    if unsafe { ffi::Py_TYPE(m.as_ptr()) } == c.rpm_type.as_ptr() as *mut ffi::PyTypeObject {
        let mut core = m.cast::<MemCore>()?.borrow_mut();
        if !core.odd_empty(py) {
            return Ok(false);
        }
        core.store_n(adm as i64, n, v); // == the shm-patched PageMem.store (adm < 2**32: always in range)
    } else if let Ok(d) = m.cast_exact::<PyDict>() {
        for i in 0..n {
            d.set_item(pyint(py, adm as i64 + i), pyint(py, ((v >> (8 * i)) & 0xff) as i64))?;
        }
    } else {
        return Ok(false);
    }
    let w = s.getattr(intern!(py, "writes"))?;
    let ent = PyTuple::new(py, [pyint(py, adm as i64), n_obj.clone(), s.getattr(intern!(py, "pc"))?])?;
    if let Ok(l) = w.cast_exact::<PyList>() {
        l.append(ent)?;
    } else {
        w.call_method1(intern!(py, "append"), (ent,))?;
    }
    Ok(true)
}

/// decoded op for a dis() entry (cached per entry object; validated against the entry's operand list each hit)
fn decoded(ent: &Bound<'_, PyTuple>) -> PyResult<(Arc<Op>, Option<i64>)> {
    let key = ent.as_ptr() as usize;
    let hit = DCACHE.with(|m| {
        let m = m.borrow();
        let e = m.get(&key)?;
        let o = e.o.bind(ent.py());
        if let Ok(l) = o.cast_exact::<PyList>() {
            if l.len() != e.items.len() {
                return None;
            }
            for (i, it) in e.items.iter().enumerate() {
                if unsafe { ffi::PyList_GET_ITEM(l.as_ptr(), i as ffi::Py_ssize_t) } != it.as_ptr() {
                    return None;
                }
            }
        }
        Some((e.op.clone(), e.sz))
    });
    if let Some(h) = hit {
        return Ok(h);
    }
    stat(S_DECODE);
    let mn = ent.get_item(0)?;
    let o = ent.get_item(1)?;
    let szo = ent.get_item(2)?;
    let sz = exact_i64(&szo).filter(|v| v.abs() < (1i64 << 40));
    let mut items: Vec<Py<PyAny>> = Vec::new();
    let mut strs: Vec<String> = Vec::new();
    let mut ok = mn.is_exact_instance_of::<PyString>() && sz.is_some();
    let olist = o.cast_exact::<PyList>().ok().map(|l| l.iter().collect::<Vec<_>>()).or_else(|| o.cast_exact::<PyTuple>().ok().map(|t| t.iter().collect()));
    match olist {
        Some(v) => {
            for it in v {
                if it.is_exact_instance_of::<PyString>() {
                    strs.push(it.extract::<String>()?);
                } else {
                    ok = false;
                }
                items.push(it.unbind());
            }
        }
        None => ok = false,
    }
    let op = if ok { decode(&mn.extract::<String>()?, &strs) } else { Op::Unsupported };
    let op = Arc::new(op);
    DCACHE.with(|m| {
        let mut m = m.borrow_mut();
        if m.len() > 262_144 {
            m.clear();
        }
        m.insert(key, DEnt { _tup: ent.clone().unbind(), o: o.clone().unbind(), items, sz, op: op.clone() });
    });
    Ok((op, sz))
}

// ---- python entry points ----------------------------------------------------------------------------------------------------
/// CPU._step
#[pyfunction]
fn step_inner(py: Python<'_>, s: &Bound<'_, PyAny>) -> PyResult<()> {
    eng::step_inner(py, cfg(py)?, s)
}

/// CPU.step
#[pyfunction]
fn step(py: Python<'_>, s: &Bound<'_, PyAny>) -> PyResult<()> {
    eng::step(py, cfg(py)?, s)
}

/// CPU.run(start, stop, max_steps)
#[pyfunction]
fn run<'py>(py: Python<'py>, s: &Bound<'py, PyAny>, start: &Bound<'py, PyAny>, stop: &Bound<'py, PyAny>, max_steps: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    eng::run(py, cfg(py)?, s, start, stop, max_steps)
}

/// CPU.ld(ad, n)
#[pyfunction]
fn ld<'py>(py: Python<'py>, s: &Bound<'py, PyAny>, ad: &Bound<'py, PyAny>, n: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let c = cfg(py)?;
    if let (true, Some(nn)) = (ad.is_exact_instance_of::<PyInt>(), exact_i64(n)) {
        if nn <= 8 {
            let adm = unsafe { ffi::PyLong_AsUnsignedLongLongMask(ad.as_ptr()) } & M32;
            trap_check(py, c, s, adm, nn, n, "load")?;
            if nn <= 0 {
                let _ = s.getattr(intern!(py, "mem"))?;
                return Ok(pyint(py, 0));
            }
            if let Some(v) = ld_body(py, c, s, adm, nn)? {
                return Ok(unsafe { Bound::from_owned_ptr(py, ffi::PyLong_FromUnsignedLongLong(v)) });
            }
        }
    }
    stat(S_H_LD);
    c.h_ld.bind(py).call1((s, ad, n))
}

/// CPU.st(ad, n, v)
#[pyfunction]
fn st(py: Python<'_>, s: &Bound<'_, PyAny>, ad: &Bound<'_, PyAny>, n: &Bound<'_, PyAny>, v: &Bound<'_, PyAny>) -> PyResult<()> {
    let c = cfg(py)?;
    if let (true, Some(nn), true) = (ad.is_exact_instance_of::<PyInt>(), exact_i64(n), v.is_exact_instance_of::<PyInt>()) {
        if nn <= 8 {
            let adm = unsafe { ffi::PyLong_AsUnsignedLongLongMask(ad.as_ptr()) } & M32;
            let w = unsafe { ffi::PyLong_AsUnsignedLongLongMask(v.as_ptr()) };
            trap_check(py, c, s, adm, nn, n, "store")?;
            if st_body(py, s, c, adm, nn.max(0), n, w)? {
                return Ok(());
            }
        }
    }
    stat(S_H_ST);
    c.h_st.bind(py).call1((s, ad, n, v))?;
    Ok(())
}

/// CPU.load(ad, data)
#[pyfunction]
fn load(py: Python<'_>, s: &Bound<'_, PyAny>, ad: &Bound<'_, PyAny>, data: &Bound<'_, PyAny>) -> PyResult<()> {
    let c = cfg(py)?;
    let m = s.getattr(intern!(py, "mem"))?;
    if unsafe { ffi::Py_TYPE(m.as_ptr()) } == c.rpm_type.as_ptr() as *mut ffi::PyTypeObject
        && ad.is_exact_instance_of::<PyInt>()
        && (data.is_exact_instance_of::<pyo3::types::PyBytes>() || data.is_exact_instance_of::<pyo3::types::PyByteArray>())
    {
        let mut core = m.cast::<MemCore>()?.borrow_mut();
        if core.odd_empty(py) {
            return mem_put_bytes(py, &mut core, ad, data);
        }
    }
    c.h_load.bind(py).call1((s, ad, data))?;
    Ok(())
}

fn mem_put_bytes(py: Python<'_>, core: &mut MemCore, ad: &Bound<'_, PyAny>, data: &Bound<'_, PyAny>) -> PyResult<()> {
    let raw: Vec<u8> = data.extract()?;
    if raw.is_empty() {
        return Ok(());
    }
    if let Some(a) = exact_i64(ad) {
        if a >= mem::LO && a.checked_add(raw.len() as i64).is_some_and(|e| e < mem::HI) {
            for (i, b) in raw.iter().enumerate() {
                core.set_byte(a + i as i64, *b);
            }
            return Ok(());
        }
    }
    for (i, b) in raw.iter().enumerate() {
        let k = ad.add(pyint(py, i as i64))?;
        core.setitem_obj(py, &k, &pyint(py, *b as i64))?;
    }
    Ok(())
}

#[pyfunction]
#[pyo3(signature = (**kw))]
fn configure(py: Python<'_>, kw: Option<&Bound<'_, PyDict>>) -> PyResult<()> {
    let kw = kw.ok_or_else(|| PyTypeError::new_err("configure(**kw)"))?;
    let g = |k: &str| -> PyResult<Bound<'_, PyAny>> { kw.get_item(k)?.ok_or_else(|| PyTypeError::new_err(format!("configure: missing {}", k))) };
    let helpers = g("helpers")?.cast_into::<PyDict>()?;
    let mut names = Vec::new();
    let mut exp = Vec::new();
    let mut keep = Vec::new();
    for h in HELPERS {
        let f = helpers.get_item(h)?.ok_or_else(|| PyTypeError::new_err(format!("configure: helper {} missing", h)))?;
        names.push(PyString::intern(py, h).unbind());
        exp.push(f.as_ptr() as usize);
        keep.push(f.unbind());
    }
    let base: i64 = g("base")?.extract()?;
    let img_lo: i64 = match kw.get_item("img_lo")? {
        Some(v) if !v.is_none() => v.extract()?,
        _ => base,
    };
    let c = Cfg {
        base,
        img_lo,
        align_fault: g("align_fault")?.cast_into::<PyType>()?.unbind(),
        trapmsg: g("trapmsg")?.unbind(),
        py_step: g("py_step")?.unbind(),
        h_ld: g("h_ld")?.unbind(),
        h_st: g("h_st")?.unbind(),
        h_load: g("h_load")?.unbind(),
        helper_names: names,
        helper_exp: exp,
        _helper_keep: keep,
        our_ld: g("our_ld")?.unbind(),
        our_st: g("our_st")?.unbind(),
        our_step: g("our_step")?.unbind(),
        our_istep: g("our_istep")?.unbind(),
        img_dis: g("img_dis")?.unbind(),
        image_cls: g("image_cls")?.cast_into::<PyType>()?.unbind(),
        rpm_type: g("rpm_type")?.cast_into::<PyType>()?.unbind(),
        dis_kind: g("dis_kind")?.unbind(),
        attr_names: ATTRS.iter().map(|a| PyString::intern(py, a).unbind()).collect(),
    };
    CFG.set(py, c).map_err(|_| PyRuntimeError::new_err("_rs_emu already configured"))
}

#[pyfunction]
fn configured(py: Python<'_>) -> bool {
    CFG.get(py).is_some()
}

#[pyfunction]
fn stats(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let d = PyDict::new(py);
    let names = [
        "native", "fb_helper", "fb_stub", "fb_pc", "fb_dis", "fb_op", "fb_state", "py_ld", "py_st", "h_ld", "h_st", "dis_call", "decode", "callbacks", "py_steps", "fb_route",
        "py_entry",
    ];
    STATS.with(|s| -> PyResult<()> {
        let s = s.borrow();
        for (i, n) in names.iter().enumerate() {
            d.set_item(*n, s[i])?;
        }
        Ok(())
    })?;
    d.set_item("dcache", DCACHE.with(|m| m.borrow().len()))?;
    Ok(d)
}

/// tests / tuning: ns per call of the engine pieces (eng::bench_parts)
#[pyfunction]
fn bench_parts(py: Python<'_>, s: &Bound<'_, PyAny>, n: usize) -> PyResult<Vec<(String, f64)>> {
    eng::bench_parts(py, cfg(py)?, s, n)
}

/// decode one (mn, ops) pair (tests): repr of the decoded op
#[pyfunction]
fn decode_repr(mn: &str, o: Vec<String>) -> String {
    format!("{:?}", decode(mn, &o))
}

#[pymodule]
fn _rs_emu(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<MemCore>()?;
    m.add_function(wrap_pyfunction!(configure, m)?)?;
    m.add_function(wrap_pyfunction!(configured, m)?)?;
    m.add_function(wrap_pyfunction!(step_inner, m)?)?;
    m.add_function(wrap_pyfunction!(step, m)?)?;
    m.add_function(wrap_pyfunction!(run, m)?)?;
    m.add_function(wrap_pyfunction!(ld, m)?)?;
    m.add_function(wrap_pyfunction!(st, m)?)?;
    m.add_function(wrap_pyfunction!(load, m)?)?;
    m.add_function(wrap_pyfunction!(stats, m)?)?;
    m.add_function(wrap_pyfunction!(decode_repr, m)?)?;
    m.add_function(wrap_pyfunction!(bench_parts, m)?)?;
    m.add("SRC_SHA", option_env!("RS_SRC_SHA").unwrap_or("unset"))?;
    m.add("API", API)?;
    Ok(())
}
