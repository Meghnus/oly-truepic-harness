//! MemCore: Rust port of mem/pagemem.PageMem (the exact-dict-semantics replacement of pyemu.CPU.mem).
//! 256-byte pages + presence bitmask + insertion-order log (one event per fresh insertion; deletes leave stale events) + `odd`
//! (a real Python dict) for every key / value a byte page cannot hold, with its own events (ODD sentinel) in the same log.
//! Every method below mirrors the PageMem method of the same name line by line; the slow `_ordered_keys` path, popitem, __eq__,
//! keys / values / items and __iter__ live in the Python subclass (emu.rs_shim.RPageMem) exactly as written in PageMem.
//! copy is copy-on-write (Arc pages, Arc::make_mut on the first write of either side), like PageMem's owner ids.

use pyo3::exceptions::{PyKeyError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyByteArray, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use pyo3::{ffi, intern};
use rustc_hash::FxHashMap;
use std::sync::Arc;

pub const LO: i64 = -(1i64 << 62);
pub const HI: i64 = 1i64 << 62;
pub const ODD: i64 = i64::MIN;

#[derive(Clone)]
pub struct Page {
    pub data: [u8; 256],
    pub mask: [u64; 4],
}

impl Page {
    fn new() -> Self {
        Page { data: [0; 256], mask: [0; 4] }
    }
    #[inline]
    pub fn has(&self, o: usize) -> bool {
        (self.mask[o >> 6] >> (o & 63)) & 1 != 0
    }
}

#[pyclass(subclass, mapping, module = "_rs_emu")]
pub struct MemCore {
    pub pages: FxHashMap<i64, Arc<Page>>,
    pub log: Vec<i64>,
    pub odd: Py<PyDict>,
    pub oddlog: Py<PyList>,
    pub n: i64,
    pub stale: i64,
    /// rs_dual only: undo journal of one step (pages as they were before their first change, log / oddlog lengths, n, stale, odd copy)
    pub journal: Option<Box<Journal>>,
}

pub struct Journal {
    pages: FxHashMap<i64, Option<Arc<Page>>>,
    log_len: usize,
    oddlog_len: usize,
    n: i64,
    stale: i64,
    odd: Option<Py<PyDict>>,
}

// ---- helpers on Python objects -------------------------------------------------------------------------------------------
/// exact `int` (not a subclass) that fits i64
#[inline]
pub fn exact_i64(o: &Bound<'_, PyAny>) -> Option<i64> {
    unsafe {
        let p = o.as_ptr();
        if ffi::PyLong_CheckExact(p) == 0 {
            return None;
        }
        let mut of: std::os::raw::c_int = 0;
        let v = ffi::PyLong_AsLongLongAndOverflow(p, &mut of);
        if of != 0 {
            return None;
        }
        Some(v)
    }
}

/// `type(a) is int and _LO <= a < _HI`
#[inline]
pub fn range_int(o: &Bound<'_, PyAny>) -> Option<i64> {
    match exact_i64(o) {
        Some(v) if (LO..HI).contains(&v) => Some(v),
        _ => None,
    }
}

/// `type(v) is int and 0 <= v < 256`
#[inline]
pub fn byte_val(o: &Bound<'_, PyAny>) -> Option<u8> {
    match exact_i64(o) {
        Some(v) if (0..256).contains(&v) => Some(v as u8),
        _ => None,
    }
}

#[inline]
pub fn pyint<'py>(py: Python<'py>, v: i64) -> Bound<'py, PyAny> {
    unsafe { Bound::from_owned_ptr(py, ffi::PyLong_FromLongLong(v)) }
}

/// mem.pagemem._key
pub fn norm_key<'py>(a: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    if a.is_exact_instance_of::<PyInt>() {
        return Ok(a.clone());
    }
    if a.is_instance_of::<PyFloat>() || a.is_instance_of::<PyString>() || a.is_instance_of::<PyBytes>() || a.is_instance_of::<PyTuple>() || a.is_none() {
        return Ok(a.clone());
    }
    let py = a.py();
    let ix = match a.getattr(intern!(py, "__index__")) {
        Ok(ix) => ix,
        Err(e) if e.is_instance_of::<pyo3::exceptions::PyAttributeError>(py) => return Ok(a.clone()),
        Err(e) => return Err(e),
    };
    match ix.call0() {
        Ok(r) => Ok(r),
        Err(e) if e.is_instance_of::<pyo3::exceptions::PyException>(py) => Ok(a.clone()),
        Err(e) => Err(e),
    }
}

pub fn key_error(py: Python<'_>, a: &Bound<'_, PyAny>) -> PyErr {
    match py.get_type::<PyKeyError>().call1((a,)) {
        Ok(e) => PyErr::from_value(e),
        Err(e) => e,
    }
}

impl MemCore {
    pub fn empty(py: Python<'_>) -> Self {
        MemCore { pages: FxHashMap::default(), log: Vec::new(), odd: PyDict::new(py).unbind(), oddlog: PyList::empty(py).unbind(), n: 0, stale: 0, journal: None }
    }

    /// log.push with ~12.5 % growth steps (PageMem's array('q') over-allocates about that much; Vec would double)
    #[inline]
    fn log_push(log: &mut Vec<i64>, a: i64) {
        if log.len() == log.capacity() {
            log.reserve_exact((log.len() / 8).max(1024));
        }
        log.push(a);
    }

    #[inline]
    pub fn odd_empty(&self, py: Python<'_>) -> bool {
        self.odd.bind(py).len() == 0
    }

    #[inline]
    pub fn byte(&self, a: i64) -> Option<u8> {
        let p = self.pages.get(&(a >> 8))?;
        let o = (a & 255) as usize;
        if p.has(o) { Some(p.data[o]) } else { None }
    }

    #[inline]
    pub fn present(&self, a: i64) -> bool {
        self.byte(a).is_some()
    }

    /// record a page's pre-change state in the rs_dual journal (no-op unless journaling)
    #[inline]
    fn touch(&mut self, pno: i64) {
        if let Some(j) = self.journal.as_mut() {
            if !j.pages.contains_key(&pno) {
                j.pages.insert(pno, self.pages.get(&pno).cloned());
            }
        }
    }

    /// the byte-page branch of __setitem__ (caller: a in range, odd does not hold a)
    #[inline]
    pub fn set_byte(&mut self, a: i64, v: u8) {
        self.touch(a >> 8);
        let p = Arc::make_mut(self.pages.entry(a >> 8).or_insert_with(|| Arc::new(Page::new())));
        let o = (a & 255) as usize;
        let bit = 1u64 << (o & 63);
        if p.mask[o >> 6] & bit == 0 {
            p.mask[o >> 6] |= bit;
            self.n += 1;
            Self::log_push(&mut self.log, a);
        }
        p.data[o] = v;
    }

    /// PageMem.store for n in 0..=8, ad and ad + n in range, odd empty: the bytes in order; a page store (o + n <= 256) creates its
    /// page even for n == 0 (PageMem `_page()`), which `load` / the page count in repr can observe
    #[inline]
    pub fn store_n(&mut self, a: i64, n: i64, w: u64) {
        if n == 0 {
            self.touch(a >> 8);
            self.pages.entry(a >> 8).or_insert_with(|| Arc::new(Page::new()));
            return;
        }
        for i in 0..n {
            self.set_byte(a + i, ((w >> (8 * i)) & 0xff) as u8);
        }
    }

    /// PageMem.get without the default: Ok(None) = absent
    pub fn get_opt<'py>(&self, py: Python<'py>, a0: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
        let a = if a0.is_exact_instance_of::<PyInt>() { a0.clone() } else { norm_key(a0)? };
        let odd = self.odd.bind(py);
        if odd.len() > 0 {
            if let Some(r) = odd.get_item(&a)? {
                return Ok(Some(r));
            }
        }
        if let Some(v) = exact_i64(&a) {
            if let Some(b) = self.byte(v) {
                return Ok(Some(pyint(py, b as i64)));
            }
        }
        Ok(None)
    }

    pub fn setitem_obj(&mut self, py: Python<'_>, a0: &Bound<'_, PyAny>, v: &Bound<'_, PyAny>) -> PyResult<()> {
        let a = if a0.is_exact_instance_of::<PyInt>() { a0.clone() } else { norm_key(a0)? };
        let ai = range_int(&a);
        let odd = self.odd.bind(py).clone();
        if let Some(ai) = ai {
            if let Some(vb) = byte_val(v) {
                if odd.len() > 0 && odd.contains(&a)? {
                    if self.present(ai) {
                        odd.del_item(&a)?; // a shadowing odd value is replaced by the byte
                    } else {
                        odd.set_item(&a, v)?; // an odd-only key keeps its place (and stays odd)
                        return Ok(());
                    }
                }
                self.set_byte(ai, vb);
                return Ok(());
            }
            if self.present(ai) {
                odd.set_item(&a, v)?; // shadow: the byte entry keeps the key's dict position
                return Ok(());
            }
        }
        if !odd.contains(&a)? {
            self.oddlog.bind(py).append(&a)?;
            Self::log_push(&mut self.log, ODD);
            self.n += 1;
        }
        odd.set_item(&a, v)
    }

    pub fn delitem_obj(&mut self, py: Python<'_>, a0: &Bound<'_, PyAny>) -> PyResult<()> {
        let a = norm_key(a0)?;
        let mut found = false;
        if let Some(ai) = range_int(&a) {
            if self.present(ai) {
                self.touch(ai >> 8);
                let p = Arc::make_mut(self.pages.get_mut(&(ai >> 8)).unwrap());
                let o = (ai & 255) as usize;
                p.mask[o >> 6] &= !(1u64 << (o & 63));
                p.data[o] = 0;
                self.n -= 1;
                self.stale += 1;
                found = true;
            }
        }
        let odd = self.odd.bind(py);
        if odd.contains(&a)? {
            odd.del_item(&a)?;
            if !found {
                self.n -= 1;
                self.stale += 1;
            }
            found = true;
        }
        if !found {
            return Err(key_error(py, &a));
        }
        Ok(())
    }

    pub fn cow_clone(&self, py: Python<'_>) -> PyResult<MemCore> {
        Ok(MemCore {
            pages: self.pages.clone(),
            log: self.log.clone(),
            odd: self.odd.bind(py).copy()?.unbind(),
            oddlog: PyList::new(py, self.oddlog.bind(py).iter())?.unbind(),
            n: self.n,
            stale: self.stale,
            journal: None,
        })
    }
}

#[pymethods]
impl MemCore {
    #[new]
    #[pyo3(signature = (*_args, **_kw))]
    fn py_new(py: Python<'_>, _args: &Bound<'_, PyTuple>, _kw: Option<&Bound<'_, PyDict>>) -> Self {
        MemCore::empty(py) // init (PageMem(init)) is applied by the Python subclass __init__ via update()
    }

    #[pyo3(signature = (a, default=None))]
    fn get<'py>(&self, py: Python<'py>, a: &Bound<'py, PyAny>, default: Option<Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyAny>> {
        Ok(match self.get_opt(py, a)? {
            Some(r) => r,
            None => default.unwrap_or_else(|| py.None().into_bound(py)),
        })
    }

    fn __getitem__<'py>(&self, py: Python<'py>, a: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        match self.get_opt(py, a)? {
            Some(r) => Ok(r),
            None => Err(key_error(py, a)),
        }
    }

    fn __contains__(&self, py: Python<'_>, a: &Bound<'_, PyAny>) -> PyResult<bool> {
        Ok(self.get_opt(py, a)?.is_some())
    }

    fn __setitem__(&mut self, py: Python<'_>, a: &Bound<'_, PyAny>, v: &Bound<'_, PyAny>) -> PyResult<()> {
        self.setitem_obj(py, a, v)
    }

    fn __delitem__(&mut self, py: Python<'_>, a: &Bound<'_, PyAny>) -> PyResult<()> {
        self.delitem_obj(py, a)
    }

    fn __len__(&self) -> usize {
        self.n as usize
    }

    fn __repr__(&self) -> String {
        format!("PageMem({} keys, {} pages)", self.n, self.pages.len())
    }

    /// the fast branch of PageMem._ordered_keys (`not self.stale and not self.odd` -> list(self.log)); None -> the caller runs the slow path
    fn _fast_keys<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyList>>> {
        if self.stale == 0 && self.odd_empty(py) {
            return Ok(Some(PyList::new(py, self.log.iter())?));
        }
        Ok(None)
    }

    /// the raw event log (ODD sentinel = -2**63), for the slow _ordered_keys path
    fn _log_list<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        PyList::new(py, self.log.iter())
    }

    fn pages_get_present<'py>(&self, py: Python<'py>, k: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyTuple>> {
        if k.is_exact_instance_of::<PyInt>() {
            if let Some(v) = exact_i64(k) {
                if self.present(v) {
                    return PyTuple::new(py, [k]);
                }
            }
        }
        Ok(PyTuple::empty(py))
    }

    fn clear(&mut self, py: Python<'_>) {
        *self = MemCore::empty(py);
    }

    /// make self a copy-on-write clone of other (PageMem.copy: pages shared until a side writes)
    fn _cow_from(&mut self, py: Python<'_>, other: PyRef<'_, MemCore>) -> PyResult<()> {
        let c = other.cow_clone(py)?;
        *self = c;
        Ok(())
    }

    #[getter]
    fn n(&self) -> i64 {
        self.n
    }
    #[getter]
    fn stale(&self) -> i64 {
        self.stale
    }
    #[getter]
    fn odd<'py>(&self, py: Python<'py>) -> Bound<'py, PyDict> {
        self.odd.bind(py).clone()
    }
    #[getter]
    fn oddlog<'py>(&self, py: Python<'py>) -> Bound<'py, PyList> {
        self.oddlog.bind(py).clone()
    }
    #[getter]
    fn npages(&self) -> usize {
        self.pages.len()
    }

    /// PageMem.store: exact equivalent of `for i in range(n): mem[ad + i] = (v >> 8*i) & 0xff`
    fn store(&mut self, py: Python<'_>, ad: &Bound<'_, PyAny>, n: &Bound<'_, PyAny>, v: &Bound<'_, PyAny>) -> PyResult<()> {
        if let (Some(a), Some(nn), true) = (exact_i64(ad), exact_i64(n), v.is_exact_instance_of::<PyInt>()) {
            if nn < 0 && a >= LO && a.checked_add(nn).is_some_and(|e| e < HI) && self.odd_empty(py) && (a & 255) + nn <= 256 {
                self.touch(a >> 8);
                self.pages.entry(a >> 8).or_insert_with(|| Arc::new(Page::new())); // `p = self._page(...)` runs first
                return Err(PyValueError::new_err("negative shift count")); // then ((1 << n) - 1) << o raises
            }
            if (0..=8).contains(&nn) && a >= LO && a.checked_add(nn).is_some_and(|e| e < HI) && self.odd_empty(py) {
                let w = unsafe { ffi::PyLong_AsUnsignedLongLongMask(v.as_ptr()) };
                self.store_n(a, nn, w);
                return Ok(());
            }
        }
        let nn: i64 = n.extract()?;
        let ff = pyint(py, 0xff);
        for i in 0..nn.max(0) {
            let k = ad.add(pyint(py, i))?;
            let b = v.rshift(pyint(py, 8 * i))?.bitand(&ff)?;
            self.setitem_obj(py, &k, &b)?;
        }
        Ok(())
    }

    /// PageMem.put_bytes: exact equivalent of `for i, b in enumerate(data): mem[ad + i] = b` for bytes / bytearray data
    fn put_bytes(&mut self, py: Python<'_>, ad: &Bound<'_, PyAny>, data: &Bound<'_, PyAny>) -> PyResult<()> {
        let n = data.len()? as i64;
        if n == 0 {
            return Ok(());
        }
        let raw: Option<Vec<u8>> = if let Ok(b) = data.cast::<PyBytes>() {
            Some(b.as_bytes().to_vec())
        } else if let Ok(b) = data.cast::<PyByteArray>() {
            Some(b.to_vec())
        } else {
            None
        };
        if let (Some(a), Some(raw)) = (exact_i64(ad), raw.as_ref()) {
            if a >= LO && a.checked_add(n).is_some_and(|e| e < HI) && self.odd_empty(py) {
                for (i, b) in raw.iter().enumerate() {
                    self.set_byte(a + i as i64, *b);
                }
                return Ok(());
            }
        }
        for (i, b) in data.try_iter()?.enumerate() {
            let k = ad.add(pyint(py, i as i64))?;
            self.setitem_obj(py, &k, &b?)?;
        }
        Ok(())
    }

    /// PageMem.load: (value, True) when every byte of [ad, ad+n) is present in ONE page, else (None, False)
    fn load<'py>(&self, py: Python<'py>, ad: &Bound<'py, PyAny>, n: i64) -> PyResult<Bound<'py, PyTuple>> {
        let none = || PyTuple::new(py, [py.None().into_bound(py), pyo3::types::PyBool::new(py, false).to_owned().into_any()]);
        let ad = match exact_i64(ad) {
            Some(v) => v,
            None => {
                ad.rshift(8)?; // `ad >> PB` (TypeError for a non-int); an int outside i64 has no page
                return none();
            }
        };
        if let Some(p) = self.pages.get(&(ad >> 8)) {
            let o = ad & 255;
            if o + n <= 256 {
                if n < 0 {
                    return Err(PyValueError::new_err("negative shift count"));
                }
                let all = (o..o + n).all(|x| p.has(x as usize));
                if all {
                    let bytes = PyBytes::new(py, &p.data[o as usize..(o + n) as usize]);
                    let v = py.get_type::<PyInt>().call_method1(intern!(py, "from_bytes"), (bytes, "little"))?;
                    return PyTuple::new(py, [v, pyo3::types::PyBool::new(py, true).to_owned().into_any()]);
                }
            }
        }
        none()
    }

    /// rs_dual: start an undo journal (one step)
    fn _jstart(&mut self, py: Python<'_>) -> PyResult<()> {
        let odd = if self.odd_empty(py) { None } else { Some(self.odd.bind(py).copy()?.unbind()) };
        self.journal = Some(Box::new(Journal {
            pages: FxHashMap::default(),
            log_len: self.log.len(),
            oddlog_len: self.oddlog.bind(py).len(),
            n: self.n,
            stale: self.stale,
            odd,
        }));
        Ok(())
    }

    /// rs_dual: what changed since _jstart: (new log events, [(pno, data or None, mask0..3)] of every changed page sorted, n, stale,
    /// odd items, new oddlog entries).  Error if the journal was lost (memory cleared / replaced during the step).
    fn _jdelta<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
        let j = self.journal.as_ref().ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("rs_dual: memory journal lost (cleared / replaced during the step)"))?;
        let tail = PyList::new(py, self.log[j.log_len.min(self.log.len())..].iter())?;
        let mut keys: Vec<&i64> = j.pages.keys().collect();
        keys.sort();
        let pages = PyList::empty(py);
        for k in keys {
            match self.pages.get(k) {
                Some(p) => pages.append((*k, PyBytes::new(py, &p.data), p.mask[0], p.mask[1], p.mask[2], p.mask[3]))?,
                None => pages.append((*k, py.None(), 0u64, 0u64, 0u64, 0u64))?,
            }
        }
        let ol = self.oddlog.bind(py);
        let otail = ol.get_slice(j.oddlog_len.min(ol.len()), ol.len());
        let odd_items = PyList::new(py, self.odd.bind(py).items().iter())?;
        PyTuple::new(py, [tail.into_any(), pages.into_any(), pyint(py, self.n), pyint(py, self.stale), odd_items.into_any(), otail.into_any()])
    }

    /// rs_dual: undo everything since _jstart and stop journaling
    fn _jrestore(&mut self, py: Python<'_>) -> PyResult<()> {
        let j = self.journal.take().ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("rs_dual: memory journal lost (cleared / replaced during the step)"))?;
        for (k, old) in j.pages.into_iter() {
            match old {
                Some(p) => {
                    self.pages.insert(k, p);
                }
                None => {
                    self.pages.remove(&k);
                }
            }
        }
        self.log.truncate(j.log_len);
        self.n = j.n;
        self.stale = j.stale;
        let odd = self.odd.bind(py);
        odd.clear();
        if let Some(o) = j.odd {
            odd.update(o.bind(py).as_mapping())?;
        }
        let ol = self.oddlog.bind(py);
        let len = ol.len();
        if len > j.oddlog_len {
            ol.del_slice(j.oddlog_len, len)?;
        }
        Ok(())
    }

    /// rs_dual: keep the changes, stop journaling
    fn _jstop(&mut self) {
        self.journal = None;
    }

    fn journal_active(&self) -> bool {
        self.journal.is_some()
    }

    /// order-sensitive digest of the byte-page state (log events in order, every page's data + mask, n, stale, odd size) - rs_dual's
    /// cheap per-step memory comparison; two MemCores with equal digests hold the same bytes in the same insertion order
    fn _digest(&self, py: Python<'_>) -> u64 {
        use std::hash::{Hash, Hasher};
        let mut h = rustc_hash::FxHasher::default();
        self.log.hash(&mut h);
        let mut keys: Vec<&i64> = self.pages.keys().collect();
        keys.sort();
        for k in keys {
            let p = &self.pages[k];
            k.hash(&mut h);
            p.data.hash(&mut h);
            p.mask.hash(&mut h);
        }
        (self.n, self.stale, self.odd.bind(py).len()).hash(&mut h);
        h.finish()
    }

    /// complete page structure (pickling / deepcopy): (pages [(pno, data, mask0..3)], log, odd, oddlog, n, stale)
    fn _getstate<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
        let mut pg: Vec<(i64, Bound<'py, PyBytes>, u64, u64, u64, u64)> = Vec::with_capacity(self.pages.len());
        let mut keys: Vec<&i64> = self.pages.keys().collect();
        keys.sort();
        for k in keys {
            let p = &self.pages[k];
            pg.push((*k, PyBytes::new(py, &p.data), p.mask[0], p.mask[1], p.mask[2], p.mask[3]));
        }
        let pages = PyList::new(py, pg)?;
        let log = PyList::new(py, self.log.iter())?;
        PyTuple::new(py, [pages.into_any(), log.into_any(), self.odd.bind(py).clone().into_any(), self.oddlog.bind(py).clone().into_any(), pyint(py, self.n), pyint(py, self.stale)])
    }

    fn _setstate(&mut self, py: Python<'_>, st: &Bound<'_, PyTuple>) -> PyResult<()> {
        let mut m = MemCore::empty(py);
        for it in st.get_item(0)?.try_iter()? {
            let (k, data, m0, m1, m2, m3): (i64, Vec<u8>, u64, u64, u64, u64) = it?.extract()?;
            let mut p = Page::new();
            p.data.copy_from_slice(&data);
            p.mask = [m0, m1, m2, m3];
            m.pages.insert(k, Arc::new(p));
        }
        m.log = st.get_item(1)?.extract()?;
        m.odd = st.get_item(2)?.cast_into::<PyDict>()?.unbind();
        m.oddlog = st.get_item(3)?.cast_into::<PyList>()?.unbind();
        m.n = st.get_item(4)?.extract()?;
        m.stale = st.get_item(5)?.extract()?;
        *self = m;
        Ok(())
    }
}
