//! Operand / instruction decoding: a mirror of pyemu's string semantics (num, get, put, ea, ret_operands and the _step if-chain).
//! Rule: an operand or instruction form that the mirror cannot reproduce EXACTLY decodes to Op::Unsupported, and that instruction is
//! executed by the original Python CPU._step (which then computes, or raises, exactly what it always did).

pub const M32: u64 = 0xffff_ffff;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum R {
    D(u8),
    A(u8),
    Sp,
    Mdr,
}

/// value of `CPU.get(s)`
#[derive(Clone, Copy, Debug)]
pub enum G {
    R(R),
    I(u32), // num(s) & M32
}

/// first element of an `(x,y)` effective address: `get(x) if reg(x) else num(x)` (NOT masked)
#[derive(Clone, Copy, Debug)]
pub enum X {
    R(R),
    I(i64),
}

/// value of `CPU.ea(s)`
#[derive(Clone, Copy, Debug)]
pub enum Ea {
    R(R),     // raw register value
    I(u32),   // num(inner) & M32
    P(X, G),  // (dx + get(y)) & M32
}

/// jmp / calls target: `ea(o[0]) if o[0].startswith('(') else num(o[0]) & M32`
#[derive(Clone, Copy, Debug)]
pub enum T {
    Ea(Ea),
    I(u32),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Cond {
    Eq,
    Ne,
    Gt,
    Ge,
    Le,
    Lt,
    Hi,
    Cc,
    Ls,
    Cs,
    Ra,
}

#[derive(Clone, Copy, Debug)]
pub enum Ext {
    B,
    Bu,
    H,
    Hu,
}

#[derive(Clone, Copy, Debug)]
pub enum Lg {
    And,
    Or,
    Xor,
}

#[derive(Clone, Copy, Debug)]
pub enum Sh {
    Asl,
    Lsr,
    Asr,
}

#[derive(Clone, Debug)]
pub enum Op {
    Unsupported,
    St { w: u8, ea: Ea, src: G },
    Ld { w: u8, ea: Ea, dst: R },
    Mov { src: G, dst: R },
    Clr { dst: R },
    ExtMdr { g: G },
    Nop,
    Ext { k: Ext, g: G, p: R },
    Add { s: G, d: G, p: R },
    Sub { s: G, d: G, p: R },
    Cmp { s: G, d: G },
    Logic { k: Lg, s: G, d: G, p: R },
    Not { g: G, p: R },
    Shift { k: Sh, cnt: Option<G>, g: G, p: R },
    Asl2 { g: G, p: R },
    Inc { g: G, p: R, flags: bool, add: u32 },
    Mul { signed: bool, setmdr: bool, a: G, b: G, p: R },
    Div { signed: bool, a: G, b: G, p: R },
    Btst { a: G, b: G },
    Setlb,
    Lcc { c: Cond },
    Bcc { c: Cond, tgt: u32 },
    Jmp { t: T },
    Call { tgt: u32, regs: Vec<G>, imm8: i64 },
    MovmPush { regs: Vec<R> },
    MovmPop { regs: Vec<R> },
    Ret { retf: bool, regs: Vec<R>, imm8: i64 },
    Calls { t: T },
    Bit { set: bool, mask: G, ea: Ea },
    Rets,
}

impl Op {
    pub fn needs_flags(&self) -> bool {
        matches!(self, Op::Lcc { .. } | Op::Bcc { .. })
    }
}

/// only plain printable ASCII is mirrored (python's str.strip / int() accept far more; anything else -> Unsupported)
#[inline]
fn plain(s: &str) -> bool {
    s.bytes().all(|b| (0x20..=0x7e).contains(&b))
}

#[inline]
fn strip(s: &str) -> &str {
    s.trim_matches(' ')
}

fn reg(s: &str) -> Option<R> {
    Some(match s {
        "d0" => R::D(0),
        "d1" => R::D(1),
        "d2" => R::D(2),
        "d3" => R::D(3),
        "a0" => R::A(0),
        "a1" => R::A(1),
        "a2" => R::A(2),
        "a3" => R::A(3),
        "sp" => R::Sp,
        "mdr" => R::Mdr,
        _ => return None,
    })
}

fn hexdigits(s: &str) -> Option<i128> {
    // up to 16 digits (every real absolute-address operand); wider -> Unsupported
    if s.is_empty() || s.len() > 16 || !s.bytes().all(|b| b.is_ascii_hexdigit()) {
        return None;
    }
    u64::from_str_radix(s, 16).ok().map(|v| v as i128)
}

fn decdigits(s: &str) -> Option<i64> {
    // int(s, 0) on an unsigned decimal: "0", "00..", or no leading zero
    if s.is_empty() || s.len() > 18 || !s.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    if s.len() > 1 && s.starts_with('0') && !s.bytes().all(|b| b == b'0') {
        return None; // int('012', 0) -> ValueError
    }
    s.parse::<i64>().ok()
}

/// pyemu.num for the contexts that use the value unmasked (immediates of call / ret, (x,y) displacements): |value| < 2**60, which
/// covers every 15-hex-digit / 18-decimal-digit literal and keeps all later i64 arithmetic exact; larger -> Unsupported
pub fn num(s0: &str) -> Option<i64> {
    num_wide(s0).filter(|v| v.abs() < (1i128 << 60)).map(|v| v as i64)
}

/// `num(s) & M32` (exact for every literal num_wide accepts, negative ones included: two's complement masking)
pub fn num32(s0: &str) -> Option<u32> {
    num_wide(s0).map(|v| (v & M32 as i128) as u32)
}

/// pyemu.num, as an i128 (hex up to 16 digits, decimal up to 18)
fn num_wide(s0: &str) -> Option<i128> {
    if !plain(s0) {
        return None;
    }
    let s = strip(s0);
    let low = s.to_ascii_lowercase();
    if low.trim_start_matches('-').starts_with("0x") && !s.starts_with('-') {
        // int(s, 16): s = 0x.. / 0X..
        return hexdigits(&s[2..]);
    }
    if !s.starts_with("-0x") {
        // int(s, 0)
        if let Some(r) = s.strip_prefix("-0X") {
            return hexdigits(r).map(|v| -v);
        }
        let (neg, body) = if let Some(r) = s.strip_prefix('-') {
            (true, r)
        } else if let Some(r) = s.strip_prefix('+') {
            (false, r)
        } else {
            (false, s)
        };
        return decdigits(body).map(|v| if neg { -(v as i128) } else { v as i128 });
    }
    // -int(s[1:], 16), s[1:] = 0x..
    hexdigits(&s[3..]).map(|v| -v)
}

/// CPU.get(s)
pub fn get(s0: &str) -> Option<G> {
    if !plain(s0) {
        return None;
    }
    let s = strip(s0);
    if let Some(r) = reg(s) {
        return Some(G::R(r));
    }
    num32(s).map(G::I)
}

/// CPU.put(s, v): 'sp' / 'mdr' / (d if s[0] == 'd' else a)[int(s[1])]   (no strip)
pub fn put(s: &str) -> Option<R> {
    if !plain(s) {
        return None;
    }
    if s == "sp" {
        return Some(R::Sp);
    }
    if s == "mdr" {
        return Some(R::Mdr);
    }
    let b = s.as_bytes();
    if b.len() < 2 || !(b'0'..=b'3').contains(&b[1]) {
        return None;
    }
    let i = b[1] - b'0';
    Some(if b[0] == b'd' { R::D(i) } else { R::A(i) })
}

/// CPU.ea(s)
pub fn ea(s0: &str) -> Option<Ea> {
    if !plain(s0) {
        return None;
    }
    let t = strip(s0);
    let inner = if t.len() >= 2 { &t[1..t.len() - 1] } else { "" };
    if inner.contains(',') {
        let parts: Vec<&str> = inner.split(',').collect();
        if parts.len() != 2 {
            return None;
        }
        let (x, y) = (strip(parts[0]), strip(parts[1]));
        let dx = match reg(x) {
            Some(r) => X::R(r),
            None => X::I(num(x)?),
        };
        return Some(Ea::P(dx, get(y)?));
    }
    if let Some(r) = reg(inner) {
        return Some(Ea::R(r));
    }
    num32(inner).map(Ea::I)
}

fn target(s: &str) -> Option<T> {
    if s.starts_with('(') {
        Some(T::Ea(ea(s)?))
    } else {
        Some(T::I(num32(s)?))
    }
}

fn cond(s: &str) -> Option<Cond> {
    Some(match s {
        "eq" => Cond::Eq,
        "ne" => Cond::Ne,
        "gt" => Cond::Gt,
        "ge" => Cond::Ge,
        "le" => Cond::Le,
        "lt" => Cond::Lt,
        "hi" => Cond::Hi,
        "cc" => Cond::Cc,
        "ls" => Cond::Ls,
        "cs" => Cond::Cs,
        "ra" => Cond::Ra,
        _ => return None,
    })
}

/// [r for r in s.strip('[]').split(',') if r]
fn reglist(s: &str) -> Vec<&str> {
    s.trim_matches(|c| c == '[' || c == ']').split(',').filter(|r| !r.is_empty()).collect()
}

/// decode (mn, o) exactly as CPU._step's if-chain would interpret it; None-producing parses -> Unsupported
pub fn decode(mn: &str, o: &[String]) -> Op {
    decode_opt(mn, o).unwrap_or(Op::Unsupported)
}

fn decode_opt(mn: &str, o: &[String]) -> Option<Op> {
    if !plain(mn) || !o.iter().all(|s| plain(s)) {
        return None;
    }
    let a = |i: usize| -> Option<&str> { o.get(i).map(|s| s.as_str()) };
    Some(match mn {
        "mov" | "movbu" | "movhu" => {
            let w = match mn {
                "mov" => 4,
                "movhu" => 2,
                _ => 1,
            };
            let (src, dst) = (a(0)?, a(1)?);
            if dst.starts_with('(') {
                Op::St { w, ea: ea(dst)?, src: get(src)? }
            } else if src.starts_with('(') {
                Op::Ld { w, ea: ea(src)?, dst: put(dst)? }
            } else {
                Op::Mov { src: get(src)?, dst: put(dst)? }
            }
        }
        "clr" => Op::Clr { dst: put(a(0)?)? },
        "ext" => Op::ExtMdr { g: get(a(0)?)? },
        "nop" | "setlb_" => Op::Nop,
        "extb" | "extbu" | "exth" | "exthu" => {
            let k = match mn {
                "extb" => Ext::B,
                "extbu" => Ext::Bu,
                "exth" => Ext::H,
                _ => Ext::Hu,
            };
            Op::Ext { k, g: get(a(0)?)?, p: put(a(0)?)? }
        }
        "add" => Op::Add { s: get(a(0)?)?, d: get(a(1)?)?, p: put(a(1)?)? },
        "sub" => Op::Sub { s: get(a(0)?)?, d: get(a(1)?)?, p: put(a(1)?)? },
        "cmp" => Op::Cmp { s: get(a(0)?)?, d: get(a(1)?)? },
        "and" | "or" | "xor" => {
            let k = match mn {
                "and" => Lg::And,
                "or" => Lg::Or,
                _ => Lg::Xor,
            };
            Op::Logic { k, s: get(a(0)?)?, d: get(a(1)?)?, p: put(a(1)?)? }
        }
        "not" => Op::Not { g: get(a(0)?)?, p: put(a(0)?)? },
        "asl" | "lsr" | "asr" => {
            let k = match mn {
                "asl" => Sh::Asl,
                "lsr" => Sh::Lsr,
                _ => Sh::Asr,
            };
            if o.len() > 1 {
                Op::Shift { k, cnt: Some(get(a(0)?)?), g: get(a(1)?)?, p: put(a(1)?)? }
            } else {
                Op::Shift { k, cnt: None, g: get(a(0)?)?, p: put(a(0)?)? }
            }
        }
        "asl2" => Op::Asl2 { g: get(a(0)?)?, p: put(a(0)?)? },
        "inc" | "inc4" => {
            let o0 = a(0)?;
            Op::Inc { g: get(o0)?, p: put(o0)?, flags: mn == "inc" && matches!(o0, "d0" | "d1" | "d2" | "d3"), add: if mn == "inc4" { 4 } else { 1 } }
        }
        "udf00" | "udf01" | "udfu00" | "udfu01" | "mul" | "mulu" => Op::Mul {
            signed: matches!(mn, "udf00" | "udfu00" | "mul"),
            setmdr: matches!(mn, "mul" | "mulu"),
            a: get(a(0)?)?,
            b: get(a(1)?)?,
            p: put(a(1)?)?,
        },
        "div" | "divu" => Op::Div { signed: mn == "div", a: get(a(0)?)?, b: get(a(1)?)?, p: put(a(1)?)? },
        "btst" => Op::Btst { a: get(a(0)?)?, b: get(a(1)?)? },
        "setlb" => Op::Setlb,
        "jmp" => Op::Jmp { t: target(a(0)?)? },
        "call" => {
            let tgt = num32(a(0)?)?;
            let regs = match a(1) {
                Some(s) => reglist(s).into_iter().map(get).collect::<Option<Vec<G>>>()?,
                None => vec![],
            };
            let imm8 = match a(2) {
                Some(s) => num(s)?,
                None => 0,
            };
            Op::Call { tgt, regs, imm8 }
        }
        "movm" => {
            let ok = |r: &&str| matches!(*r, "d2" | "d3" | "a2" | "a3");
            if a(1)? == "(sp)" {
                let rl = reglist(a(0)?);
                if !rl.iter().all(ok) {
                    return None;
                }
                Op::MovmPush { regs: rl.into_iter().map(reg).collect::<Option<Vec<R>>>()? }
            } else {
                if a(0)? != "(sp)" {
                    return None;
                }
                let rl = reglist(a(1)?);
                if !rl.iter().all(ok) {
                    return None;
                }
                Op::MovmPop { regs: rl.into_iter().map(put).collect::<Option<Vec<R>>>()? }
            }
        }
        "ret" | "retf" => {
            let (regs, imm8) = ret_operands(o)?;
            Op::Ret { retf: mn == "retf", regs, imm8 }
        }
        "calls" => Op::Calls { t: target(a(0)?)? },
        "bset" | "bclr" => Op::Bit { set: mn == "bset", mask: get(a(0)?)?, ea: ea(a(1)?)? },
        "rets" => Op::Rets,
        _ => {
            let b = mn.as_bytes();
            if b.len() >= 1 && b[0] == b'l' {
                if let Some(c) = cond(&mn[1..]) {
                    if o.is_empty() {
                        return Some(Op::Lcc { c });
                    }
                    // `l<cc>` with operands: not an Lcc; python continues the chain (b.. / jmp / ... none match) -> NotImplementedError
                    return None;
                }
            }
            if b.len() >= 1 && b[0] == b'b' {
                if let Some(c) = cond(&mn[1..]) {
                    return Some(Op::Bcc { c, tgt: num32(a(0)?)? });
                }
            }
            return None;
        }
    })
}

/// pyemu.ret_operands, registers as put() targets
fn ret_operands(o: &[String]) -> Option<(Vec<R>, i64)> {
    if !o.is_empty() && o[0].starts_with('[') {
        let regs = reglist(&o[0]).into_iter().map(put).collect::<Option<Vec<R>>>()?;
        let imm8 = if o.len() > 1 { num(&o[1])? } else { 0 };
        Some((regs, imm8))
    } else if !o.is_empty() {
        Some((vec![], num(&o[0])?))
    } else {
        Some((vec![], 0))
    }
}

#[cfg(test)]
mod t {
    use super::*;
    #[test]
    fn nums() {
        assert_eq!(num("0x10"), Some(16));
        assert_eq!(num("0X10"), Some(16));
        assert_eq!(num("-0x10"), Some(-16));
        assert_eq!(num("-0X10"), Some(-16));
        assert_eq!(num("12"), Some(12));
        assert_eq!(num("-4"), Some(-4));
        assert_eq!(num("+4"), Some(4));
        assert_eq!(num("00"), Some(0));
        assert_eq!(num("012"), None);
        assert_eq!(num("--0x5"), None);
        assert_eq!(num(" 7 "), Some(7));
        assert_eq!(num("0x"), None);
    }

    #[test]
    fn registers_and_immediates() {
        assert!(matches!(get("d0"), Some(G::R(R::D(0)))));
        assert!(matches!(get(" a3 "), Some(G::R(R::A(3)))));
        assert!(matches!(get("sp"), Some(G::R(R::Sp))));
        assert!(matches!(get("mdr"), Some(G::R(R::Mdr))));
        assert!(matches!(get("-1"), Some(G::I(0xffff_ffff))));
        assert!(matches!(get("0x80000000"), Some(G::I(0x8000_0000))));
        assert!(get("1_0").is_none() || matches!(get("1_0"), Some(G::I(_))), "must decode or refuse, never panic");
        assert!(matches!(put("d2"), Some(R::D(2))));
        assert!(put("d5").is_none(), "a register that does not exist must fall back to Python (which raises)");
        // pyemu quirk, mirrored on purpose: put() indexes by the first letter (d -> data, anything else -> address) and the digit
        assert!(matches!(put("x1"), Some(R::A(1))));
    }

    #[test]
    fn effective_addresses() {
        assert!(matches!(ea("(a0)"), Some(Ea::R(R::A(0)))));
        assert!(matches!(ea("(0x1000)"), Some(Ea::I(0x1000))));
        assert!(matches!(ea("(8,a1)"), Some(Ea::P(X::I(8), G::R(R::A(1))))));
        assert!(matches!(ea("(d1,a2)"), Some(Ea::P(X::R(R::D(1)), G::R(R::A(2))))));
        assert!(ea("(x,y,z)").is_none(), "three parts is not an addressing mode");
    }

    #[test]
    fn malformed_forms_are_unsupported_not_guessed() {
        // the exactness rule: anything the mirror cannot reproduce exactly decodes to Unsupported, so Python's _step runs (and raises)
        let s = |v: &[&str]| v.iter().map(|x| x.to_string()).collect::<Vec<_>>();
        assert!(matches!(decode("mov", &s(&["d0"])), Op::Unsupported));
        assert!(matches!(decode("foo", &s(&[])), Op::Unsupported));
        assert!(matches!(decode("add", &s(&["d9", "d0"])), Op::Unsupported));
    }
}
