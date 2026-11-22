"""STEP (ISO 10303-21) and IGES, edited only inside their strings, on the LOGICAL text. CORE builder.

Gap these close (measured in tests/test_andre_system.py): a raw byte matcher cannot see
  * STEP names broken across lines inside a quoted string ("Widefiel\\r\\nd Mirror") or written as \\X2\\...\\X0\\;
  * IGES values split at the 72-column boundary, and values that start right after the Hollerith "nH" prefix
    (a word-boundary rule rejects "12HJohn Doe" because 'H' is a letter).
Method: build the logical string with a byte span per character, run the substitution on the logical text, write
back only changed characters into their own spans. Stand-ins are same length per character, so no count, column,
record or sequence number moves. Plain functions (bytes in, bytes out) for reuse by the other pipeline:
    rewrite_step(data, sub, field=None, names=None) -> (bytes, n)
    rewrite_iges(data, sub, field=None, names=None) -> (bytes, n)
sub(text) -> (text, n) and field(value) -> value must keep length per character.
"""
import re
from deid.api import register, Result

HANDLER_VERSION = 1
_STR = re.compile(rb"'(?:[^']|'')*'|/\*.*?\*/", re.S)
_X2 = re.compile(rb"\\X2\\((?:[0-9A-Fa-f]{4})+)\\X0\\")
_X1 = re.compile(rb"\\X\\([0-9A-Fa-f]{2})")
_USER = re.compile(r"(?i)((?:[a-z]:)?[\\/]+(?:users|home|documents and settings|scratch\d*|u|usr/people)[\\/]+)([^\\/'\"\x00\r\n]{2,})")


def scrub(text, sub, field=None, names=None):
    """Logical text -> (new text, n). names: [(old, new)] own file names; field: user folder after /home/, C:\\Users\\."""
    n = 0
    if names:
        for a, b in names:
            if a and a != b and a in text and len(a) == len(b):
                n += text.count(a); text = text.replace(a, b)
    if field is not None:
        def f(m):
            nonlocal n
            v = field(m.group(2))
            if len(v) != len(m.group(2)):
                return m.group(0)
            n += v != m.group(2)
            return m.group(1) + v
        text = _USER.sub(f, text)
    sur = [i for i, ch in enumerate(text) if "\ud800" <= ch <= "\udfff"]
    if sur:                                    # lone surrogates (surrogateescape bytes, split \X2\ pairs): never sent
        t2 = list(text)                        # to the matcher, and put back unchanged afterwards
        for i in sur:
            t2[i] = "\ufffd"
        new, k = sub("".join(t2))
        if len(new) == len(text):
            new = list(new)
            for i in sur:
                new[i] = text[i]
            new = "".join(new)
    else:
        new, k = sub(text)
    if len(new) != len(text):
        return text, n
    return new, n + k


# ------------------------------------------------------------------ STEP
def _step_chars(b, s, e):
    """Logical chars of b[s:e] with spans and encoders. CR/LF inside a string are layout, not content."""
    out = []                                   # (char, start, end, kind)
    i = s
    while i < e:
        c = b[i]
        if c in (13, 10):
            i += 1; continue
        if c == 0x5C:                          # backslash
            m = _X2.match(b, i, e)
            if m:
                hx = m.group(1); j = m.start(1)
                for k in range(0, len(hx), 4):
                    cp = int(hx[k:k + 4], 16); out.append((chr(cp), j + k, j + k + 4, "raw" if 0xD800 <= cp <= 0xDFFF else "x2"));
                i = m.end(); continue
            m = _X1.match(b, i, e)
            if m:
                out.append((chr(int(m.group(1), 16)), m.start(1), m.end(1), "x1")); i = m.end(); continue
        if c < 0x80:
            out.append((chr(c), i, i + 1, "a")); i += 1; continue
        j = i + 1
        while j < e and j - i < 4 and 0x80 <= b[j] < 0xC0:
            j += 1
        try:
            out.append((b[i:j].decode("utf-8"), i, j, "u")); i = j
        except UnicodeDecodeError:
            out.append(("\ufffd", i, i + 1, "raw")); i += 1   # GBK and other non-UTF-8 bytes: opaque
    return out


def _put(buf, ch, s, e, kind):
    if kind == "x2":
        enc = b"%04X" % ord(ch) if ord(ch) < 0x10000 else None
    elif kind == "x1":
        enc = b"%02X" % ord(ch) if ord(ch) < 0x100 else None
    elif kind in ("a", "u"):
        enc = ch.encode("utf-8")
    else:
        enc = None
    if enc is None or len(enc) != e - s:
        return False
    buf[s:e] = enc
    return True


def _apply(buf, chars, new):
    n = 0
    for (old, s, e, kind), ch in zip(chars, new):
        if ch != old and _put(buf, ch, s, e, kind):
            n += 1
    return n


def _file_name_fields(b):
    """{token start: field index} for the strings of the HEADER FILE_NAME entity."""
    h = b.find(b"FILE_NAME", 0, 1 << 16)
    if h < 0:
        return {}
    res, depth, field, i = {}, 0, 0, h + 9
    while i < len(b) and i < h + (1 << 16):
        c = b[i:i + 1]
        if c == b"'":
            m = _STR.match(b, i)
            if not m:
                break
            res[m.start()] = field; i = m.end(); continue
        if c == b"(":
            depth += 1
        elif c == b")":
            depth -= 1
            if depth == 0:
                break
        elif c == b"," and depth == 1:
            field += 1
        i += 1
    return res


def rewrite_step(data, sub, field=None, names=None):
    return _step_piece(data, sub, field, names, _file_name_fields(data))


def _first_open(b, s, e):
    """Index of the first token opener (quote or comment start) in b[s:e], or -1."""
    q = b.find(b"'", s, e); c = b.find(b"/*", s, e)
    cand = [x for x in (q, c) if x >= 0]
    return min(cand) if cand else -1


def _safe_cut(buf):
    """Largest prefix length that ends outside every string and comment (scan starts outside a token)."""
    prev = 0
    for m in _STR.finditer(buf):
        j = _first_open(buf, prev, m.start())
        if j >= 0:
            return j
        prev = m.end()
    j = _first_open(buf, prev, len(buf))
    if j >= 0:
        return j
    return len(buf) - 1 if buf.endswith(b"/") else len(buf)


def rewrite_step_file(src, dst, sub, field=None, names=None, chunk=32 << 20):
    """Chunked STEP rewrite for files of 200 MB or more: memory about 3 x chunk. Output is byte-identical to
    rewrite_step(open(src).read()) (tests/test_core_v2.py t_chunk). Writes dst atomically; returns replacements."""
    import os
    n = 0; carry = b""; first = True
    with open(src, "rb") as f, open(dst + ".part", "wb") as o:
        while True:
            blk = f.read(chunk); eof = not blk
            buf = carry + blk
            cut = len(buf) if eof else _safe_cut(buf)
            piece, carry = buf[:cut], buf[cut:]
            if piece:
                out, k = _step_piece(piece, sub, field, names, _file_name_fields(piece) if first else {})
                o.write(out); n += k; first = False
            if eof:
                break
    os.replace(dst + ".part", dst)
    return n


def _step_piece(data, sub, field, names, fields):
    buf = bytearray(data); n = 0
    fast_spans, fast_text = [], []
    for m in _STR.finditer(data):
        s, e = m.span()
        if e - s <= 2:
            continue
        s, e = (s + 2, e - 2) if data[s:s + 1] == b"/" else (s + 1, e - 1)
        tok = data[s:e]
        fi = fields.get(m.start())
        if fi is None and b"\\X" not in tok and b"\r" not in tok and b"\n" not in tok:
            try:
                fast_text.append(tok.decode("utf-8")); fast_spans.append((s, e)); continue
            except UnicodeDecodeError:
                pass
        chars = _step_chars(data, s, e)
        text = "".join(c[0] for c in chars)
        if fi in (2, 3, 6) and field is not None and text.strip():
            new = field(text); k = int(new != text)
            if len(new) != len(text):
                new, k = scrub(text, sub, field, names)
        else:
            new, k = scrub(text, sub, field, names)
        if k and len(new) == len(text):
            n += k if _apply(buf, chars, new) else 0
    if fast_text:                               # one substitution call for all simple strings
        joined = "\x00".join(fast_text)
        new, k = scrub(joined, sub, field, names)
        if k and len(new) == len(joined):
            parts = new.split("\x00")
            if len(parts) == len(fast_spans):
                for (s, e), old, p in zip(fast_spans, fast_text, parts):
                    if p != old:
                        enc = p.encode("utf-8")
                        if len(enc) == e - s:
                            buf[s:e] = enc
                n += k
    return bytes(buf), n


# ------------------------------------------------------------------ IGES
def _iges_lines(data):
    """[(start, end_of_content)] per physical line."""
    out, i = [], 0
    while i < len(data):
        j = data.find(b"\n", i)
        j = len(data) if j < 0 else j + 1
        k = j
        while k > i and data[k - 1] in (10, 13):
            k -= 1
        out.append((i, k)); i = j
    return out


_HOL = re.compile(rb"[,;] *(\d+)H")


def _hollerith(joined, pd=b",", rd=b";"):
    """Yield (field number, start, end) of every Hollerith string in a joined section (logical offsets)."""
    field = 1
    first = re.match(rb" *(\d+)H", joined)
    pos = 0
    if first:
        L = int(first.group(1)); yield field, first.end(), first.end() + L; pos = first.end() + L
    pat = re.compile(re.escape(pd) + rb" *(\d+)H|" + re.escape(pd) + b"|" + re.escape(rd))
    while True:
        m = pat.search(joined, pos)
        if not m:
            return
        if m.group(0)[:1] == pd:
            field += 1
        if m.group(1) is not None:
            L = int(m.group(1)); yield field, m.end(), m.end() + L; pos = m.end() + L
        else:
            pos = m.end()


def rewrite_iges(data, sub, field=None, names=None):
    """Only the Start section, the Global section and the Hollerith strings of the Parameter section are read.
    Same length per character: no count, column, pointer or sequence number moves."""
    if len(data) < 80:
        return data, 0
    starts = [0]; starts += [m.end() for m in re.finditer(b"\n", data)]
    sect = {83: [], 71: [], 80: []}                         # S, G, P
    for s in starts:
        if s + 73 <= len(data):
            c = data[s + 72]
            if c in sect and data.find(b"\n", s, s + 73) < 0:
                sect[c].append(s)
    if not sect[71]:
        return data, 0
    buf = bytearray(data); n = 0
    jobs = []                                              # (logical text, [byte offsets], use field)
    def pos_of(lines, width, a, z):
        return [lines[o // width] + o % width for o in range(a, z)]
    sl = sect[83]
    if sl:
        j = b"".join(data[s:s + 72] for s in sl); jobs.append((j.decode("latin-1"), pos_of(sl, 72, 0, len(j)), False))
    gl = sect[71]; gj = b"".join(data[s:s + 72] for s in gl)
    pd, rd = b",", b";"
    if gj[:2] == b"1H":
        pd = gj[2:3]
        if gj[3:6] == pd + b"1H":
            rd = gj[6:7]
    for fno, a, z in _hollerith(gj, pd, rd):
        if z <= len(gj) and z > a:
            jobs.append((gj[a:z].decode("latin-1"), pos_of(gl, 72, a, z), fno in (21, 22)))
    pl = sect[80]
    if pl:
        pj = b"".join(data[s:s + 64] for s in pl)
        for fno, a, z in _hollerith(pj, pd, rd):
            if z <= len(pj) and z > a and any(65 <= c <= 122 for c in pj[a:z]):
                jobs.append((pj[a:z].decode("latin-1"), pos_of(pl, 64, a, z), False))
    plain = [(t, p) for t, p, f in jobs if not (f and field is not None and t.strip())]
    for t, p, f in jobs:                                   # identity fields first: G21 author, G22 organisation
        if f and field is not None and t.strip():
            new = field(t)
            if len(new) == len(t) and new != t:
                n += sum(_put1(buf, c, q) for o, c, q in zip(t, new, p) if o != c) > 0
    if plain:
        joined = "\x00".join(t for t, p in plain)
        new, k = scrub(joined, sub, field, names)
        if k and len(new) == len(joined):
            off = 0
            for t, p in plain:
                nt = new[off:off + len(t)]; off += len(t) + 1
                if nt != t:
                    for o, c, q in zip(t, nt, p):
                        if o != c:
                            _put1(buf, c, q)
            n += k
    return bytes(buf), n


def _put1(buf, ch, q):
    if ord(ch) < 256:
        buf[q] = ord(ch); return True
    return False


# ------------------------------------------------------------------ contract wrappers
def _ctx_funcs(ctx):
    sub = lambda t: ctx.mapping.sub_text(t, ctx.source)
    fld = lambda v: ctx.mapping.sub_field(v, ctx.source)
    old = ctx.old_path.rsplit("/", 1)[-1]; new = ctx.new_path.rsplit("/", 1)[-1]
    names = []
    if old != new and len(old) == len(new):
        names = [(old, new)]
        so, sn = old.rsplit(".", 1)[0], new.rsplit(".", 1)[0]
        if len(so) >= 4:
            names.append((so, sn))
    return sub, fld, names


@register("step", lambda p, h: p.endswith((".step", ".stp", ".p21")) or h.lstrip()[:12] == b"ISO-10303-21", priority=50)
def handle_step(data, ctx):
    try:
        sub, fld, names = _ctx_funcs(ctx)
        out, n = rewrite_step(data, sub, fld, names)
        return Result(out if n else None, n, "ok" if n else "unchanged")
    except Exception as e:
        return Result(None, status="failed", notes=["step rewrite error %s" % type(e).__name__])


@register("iges", lambda p, h: p.endswith((".igs", ".iges")), priority=50)
def handle_iges(data, ctx):
    try:
        sub, fld, names = _ctx_funcs(ctx)
        out, n = rewrite_iges(data, sub, fld, names)
        return Result(out if n else None, n, "ok" if n else "unchanged")
    except Exception as e:
        return Result(None, status="failed", notes=["iges rewrite error %s" % type(e).__name__])
