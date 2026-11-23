//! The execution engine: CPU.step / CPU._step / CPU.run with the CPU state cached in Rust.
//!
//! State (d / a / sp / mdr / pc / z n c v / lar / steps / _trap) is loaded from the Python object on first use, kept in Rust while only
//! native code runs, and written back (only what changed) by `flush()` before EVERY call into Python code and on every exit (normal or
//! raise).  After a Python call everything (methods, lists, memory object, image, stubs, the decoded-instruction map) is re-derived and
//! reloaded, exactly as the Python code would re-read it.  Python code therefore never observes the cache.

use super::*;
use rustc_hash::FxHashSet;

#[derive(Clone, Copy, PartialEq)]
enum MemKind {
    Core,
    Dict,
    Other,
}

#[derive(Clone, Copy)]
enum DisKind {
    Base,                  // pyemu.Image.dis: `if va in self.cache: return self.cache[va]` ...
    Split(i64, i64),       // a reviewed range-split override: outside [lo, lo+size) -> Image.dis; inside -> side cache dict, else call
    Other,                 // anything else: call img.dis(pc)
}

/// everything derived from the object graph (re-derived after any Python callback)
struct Env<'py> {
    m: Mode,
    idict: Option<Bound<'py, PyDict>>, // the instance dict when it IS the attribute store (Mode::fast_attrs)
    dl: Option<Bound<'py, PyList>>,
    al: Option<Bound<'py, PyList>>,
    mem: Option<Bound<'py, PyAny>>, // lazily: CPU.ld / CPU.st read self.mem when they run
    mk: MemKind,
    wl: Option<Bound<'py, PyList>>,
    tl: Option<Bound<'py, PyList>>,
    tlen: usize,
    stubs_dict: Option<Bound<'py, PyDict>>,
    img: Bound<'py, PyAny>,
    dk: DisKind,
    icache: Option<Bound<'py, PyDict>>,
    side: Option<Bound<'py, PyDict>>,
    align: Option<(Bound<'py, PyAny>, bool)>, // lazily (CPU.step only)
    imgv: ImgView<'py>,
    dec: FxHashMap<u32, (Bound<'py, PyTuple>, Arc<Op>, i64)>,
    route: bool, // the whole step must be the Python _step: d / a / writes / trace share a list object, or Image.dis is not one the engine mirrors
}

#[derive(Clone, Copy, PartialEq)]
enum Pc {
    U(u32),
    Obj, // a non-u32 pc object (kept in Vals::pc_obj)
}

/// the cached state (flags, lar and _trap are read lazily: the Python code reads them only where they are used)
struct Vals<'py> {
    canon: bool, // d, a, sp, mdr are all u32 ints
    d: [u32; 4],
    a: [u32; 4],
    sp: u32,
    mdr: u32,
    pc: Pc,
    pc_obj: Option<Bound<'py, PyAny>>,
    f_loaded: bool,
    f: [Option<bool>; 4], // z n c v; None = not an exact bool
    lar_loaded: bool,
    lar: Option<u32>,
    lar_obj: Option<Bound<'py, PyAny>>,
    steps: Option<i64>,
    trap: Option<Bound<'py, PyAny>>,
    trap_t: bool,
    // dirty bits
    dd: [bool; 4],
    da: [bool; 4],
    dsp: bool,
    dmdr: bool,
    dpc: bool,
    dflags: bool,
    dlar: bool,
    dsteps: bool,
    dtrap: bool,
}

pub(crate) enum Res {
    Done,
    Fallback,
}

pub(crate) struct Eng<'a, 'py> {
    py: Python<'py>,
    c: &'static Cfg,
    s: &'a Bound<'py, PyAny>,
    env: Option<Env<'py>>,
    v: Option<Vals<'py>>,
    pre_mode: Option<Mode>, // a Mode computed just before (no Python ran since): derive() reuses it
}

macro_rules! n {
    ($py:expr, $s:literal) => {
        intern!($py, $s)
    };
}

/// getattr: the instance dict directly when it is the attribute store (Mode::fast_attrs: generic getattro, no class attribute of
/// that name), else / when absent the generic getattr (same value or the same AttributeError)
#[inline]
fn ga<'py>(s: &Bound<'py, PyAny>, d: Option<&Bound<'py, PyDict>>, nm: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    if let Some(d) = d {
        unsafe {
            let p = ffi::PyDict_GetItemWithError(d.as_ptr(), nm.as_ptr()); // str key: NULL = absent (no error possible)
            if !p.is_null() {
                return Ok(Bound::from_borrowed_ptr(s.py(), p));
            }
        }
    }
    s.getattr(nm)
}

/// setattr (instance dict store under Mode::fast_attrs, which is what generic setattr does without a class attribute)
#[inline]
fn sa<'py>(s: &Bound<'py, PyAny>, d: Option<&Bound<'py, PyDict>>, nm: &Bound<'py, PyString>, v: &Bound<'py, PyAny>) -> PyResult<()> {
    match d {
        Some(d) => d.set_item(nm, v),
        None => s.setattr(nm, v),
    }
}

impl<'a, 'py> Eng<'a, 'py> {
    pub(crate) fn new(py: Python<'py>, c: &'static Cfg, s: &'a Bound<'py, PyAny>) -> Self {
        Eng { py, c, s, env: None, v: None, pre_mode: None }
    }

    // ---- python callbacks ----------------------------------------------------------------------------------------------------
    /// write the cached state back before Python code runs (and drop the image buffer view: Python may resize / close img.d)
    fn before_cb(&mut self) -> PyResult<()> {
        stat(S_CB);
        self.flush()?;
        if let Some(e) = self.env.as_mut() {
            e.imgv = ImgView::new();
        }
        Ok(())
    }

    /// after Python code ran: everything is re-derived / reloaded on next use
    fn after_cb(&mut self) {
        self.env = None;
        self.v = None;
    }

    fn call<T>(&mut self, f: impl FnOnce(&Self) -> PyResult<T>) -> PyResult<T> {
        self.before_cb()?;
        let r = f(self);
        self.after_cb();
        r
    }

    // ---- derive / load / flush ------------------------------------------------------------------------------------------------
    fn derive(&mut self) -> PyResult<()> {
        if self.env.is_some() {
            return Ok(());
        }
        let (py, c, s) = (self.py, self.c, self.s);
        let m = match self.pre_mode.take() {
            Some(m) => m,
            None => mode(py, c, s),
        };
        let idict = if m.fast_attrs { inst_dict(py, s) } else { None };
        let idr = idict.as_ref();
        let img = ga(s, idr, n!(py, "img"))?;
        // dis kind of the image's class (+ the instance must not carry its own `dis`)
        let tp = unsafe { ffi::Py_TYPE(img.as_ptr()) };
        let key = tp as usize;
        let tag = unsafe { (*tp).tp_version_tag };
        let disf = match if tag != 0 { ICACHE.with(|m| m.borrow().get(&key).and_then(|(g, f)| if *g == tag { Some(*f) } else { None })) } else { None } {
            Some(f) => f,
            None => {
                let f = type_lookup(tp, n!(py, "dis"));
                let tag2 = unsafe { (*tp).tp_version_tag };
                if tag2 != 0 {
                    ICACHE.with(|m| m.borrow_mut().insert(key, (tag2, f)));
                }
                f
            }
        };
        let own = inst_dict(py, &img);
        let own_dis = own.as_ref().is_some_and(|d| dict_has(d, n!(py, "dis")));
        let mut dk = DisKind::Other;
        let mut side = None;
        if !own_dis && disf != 0 {
            if disf == c.img_dis.as_ptr() as usize {
                dk = DisKind::Base;
            } else {
                let known = DKIND.with(|m| m.borrow().get(&disf).map(|(_, k)| k.as_ref().map(|(a, b, c2)| (a.clone_ref(py), *b, c2.clone_ref(py)))));
                let known = match known {
                    Some(k) => k,
                    None => {
                        // ask rs_shim once per function (pure Python: compares the function's source with the reviewed one)
                        let fobj = unsafe { Bound::from_borrowed_ptr(py, disf as *mut ffi::PyObject) };
                        let r = c.dis_kind.bind(py).call1((&fobj,))?;
                        let k = if r.is_none() {
                            None
                        } else {
                            let t = r.cast::<PyTuple>()?;
                            Some((t.get_item(0)?.unbind(), t.get_item(1)?.extract::<i64>()?, t.get_item(2)?.cast_into::<PyString>()?.unbind()))
                        };
                        DKIND.with(|m| m.borrow_mut().insert(disf, (fobj.unbind(), k.as_ref().map(|(a, b, c2)| (a.clone_ref(py), *b, c2.clone_ref(py))))));
                        k
                    }
                };
                if let Some((lo_spec, size, side_attr)) = known {
                    // lo_spec: an int, or ("glob", module_dict, name), or ("attr", name)
                    let lo_spec = lo_spec.bind(py);
                    let lo = if let Some(v) = exact_i64(lo_spec) {
                        Some(v)
                    } else if let Ok(t) = lo_spec.cast::<PyTuple>() {
                        let kind: String = t.get_item(0)?.extract()?;
                        let o = if kind == "glob" {
                            t.get_item(1)?.cast_into::<PyDict>()?.get_item(t.get_item(2)?)?
                        } else {
                            Some(img.getattr(t.get_item(1)?.cast_into::<PyString>()?)?)
                        };
                        o.and_then(|o| exact_i64(&o))
                    } else {
                        None
                    };
                    // the reviewed functions call `E.Image.dis` dynamically: the shortcut mirrors it only while that is still the base dis
                    let base_ok = type_lookup(c.image_cls.as_ptr() as *mut ffi::PyTypeObject, n!(py, "dis")) == c.img_dis.as_ptr() as usize;
                    if let (Some(lo), true) = (lo, base_ok) {
                        dk = DisKind::Split(lo, size);
                        side = own.as_ref().and_then(|d| d.get_item(side_attr.bind(py)).ok().flatten()).and_then(|x| x.cast_into_exact::<PyDict>().ok());
                    }
                }
            }
        }
        let icache = if matches!(dk, DisKind::Base | DisKind::Split(..)) {
            own.as_ref().and_then(|d| d.get_item(n!(py, "cache")).ok().flatten()).and_then(|x| x.cast_into_exact::<PyDict>().ok())
        } else {
            None
        };
        let (dob, aob) = (ga(s, idr, n!(py, "d"))?, ga(s, idr, n!(py, "a"))?);
        let (wob, tob) = (ga(s, idr, n!(py, "writes"))?, ga(s, idr, n!(py, "trace"))?);
        let ptrs = [dob.as_ptr(), aob.as_ptr(), wob.as_ptr(), tob.as_ptr()];
        let alias = (0..4).any(|i| (i + 1..4).any(|j| ptrs[i] == ptrs[j]));
        let route = alias || matches!(dk, DisKind::Other);
        let dl = dob.cast_into_exact::<PyList>().ok().filter(|l| l.len() == 4);
        let al = aob.cast_into_exact::<PyList>().ok().filter(|l| l.len() == 4);
        let wl = wob.cast_into_exact::<PyList>().ok();
        let tl = tob.cast_into_exact::<PyList>().ok();
        let tlen = tl.as_ref().map(|l| l.len()).unwrap_or(0);
        let stubs_dict = ga(s, idr, n!(py, "stubs"))?.cast_into_exact::<PyDict>().ok();
        self.env = Some(Env {
            m,
            idict,
            dl,
            al,
            mem: None,
            mk: MemKind::Other,
            wl,
            tl,
            tlen,
            stubs_dict,
            img,
            dk,
            icache,
            side,
            align: None,
            imgv: ImgView::new(),
            dec: FxHashMap::default(),
            route,
        });
        Ok(())
    }

    #[inline]
    fn idict(&self) -> Option<&Bound<'py, PyDict>> {
        self.env.as_ref().and_then(|e| e.idict.as_ref())
    }

    /// self.mem (read when CPU.ld / CPU.st would read it)
    fn mem(&mut self) -> PyResult<()> {
        self.derive()?;
        if self.env.as_ref().unwrap().mem.is_some() {
            return Ok(());
        }
        let (py, c, s) = (self.py, self.c, self.s);
        let mem = ga(s, self.idict(), n!(py, "mem"))?;
        let mk = if unsafe { ffi::Py_TYPE(mem.as_ptr()) } == c.rpm_type.as_ptr() as *mut ffi::PyTypeObject {
            if mem.cast::<MemCore>()?.borrow().odd_empty(py) { MemKind::Core } else { MemKind::Other }
        } else if mem.is_exact_instance_of::<PyDict>() {
            MemKind::Dict
        } else {
            MemKind::Other
        };
        let e = self.env.as_mut().unwrap();
        e.mem = Some(mem);
        e.mk = mk;
        Ok(())
    }

    /// (self.align, truthiness)
    fn align(&mut self) -> PyResult<(Bound<'py, PyAny>, bool)> {
        self.derive()?;
        if let Some(a) = &self.env.as_ref().unwrap().align {
            return Ok(a.clone());
        }
        let a = ga(self.s, self.idict(), n!(self.py, "align"))?;
        let t = a.is_truthy()?;
        self.env.as_mut().unwrap().align = Some((a.clone(), t));
        Ok((a, t))
    }

    fn load(&mut self) -> PyResult<()> {
        if self.v.is_some() {
            return Ok(());
        }
        self.derive()?;
        let (py, s) = (self.py, self.s);
        let e = self.env.as_ref().unwrap();
        let idr = e.idict.as_ref();
        let mut canon = e.dl.is_some() && e.al.is_some();
        let mut d = [0u32; 4];
        let mut a = [0u32; 4];
        if canon {
            for i in 0..4 {
                match as_u32(&e.dl.as_ref().unwrap().get_item(i)?) {
                    Some(x) => d[i] = x,
                    None => canon = false,
                }
                match as_u32(&e.al.as_ref().unwrap().get_item(i)?) {
                    Some(x) => a[i] = x,
                    None => canon = false,
                }
            }
        }
        let spo = ga(s, idr, n!(py, "sp"))?;
        let mdro = ga(s, idr, n!(py, "mdr"))?;
        let (sp, mdr) = match (as_u32(&spo), as_u32(&mdro)) {
            (Some(x), Some(y)) => (x, y),
            _ => {
                canon = false;
                (0, 0)
            }
        };
        let pco = ga(s, idr, n!(py, "pc"))?;
        let (pc, pc_obj) = match as_u32(&pco) {
            Some(x) => (Pc::U(x), None),
            None => (Pc::Obj, Some(pco)),
        };
        let steps = exact_i64(&ga(s, idr, n!(py, "steps"))?).filter(|v| *v < i64::MAX);
        self.v = Some(Vals {
            canon,
            d,
            a,
            sp,
            mdr,
            pc,
            pc_obj,
            f_loaded: false,
            f: [None; 4],
            lar_loaded: false,
            lar: None,
            lar_obj: None,
            steps,
            trap: None,
            trap_t: false,
            dd: [false; 4],
            da: [false; 4],
            dsp: false,
            dmdr: false,
            dpc: false,
            dflags: false,
            dlar: false,
            dsteps: false,
            dtrap: false,
        });
        Ok(())
    }

    /// z n c v (read where `cond` reads them)
    fn load_flags(&mut self) -> PyResult<[Option<bool>; 4]> {
        self.load()?;
        if !self.v.as_ref().unwrap().f_loaded {
            let (py, s) = (self.py, self.s);
            let mut f = [None; 4];
            for (i, nm) in [n!(py, "z"), n!(py, "n"), n!(py, "c"), n!(py, "v")].into_iter().enumerate() {
                f[i] = exact_bool(&ga(s, self.idict(), nm)?);
            }
            let v = self.v.as_mut().unwrap();
            v.f = f;
            v.f_loaded = true;
        }
        Ok(self.v.as_ref().unwrap().f)
    }

    /// self.lar (read where an Lcc reads it)
    fn load_lar(&mut self) -> PyResult<()> {
        self.load()?;
        if !self.v.as_ref().unwrap().lar_loaded {
            let o = ga(self.s, self.idict(), n!(self.py, "lar"))?;
            let v = self.v.as_mut().unwrap();
            v.lar = as_u32(&o);
            v.lar_obj = Some(o);
            v.lar_loaded = true;
        }
        Ok(())
    }

    /// truthiness of self._trap (read where _chk reads it)
    fn trap_truthy(&mut self) -> PyResult<bool> {
        self.load()?;
        if self.v.as_ref().unwrap().trap.is_none() {
            let o = ga(self.s, self.idict(), n!(self.py, "_trap"))?;
            let t = o.is_truthy()?;
            let v = self.v.as_mut().unwrap();
            v.trap = Some(o);
            v.trap_t = t;
        }
        Ok(self.v.as_ref().unwrap().trap_t)
    }

    pub(crate) fn flush(&mut self) -> PyResult<()> {
        let (py, s) = (self.py, self.s);
        let v = match self.v.as_mut() {
            Some(v) => v,
            None => return Ok(()),
        };
        let e = self.env.as_ref().unwrap();
        let idr = e.idict.as_ref();
        for i in 0..4 {
            if v.dd[i] {
                e.dl.as_ref().unwrap().set_item(i, pyint(py, v.d[i] as i64))?;
                v.dd[i] = false;
            }
            if v.da[i] {
                e.al.as_ref().unwrap().set_item(i, pyint(py, v.a[i] as i64))?;
                v.da[i] = false;
            }
        }
        if v.dsp {
            sa(s, idr, n!(py, "sp"), &pyint(py, v.sp as i64))?;
            v.dsp = false;
        }
        if v.dmdr {
            sa(s, idr, n!(py, "mdr"), &pyint(py, v.mdr as i64))?;
            v.dmdr = false;
        }
        if v.dsteps {
            sa(s, idr, n!(py, "steps"), &pyint(py, v.steps.unwrap()))?;
            v.dsteps = false;
        }
        if v.dflags {
            for (i, nm) in [n!(py, "z"), n!(py, "n"), n!(py, "c"), n!(py, "v")].into_iter().enumerate() {
                sa(s, idr, nm, &pybool(py, v.f[i].unwrap()))?;
            }
            v.dflags = false;
        }
        if v.dlar {
            sa(s, idr, n!(py, "lar"), &pyint(py, v.lar.unwrap() as i64))?;
            v.dlar = false;
        }
        if v.dpc {
            match v.pc {
                Pc::U(x) => sa(s, idr, n!(py, "pc"), &pyint(py, x as i64))?,
                Pc::Obj => sa(s, idr, n!(py, "pc"), v.pc_obj.as_ref().unwrap())?,
            }
            v.dpc = false;
        }
        if v.dtrap {
            sa(s, idr, n!(py, "_trap"), v.trap.as_ref().unwrap())?;
            v.dtrap = false;
        }
        Ok(())
    }

    #[inline]
    fn vv(&mut self) -> PyResult<&mut Vals<'py>> {
        if self.v.is_none() {
            self.load()?;
        }
        Ok(self.v.as_mut().unwrap())
    }

    /// state after a mid-instruction callback must still be u32 (pyemu would compute with whatever it is)
    fn vc(&mut self) -> PyResult<&mut Vals<'py>> {
        let v = self.vv()?;
        if !v.canon {
            return changed("a register / sp / mdr");
        }
        Ok(v)
    }

    fn set_trap(&mut self, t: Bound<'py, PyAny>, truthy: bool) -> PyResult<()> {
        let v = self.vv()?;
        v.trap = Some(t);
        v.trap_t = truthy;
        v.dtrap = true;
        Ok(())
    }

    fn pc_object(&mut self) -> PyResult<Bound<'py, PyAny>> {
        let py = self.py;
        let v = self.vv()?;
        Ok(match v.pc {
            Pc::U(x) => pyint(py, x as i64),
            Pc::Obj => v.pc_obj.clone().unwrap(),
        })
    }

    // ---- registers ------------------------------------------------------------------------------------------------------------
    #[inline]
    fn reg(&mut self, r: R) -> PyResult<u32> {
        let v = self.vc()?;
        Ok(match r {
            R::D(i) => v.d[i as usize],
            R::A(i) => v.a[i as usize],
            R::Sp => v.sp,
            R::Mdr => v.mdr,
        })
    }

    #[inline]
    fn get(&mut self, g: G) -> PyResult<u32> {
        match g {
            G::I(x) => Ok(x),
            G::R(r) => self.reg(r),
        }
    }

    #[inline]
    fn put(&mut self, r: R, x: u64) -> PyResult<()> {
        let x = (x & M32) as u32;
        let v = self.vc()?;
        match r {
            R::D(i) => {
                v.d[i as usize] = x;
                v.dd[i as usize] = true;
            }
            R::A(i) => {
                v.a[i as usize] = x;
                v.da[i as usize] = true;
            }
            R::Sp => {
                v.sp = x;
                v.dsp = true;
            }
            R::Mdr => {
                v.mdr = x;
                v.dmdr = true;
            }
        }
        Ok(())
    }

    fn ea(&mut self, e: Ea) -> PyResult<i64> {
        Ok(match e {
            Ea::R(r) => self.reg(r)? as i64,
            Ea::I(x) => x as i64,
            Ea::P(x, y) => {
                let dx: i128 = match x {
                    X::R(r) => self.reg(r)? as i128,
                    X::I(k) => k as i128,
                };
                ((dx + self.get(y)? as i128) & M32 as i128) as i64
            }
        })
    }

    fn target(&mut self, t: T) -> PyResult<u32> {
        Ok(match t {
            T::Ea(e) => self.ea(e)? as u32,
            T::I(x) => x,
        })
    }

    #[inline]
    fn flags(&mut self, z: bool, n: bool, c: bool, w: bool) -> PyResult<()> {
        let v = self.vv()?;
        v.f = [Some(z), Some(n), Some(c), Some(w)];
        v.f_loaded = true;
        v.dflags = true;
        Ok(())
    }

    fn flags_sub(&mut self, b: u64, a: u64) -> PyResult<u64> {
        let r = b.wrapping_sub(a) & M32;
        self.flags(r == 0, r & 0x8000_0000 != 0, b < a, ((b ^ a) & (b ^ r) & 0x8000_0000) != 0)?;
        Ok(r)
    }

    fn flags_add(&mut self, b: u64, a: u64) -> PyResult<u64> {
        let r = (b + a) & M32;
        self.flags(r == 0, r & 0x8000_0000 != 0, b + a > M32, (!(b ^ a) & (b ^ r) & 0x8000_0000) != 0)?;
        Ok(r)
    }

    fn flags_logic(&mut self, r: u64) -> PyResult<()> {
        self.flags(r & M32 == 0, r & 0x8000_0000 != 0, false, false)
    }

    fn cond(&mut self, k: Cond) -> PyResult<bool> {
        let f = self.load_flags()?;
        let (z, n, cc, v) = match f {
            [Some(z), Some(n), Some(c), Some(v)] => (z, n, c, v),
            _ => return changed("a flag"),
        };
        Ok(match k {
            Cond::Eq => z,
            Cond::Ne => !z,
            Cond::Gt => !(z || (n != v)),
            Cond::Ge => n == v,
            Cond::Le => z || (n != v),
            Cond::Lt => n != v,
            Cond::Hi => !(cc || z),
            Cond::Cc => !cc,
            Cond::Ls => cc || z,
            Cond::Cs => cc,
            Cond::Ra => true,
        })
    }

    // ---- memory ---------------------------------------------------------------------------------------------------------------
    fn cur_pc(&mut self) -> PyResult<u32> {
        match self.vv()?.pc {
            Pc::U(x) => Ok(x),
            Pc::Obj => changed("pc"),
        }
    }

    fn trap(&mut self, adm: u64, n: i64, what: &str) -> PyResult<()> {
        if self.trap_truthy()? && n > 1 && (adm & (n as u64 - 1)) != 0 {
            let pc = self.cur_pc()?;
            let msg = format!("misaligned {} u{} at {:#x} (pc={:#x})", what, 8 * n, adm, pc);
            return Err(PyErr::from_value(self.c.align_fault.bind(self.py).call1((msg,))?));
        }
        Ok(())
    }

    fn ld(&mut self, ad: i64, n: i64) -> PyResult<u32> {
        let py = self.py;
        self.derive()?;
        if !self.env.as_ref().unwrap().m.ld_nat {
            stat(S_PY_LD);
            let s = self.s;
            let r = self.call(|_| s.call_method1(n!(py, "ld"), (pyint(py, ad), n)))?;
            return match as_u32(&r) {
                Some(x) => Ok(x),
                None => changed("an overridden ld result"),
            };
        }
        let adm = ad as u64 & M32;
        self.trap(adm, n, "load")?;
        self.mem()?;
        let c = self.c;
        let e = self.env.as_mut().unwrap();
        let mut val: u64 = 0;
        match e.mk {
            MemKind::Core => {
                let core = e.mem.as_ref().unwrap().cast::<MemCore>()?.borrow();
                for i in 0..n {
                    let a = adm as i64 + i;
                    let b = match core.byte(a) {
                        Some(b) => b,
                        None => match e.imgv.byte(py, c, &e.img, a)? {
                            Some(b) => b,
                            None => {
                                drop(core);
                                return self.ld_h(ad, n);
                            }
                        },
                    };
                    val |= (b as u64) << (8 * i);
                }
            }
            MemKind::Dict => {
                let dict = e.mem.as_ref().unwrap().cast::<PyDict>()?.clone();
                for i in 0..n {
                    let a = adm as i64 + i;
                    let b = match dict.get_item(pyint(py, a))? {
                        Some(x) if !x.is_none() => match mem::byte_val(&x) {
                            Some(b) => b,
                            None => return self.ld_h(ad, n),
                        },
                        _ => match e.imgv.byte(py, c, &e.img, a)? {
                            Some(b) => b,
                            None => return self.ld_h(ad, n),
                        },
                    };
                    val |= (b as u64) << (8 * i);
                }
            }
            MemKind::Other => return self.ld_h(ad, n),
        }
        Ok(val as u32)
    }

    /// the previously installed Python ld (stock / ofh.mem.shm), for memory the native code does not handle
    fn ld_h(&mut self, ad: i64, n: i64) -> PyResult<u32> {
        stat(S_H_LD);
        let (py, s, c) = (self.py, self.s, self.c);
        let r = self.call(|_| c.h_ld.bind(py).call1((s, pyint(py, ad), n)))?;
        match as_u32(&r) {
            Some(x) => Ok(x),
            None => changed("an ld result"),
        }
    }

    fn st(&mut self, ad: i64, n: i64, x: u32) -> PyResult<()> {
        let py = self.py;
        self.derive()?;
        if !self.env.as_ref().unwrap().m.st_nat {
            stat(S_PY_ST);
            let s = self.s;
            self.call(|_| s.call_method1(n!(py, "st"), (pyint(py, ad), n, x)))?;
            return Ok(());
        }
        let adm = ad as u64 & M32;
        self.trap(adm, n, "store")?;
        self.mem()?;
        let pc = self.cur_pc()?;
        let e = self.env.as_mut().unwrap();
        match e.mk {
            MemKind::Core => {
                e.mem.as_ref().unwrap().cast::<MemCore>()?.borrow_mut().store_n(adm as i64, n, x as u64);
            }
            MemKind::Dict => {
                let dict = e.mem.as_ref().unwrap().cast::<PyDict>()?;
                for i in 0..n {
                    dict.set_item(pyint(py, adm as i64 + i), pyint(py, ((x as u64 >> (8 * i)) & 0xff) as i64))?;
                }
            }
            MemKind::Other => {
                stat(S_H_ST);
                let (s, c) = (self.s, self.c);
                self.call(|_| c.h_st.bind(py).call1((s, pyint(py, ad), n, x)))?;
                return Ok(());
            }
        }
        let ent = PyTuple::new(py, [pyint(py, adm as i64), pyint(py, n), pyint(py, pc as i64)])?;
        let wl = e.wl.clone();
        match wl {
            Some(l) => l.append(ent)?,
            None => {
                // a mid-instruction callback rebound self.writes to a non-list: `self.writes.append(...)` through Python (same result /
                // same exception), state flushed first
                let s = self.s;
                self.call(|eng| ga(s, eng.idict(), n!(py, "writes"))?.call_method1(n!(py, "append"), (ent,)).map(|_| ()))?;
            }
        }
        Ok(())
    }

    // ---- one instruction ------------------------------------------------------------------------------------------------------
    /// the decoded instruction at pc (dis() semantics), or None -> the Python step must run it
    fn fetch(&mut self, pc: u32) -> PyResult<Option<(Bound<'py, PyTuple>, Arc<Op>, i64)>> {
        let py = self.py;
        if let Some(h) = self.env.as_ref().unwrap().dec.get(&pc) {
            return Ok(Some(h.clone()));
        }
        let e = self.env.as_ref().unwrap();
        let pco = pyint(py, pc as i64);
        let mut ent = None;
        let base = match e.dk {
            DisKind::Base => true,
            DisKind::Split(lo, size) => {
                let inside = lo <= pc as i64 && (pc as i64) < lo + size;
                if inside {
                    if let Some(sd) = &e.side {
                        ent = sd.get_item(&pco)?;
                    }
                }
                !inside
            }
            DisKind::Other => false,
        };
        if base {
            if let Some(cd) = &e.icache {
                ent = cd.get_item(&pco)?;
            }
        }
        let ent = match ent {
            Some(x) => x,
            None => {
                stat(S_DIS_CALL);
                let img = e.img.clone();
                let r = self.call(|_| img.call_method1(n!(py, "dis"), (&pco,)))?;
                self.derive()?;
                r
            }
        };
        let t = match ent.cast_into_exact::<PyTuple>() {
            Ok(t) if t.len() == 3 => t,
            _ => {
                stat(S_FB_DIS);
                return Ok(None);
            }
        };
        let (op, sz) = decoded(&t)?;
        if matches!(*op, Op::Unsupported) {
            stat(S_FB_OP);
            return Ok(None);
        }
        let h = (t, op, sz.unwrap());
        self.env.as_mut().unwrap().dec.insert(pc, h.clone());
        Ok(Some(h))
    }

    fn is_stub(&mut self, pco: &Bound<'py, PyAny>) -> PyResult<Option<bool>> {
        match &self.env.as_ref().unwrap().stubs_dict {
            Some(d) => Ok(Some(d.contains(pco)?)),
            None => Ok(None), // not a dict: only the Python code may test it
        }
    }

    /// CPU._step natively; Fallback = nothing was done, the original Python _step must run this step
    pub(crate) fn step_native(&mut self) -> PyResult<Res> {
        self.derive()?;
        {
            let e = self.env.as_ref().unwrap();
            if e.m.full_py {
                stat(S_FB_HELPER);
                return Ok(Res::Fallback);
            }
            if e.route {
                stat(S_FB_ROUTE);
                return Ok(Res::Fallback);
            }
        }
        self.load()?;
        let pc = match self.v.as_ref().unwrap().pc {
            Pc::U(x) => x,
            Pc::Obj => {
                stat(S_FB_PC);
                return Ok(Res::Fallback);
            }
        };
        {
            let e = self.env.as_ref().unwrap();
            let stub = match &e.stubs_dict {
                Some(d) => d.contains(pyint(self.py, pc as i64))?,
                None => {
                    stat(S_FB_STUB);
                    return Ok(Res::Fallback);
                }
            };
            if stub {
                stat(S_FB_STUB);
                return Ok(Res::Fallback);
            }
        }
        let (ent, op, sz) = match self.fetch(pc)? {
            Some(h) => h,
            None => return Ok(Res::Fallback),
        };
        self.load()?; // a dis() call re-derives / reloads
        let fl_ok = !op.needs_flags() || self.load_flags()?.iter().all(|x| x.is_some());
        {
            let e = self.env.as_ref().unwrap();
            let v = self.v.as_ref().unwrap();
            if !v.canon || v.steps.is_none() || e.wl.is_none() || e.tl.is_none() || !fl_ok || v.pc != Pc::U(pc) || e.m.full_py {
                stat(S_FB_STATE);
                return Ok(Res::Fallback);
            }
        }
        stat(S_NATIVE);
        // side effects, in CPU._step's order
        let py = self.py;
        {
            let v = self.v.as_mut().unwrap();
            v.steps = Some(v.steps.unwrap() + 1);
            v.dsteps = true;
        }
        {
            let e = self.env.as_mut().unwrap();
            if e.tlen < 4000 {
                let t = PyTuple::new(py, [pyint(py, pc as i64), ent.get_item(0)?, ent.get_item(1)?])?;
                e.tl.as_ref().unwrap().append(t)?;
                e.tlen += 1;
            }
        }
        let npc = ((pc as i64 + sz) as u64 & M32) as u32;
        let next = self.exec(&op, npc)?;
        if next.is_none() {
            self.load_lar()?;
        }
        let v = self.vv()?;
        match next {
            Some(p) => v.pc = Pc::U(p),
            None => match v.lar {
                Some(l) => v.pc = Pc::U(l),
                None => {
                    v.pc = Pc::Obj;
                    v.pc_obj = v.lar_obj.clone();
                }
            },
        }
        v.dpc = true;
        Ok(Res::Done)
    }

    /// execute one decoded instruction; returns the next pc (None = Lcc taken: pc <- lar)
    fn exec(&mut self, op: &Op, npc: u32) -> PyResult<Option<u32>> {
        let mut npc = npc;
        match op {
            Op::Unsupported => unreachable!(),
            Op::St { w, ea, src } => {
                let ad = self.ea(*ea)?;
                let v = self.get(*src)?;
                self.st(ad, *w as i64, v)?;
            }
            Op::Ld { w, ea, dst } => {
                let ad = self.ea(*ea)?;
                let v = self.ld(ad, *w as i64)?;
                self.put(*dst, v as u64)?;
            }
            Op::Mov { src, dst } => {
                let v = self.get(*src)?;
                self.put(*dst, v as u64)?;
            }
            Op::Clr { dst } => {
                self.put(*dst, 0)?;
                self.flags(true, false, false, false)?;
            }
            Op::ExtMdr { g } => {
                let v = self.get(*g)?;
                self.put(R::Mdr, if v & 0x8000_0000 != 0 { M32 } else { 0 })?;
            }
            Op::Nop => {}
            Op::Ext { k, g, p } => {
                let v = self.get(*g)? as u64;
                let r = match k {
                    Ext::B => ((v & 0xff) as u8 as i8) as i64 as u64,
                    Ext::Bu => v & 0xff,
                    Ext::H => ((v & 0xffff) as u16 as i16) as i64 as u64,
                    Ext::Hu => v & 0xffff,
                };
                self.put(*p, r)?;
            }
            Op::Add { s, d, p } => {
                let (b, a) = (self.get(*d)? as u64, self.get(*s)? as u64);
                let r = self.flags_add(b, a)?;
                self.put(*p, r)?;
            }
            Op::Sub { s, d, p } => {
                let (b, a) = (self.get(*d)? as u64, self.get(*s)? as u64);
                let r = self.flags_sub(b, a)?;
                self.put(*p, r)?;
            }
            Op::Cmp { s, d } => {
                let (b, a) = (self.get(*d)? as u64, self.get(*s)? as u64);
                self.flags_sub(b, a)?;
            }
            Op::Logic { k, s, d, p } => {
                let (x, y) = (self.get(*d)? as u64, self.get(*s)? as u64);
                let r = match k {
                    Lg::And => x & y,
                    Lg::Or => x | y,
                    Lg::Xor => x ^ y,
                };
                self.put(*p, r)?;
                self.flags_logic(r)?;
            }
            Op::Not { g, p } => {
                let r = !(self.get(*g)? as u64) & M32;
                self.put(*p, r)?;
                self.flags_logic(r)?;
            }
            Op::Shift { k, cnt, g, p } => {
                let k_ = match cnt {
                    Some(c) => self.get(*c)? & 31,
                    None => 1,
                };
                let v = self.get(*g)? as u64;
                let r = match k {
                    Sh::Asl => (v << k_) & M32,
                    Sh::Lsr => v >> k_,
                    Sh::Asr => (((v as u32 as i32) >> k_) as i64 as u64) & M32,
                };
                self.put(*p, r)?;
                self.flags_logic(r)?;
            }
            Op::Asl2 { g, p } => {
                let r = ((self.get(*g)? as u64) << 2) & M32;
                self.put(*p, r)?;
                self.flags_logic(r)?;
            }
            Op::Inc { g, p, flags, add } => {
                let v = self.get(*g)? as u64;
                if *flags {
                    let r = self.flags_add(v, 1)?;
                    self.put(*p, r)?;
                } else {
                    self.put(*p, v + *add as u64)?;
                }
            }
            Op::Mul { signed, setmdr, a, b, p } => {
                let (x, y) = (self.get(*a)?, self.get(*b)?);
                let r: i128 = if *signed { (x as i32 as i128) * (y as i32 as i128) } else { (x as i128) * (y as i128) };
                self.put(*p, (r & M32 as i128) as u64)?;
                self.flags_logic((r & M32 as i128) as u64)?;
                if *setmdr {
                    self.put(R::Mdr, ((r >> 32) & M32 as i128) as u64)?;
                }
            }
            Op::Div { signed, a, b, p } => {
                let (x, y) = (self.get(*a)?, self.get(*b)?);
                let (q, r): (i64, i64) = if *signed {
                    let (a_, b_) = (x as i32 as i64, y as i32 as i64);
                    if a_ != 0 {
                        let q = (b_ as f64 / a_ as f64) as i64; // int(b / a): true division then truncation
                        (q, b_ - q * a_)
                    } else {
                        (0, 0)
                    }
                } else {
                    let (a_, b_) = (x as i64, y as i64);
                    if a_ != 0 { (b_ / a_, b_ % a_) } else { (0, 0) }
                };
                self.put(*p, q as u64 & M32)?;
                self.put(R::Mdr, r as u64 & M32)?;
                self.flags_logic(q as u64 & M32)?;
            }
            Op::Btst { a, b } => {
                let r = self.get(*b)? as u64 & self.get(*a)? as u64;
                self.flags_logic(r)?;
            }
            Op::Setlb => {
                let v = self.vv()?;
                v.lar = Some(npc);
                v.lar_loaded = true;
                v.dlar = true;
            }
            Op::Lcc { c } => {
                if self.cond(*c)? {
                    return Ok(None);
                }
            }
            Op::Bcc { c, tgt } => {
                if self.cond(*c)? {
                    npc = *tgt;
                }
            }
            Op::Jmp { t } => {
                npc = self.target(*t)?;
            }
            Op::Call { tgt, regs, imm8 } => {
                let csp = self.reg(R::Sp)? as i64;
                self.put(R::Sp, (csp - imm8) as u64)?;
                for (i, r) in regs.iter().enumerate() {
                    let sp = self.reg(R::Sp)? as i64;
                    let v = self.get(*r)?;
                    self.st(sp + imm8 - 4 * (i as i64 + 1), 4, v)?;
                }
                self.st(csp, 4, npc)?;
                self.put(R::Mdr, npc as u64)?;
                npc = *tgt;
            }
            Op::MovmPush { regs } => {
                for (i, r) in regs.iter().enumerate() {
                    let sp = self.reg(R::Sp)? as i64;
                    let v = self.reg(*r)?;
                    self.st(sp - 4 * (i as i64 + 1), 4, v)?;
                }
                let sp = self.reg(R::Sp)? as i64;
                self.put(R::Sp, (sp - 4 * regs.len() as i64) as u64)?;
            }
            Op::MovmPop { regs } => {
                let n = regs.len() as i64;
                for (i, r) in regs.iter().enumerate() {
                    let sp = self.reg(R::Sp)? as i64;
                    let v = self.ld(sp + 4 * n - 4 * (i as i64 + 1), 4)?;
                    self.put(*r, v as u64)?;
                }
                let sp = self.reg(R::Sp)? as i64;
                self.put(R::Sp, (sp + 4 * n) as u64)?;
            }
            Op::Ret { retf, regs, imm8 } => {
                for (i, r) in regs.iter().enumerate() {
                    let sp = self.reg(R::Sp)? as i64;
                    let v = self.ld(sp + imm8 - 4 * (i as i64 + 1), 4)?;
                    self.put(*r, v as u64)?;
                }
                let sp = self.reg(R::Sp)? as i64;
                self.put(R::Sp, (sp + imm8) as u64)?;
                npc = if *retf {
                    self.reg(R::Mdr)?
                } else {
                    let sp = self.reg(R::Sp)? as i64;
                    self.ld(sp, 4)?
                };
            }
            Op::Calls { t } => {
                let sp = self.reg(R::Sp)? as i64;
                self.st(sp, 4, npc)?;
                self.put(R::Mdr, npc as u64)?;
                npc = self.target(*t)?;
            }
            Op::Bit { set, mask, ea } => {
                let mask = self.get(*mask)? as u64 & 0xff;
                let ad = self.ea(*ea)?;
                let t = self.ld(ad, 1)? as u64;
                self.flags((t & mask) == 0, false, false, false)?;
                self.st(ad, 1, if *set { (t | mask) as u32 } else { (t & !mask & 0xff) as u32 })?;
            }
            Op::Rets => {
                let sp = self.reg(R::Sp)? as i64;
                npc = self.ld(sp, 4)?;
            }
        }
        Ok(Some(npc))
    }

    /// the original Python CPU._step (state flushed before, reloaded after)
    fn py_step(&mut self) -> PyResult<()> {
        stat(S_PYSTEP);
        let (py, s, c) = (self.py, self.s, self.c);
        self.call(|_| c.py_step.bind(py).call1((s,)).map(|_| ()))
    }

    /// CPU._step: native, else the Python one
    fn inner(&mut self) -> PyResult<()> {
        match self.step_native()? {
            Res::Done => Ok(()),
            Res::Fallback => self.py_step(),
        }
    }

    /// CPU.step: `self._trap = self.align and self.pc not in self.stubs; try: self._step() finally: self._trap = False`
    fn step_one(&mut self) -> PyResult<()> {
        let py = self.py;
        let m = match &self.env {
            Some(env) => env.m,
            None => mode(py, self.c, self.s),
        };
        if !m.istep_ours || m.full_py {
            // a Python _step (subclass / instance), an overridden helper or attribute hooks: nothing to cache, run CPU.step's Python
            // semantics on the attributes (our _step = the original Python _step directly)
            if m.full_py {
                stat(S_PY_ENTRY);
            }
            let (s, c) = (self.s, self.c);
            return self.call(|_| light_step(py, c, s, m.istep_ours));
        }
        if self.env.is_none() {
            self.pre_mode = Some(m);
        }
        self.derive()?;
        if self.env.as_ref().unwrap().route {
            // aliased lists / unreviewed Image.dis: the Python step (nothing loaded or changed yet)
            stat(S_FB_ROUTE);
            let (s, c) = (self.s, self.c);
            return self.call(|_| light_step(py, c, s, true));
        }
        self.load()?;
        let (align, align_t) = self.align()?;
        if align_t {
            let pco = self.pc_object()?;
            let st = match self.is_stub(&pco)? {
                Some(b) => b,
                None => {
                    let stubs = self.s.getattr(n!(py, "stubs"))?;
                    let r = self.call(|_| stubs.contains(&pco))?;
                    self.load()?;
                    r
                }
            };
            self.set_trap(pybool(py, !st), !st)?;
        } else {
            self.set_trap(align, false)?;
        }
        let r = self.inner();
        let f = self.set_trap(pybool(py, false), false);
        r?;
        f
    }
}

/// CPU.step on the attributes: `self._trap = self.align and self.pc not in self.stubs; try: self._step() finally: self._trap = False`.
/// direct: self._step is ours and must be the Python one -> call the original Python _step without going through the wrapper.
fn light_step(py: Python<'_>, c: &Cfg, s: &Bound<'_, PyAny>, direct: bool) -> PyResult<()> {
    let align = s.getattr(n!(py, "align"))?;
    let trap = if align.is_truthy()? {
        let pc = s.getattr(n!(py, "pc"))?;
        pybool(py, !s.getattr(n!(py, "stubs"))?.contains(pc)?)
    } else {
        align
    };
    s.setattr(n!(py, "_trap"), trap)?;
    let r = if direct { c.py_step.bind(py).call1((s,)).map(|_| ()) } else { s.call_method0(n!(py, "_step")).map(|_| ()) };
    let f = s.setattr(n!(py, "_trap"), pybool(py, false));
    r?;
    f
}

// ---- entry points (called from lib.rs) -------------------------------------------------------------------------------------------
fn finish<T>(e: &mut Eng<'_, '_>, r: PyResult<T>) -> PyResult<T> {
    let f = e.flush();
    let r = r?;
    f?;
    Ok(r)
}

pub(crate) fn step_inner(py: Python<'_>, c: &'static Cfg, s: &Bound<'_, PyAny>) -> PyResult<()> {
    let m = mode(py, c, s);
    if m.full_py {
        // overridden helper / attribute hooks: the original Python _step, no engine (no derive / load / flush)
        stat(S_PY_ENTRY);
        return c.py_step.bind(py).call1((s,)).map(|_| ());
    }
    let mut e = Eng::new(py, c, s);
    e.pre_mode = Some(m);
    let r = e.inner();
    finish(&mut e, r)
}

pub(crate) fn step(py: Python<'_>, c: &'static Cfg, s: &Bound<'_, PyAny>) -> PyResult<()> {
    let mut e = Eng::new(py, c, s);
    let r = e.step_one();
    finish(&mut e, r)
}

/// tests / tuning: ns per call of the engine's pieces on CPU object `s` (no state is changed: derive / load / mode only read)
pub(crate) fn bench_parts(py: Python<'_>, c: &'static Cfg, s: &Bound<'_, PyAny>, n: usize) -> PyResult<Vec<(String, f64)>> {
    let mut out = Vec::new();
    let t = std::time::Instant::now();
    for _ in 0..n {
        let _ = tinfo(py, c, s);
    }
    out.push(("tinfo".to_string(), t.elapsed().as_nanos() as f64 / n as f64));
    out.push(("tp_version_tag".to_string(), unsafe { (*ffi::Py_TYPE(s.as_ptr())).tp_version_tag } as f64));
    let t = std::time::Instant::now();
    for _ in 0..n {
        let _ = mode(py, c, s);
    }
    out.push(("mode".to_string(), t.elapsed().as_nanos() as f64 / n as f64));
    let t = std::time::Instant::now();
    for _ in 0..n {
        let _ = inst_dict(py, s);
    }
    out.push(("inst_dict".to_string(), t.elapsed().as_nanos() as f64 / n as f64));
    let t = std::time::Instant::now();
    for _ in 0..n {
        let mut e = Eng::new(py, c, s);
        e.derive()?;
    }
    out.push(("derive".to_string(), t.elapsed().as_nanos() as f64 / n as f64));
    let t = std::time::Instant::now();
    for _ in 0..n {
        let mut e = Eng::new(py, c, s);
        e.load()?;
    }
    out.push(("derive+load".to_string(), t.elapsed().as_nanos() as f64 / n as f64));
    let t = std::time::Instant::now();
    for _ in 0..n {
        for _ in 0..10 {
            let _ = s.getattr(n!(py, "sp"))?;
        }
    }
    out.push(("10 getattr".to_string(), t.elapsed().as_nanos() as f64 / n as f64));
    let t = std::time::Instant::now();
    for _ in 0..n {
        let mut e = Eng::new(py, c, s);
        e.load()?;
        let pc = match e.v.as_ref().unwrap().pc {
            Pc::U(x) => x,
            Pc::Obj => 0,
        };
        let _ = e.fetch(pc)?;
    }
    out.push(("derive+load+fetch".to_string(), t.elapsed().as_nanos() as f64 / n as f64));
    Ok(out)
}

pub(crate) fn run<'py>(py: Python<'py>, c: &'static Cfg, s: &Bound<'py, PyAny>, start: &Bound<'py, PyAny>, stop: &Bound<'py, PyAny>, max_steps: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    s.setattr(n!(py, "pc"), start)?;
    // stop: an exact set / frozenset of exact ints is tested natively; anything else through Python (`self.pc not in stop`)
    let stopset: Option<FxHashSet<i64>> = if stop.is_exact_instance_of::<pyo3::types::PySet>() || stop.is_exact_instance_of::<pyo3::types::PyFrozenSet>() {
        let mut h = FxHashSet::default();
        let mut ok = true;
        for k in stop.try_iter()? {
            match exact_i64(&k?) {
                Some(v) => {
                    h.insert(v);
                }
                None => ok = false,
            }
        }
        if ok { Some(h) } else { None }
    } else {
        None
    };
    let ms = exact_i64(max_steps);
    let mut e = Eng::new(py, c, s);
    let r = (|| -> PyResult<Bound<'py, PyAny>> {
        let mut stop_checked = false; // the Python loop below already tested this pc against `stop`
        let mut route_known = false; // aliased lists / unreviewed Image.dis seen in this run: keep the Python loop
        loop {
            let m = match &e.env {
                Some(env) => env.m,
                None => mode(py, c, s),
            };
            let mut py_body = !m.step_ours || m.full_py;
            if !py_body {
                if e.env.is_none() {
                    e.pre_mode = Some(m);
                }
                e.derive()?;
                if e.env.as_ref().unwrap().route {
                    route_known = true;
                    py_body = true;
                }
            }
            if py_body {
                // a Python step (subclass / instance), an overridden helper, attribute hooks, aliased lists or an unreviewed Image.dis:
                // CPU.run's loop on the attributes (CPU.step's Python semantics when step is ours), until the run ends or the object
                // becomes one the engine runs again (one mode() per step, no engine state)
                let done = e.call(|_| -> PyResult<bool> {
                    loop {
                        if stop.contains(s.getattr(n!(py, "pc"))?)? {
                            return Ok(true);
                        }
                        let m2 = mode(py, c, s);
                        if m2.step_ours && !m2.full_py && !route_known {
                            return Ok(false);
                        }
                        if m2.step_ours {
                            stat(if m2.full_py { S_PY_ENTRY } else { S_FB_ROUTE });
                            light_step(py, c, s, m2.istep_ours)?;
                        } else {
                            s.call_method0(n!(py, "step"))?;
                        }
                        if s.getattr(n!(py, "steps"))?.gt(max_steps)? {
                            return Err(PyRuntimeError::new_err("max steps"));
                        }
                    }
                })?;
                if done {
                    return s.getattr(n!(py, "pc"));
                }
                stop_checked = true;
                continue;
            }
            e.load()?;
            let pc = e.v.as_ref().unwrap().pc;
            let hit = match (&stopset, pc) {
                _ if std::mem::take(&mut stop_checked) => false,
                (Some(set), Pc::U(x)) => set.contains(&(x as i64)),
                _ => {
                    let pco = e.pc_object()?;
                    e.call(|_| stop.contains(&pco))?
                }
            };
            if hit {
                return e.pc_object();
            }
            e.step_one()?;
            let st = e.vv()?.steps;
            let over = match (st, ms) {
                (Some(a), Some(b)) => a > b,
                _ => e.call(|_| s.getattr(n!(py, "steps"))?.gt(max_steps))?,
            };
            if over {
                return Err(PyRuntimeError::new_err("max steps"));
            }
        }
    })();
    finish(&mut e, r)
}
