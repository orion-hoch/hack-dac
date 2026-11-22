"""Odd files and the plain-text fallback. CORE builder.

  * SolidWorks lock files "~$name.SLDPRT": the whole content is a Windows user name plus NUL (6 to 8 bytes seen),
    an identity by definition, so it goes through the field substitution, not a dictionary lookup.
  * FrontPage sidecars under _vti_cnf/: "vti_author:SR|DOMAIN\\user" style lines; author fields substituted.
  * Plain text and source code (also what archives hold that the corpus has nowhere else: KiCad, C, HTML,
    Markdown, JSON): whole-text substitution, same length per character, bytes outside UTF-8 kept opaque.
Plain functions: lockfile(data, field), vti(data, field, sub), plain(data, sub, field).
"""
import re
from deid.api import register, Result
from deid.handlers.text_step import scrub

HANDLER_VERSION = 2
TEXT_EXT = (".txt", ".md", ".html", ".htm", ".xml", ".json", ".geojson", ".csv", ".c", ".h", ".cpp", ".hpp", ".py",
            ".m", ".scad", ".kicad_mod", ".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_sym", ".kicad_prl", ".sch",
            ".lib", ".pro", ".cfg", ".ini", ".yaml", ".yml", ".gcode", ".nc", ".ngc", ".mtl", ".obj", ".wrl", ".vrml",
            ".unv", ".x_t", ".xmt_txt", ".sat", ".svg", ".dxf", ".ps", ".eps", ".tex", ".bat", ".sh", ".js", ".css",
            ".ino", ".rst", ".log", ".map", ".md5", ".gitignore", ".lst", ".bom", ".net", ".drl", ".gbr", ".gtl",
            ".gbl", ".gto", ".gbo", ".gts", ".gbs", ".gko", ".gm1", ".step", ".stp", ".igs", ".iges", ".dat", ".inp")


def lockfile(data, field):
    if not data or len(data) > 512:
        return data, 0
    for enc, nul in (("utf-16-le", b"\x00\x00"), ("latin-1", b"\x00")):
        if enc == "utf-16-le" and not (len(data) >= 4 and data[1] == 0 and data[3] == 0):
            continue
        end = data.find(nul) if enc == "latin-1" else next((i for i in range(0, len(data) - 1, 2) if data[i:i + 2] == nul), -1)
        end = len(data) if end < 0 else end
        name = data[:end].decode(enc, "replace")
        if not name.strip():
            return data, 0
        new = field(name)
        enc_new = new.encode(enc, "replace")
        if new == name or len(enc_new) != end:
            return data, 0
        return enc_new + data[end:], 1
    return data, 0


_VTI = re.compile(rb"(?m)^(vti_(?:author|modifiedby|title|description)[^:]*:[A-Z]{2}\|)([^\r\n]+)")


def vti(data, field, sub):
    n = 0
    def f(m):
        nonlocal n
        v = m.group(2).decode("latin-1"); nv = field(v)
        if len(nv) != len(v):
            return m.group(0)
        n += nv != v
        return m.group(1) + nv.encode("latin-1", "replace")
    out = _VTI.sub(f, data)
    out2, k = plain(out, sub, None)
    return out2, n + k


def plain(data, sub, field=None, names=None):
    text = data.decode("utf-8", "surrogateescape")
    new, k = scrub(text, sub, field, names)
    if not k:
        return data, 0
    out = new.encode("utf-8", "surrogateescape")
    if len(out) != len(data):                  # a stand-in changed byte width: keep only same-width characters
        b = bytearray(data); pos = 0; k = 0
        for a, c in zip(text, new):
            w = len(a.encode("utf-8", "surrogateescape"))
            if a != c and len(c.encode("utf-8", "surrogateescape")) == w:
                b[pos:pos + w] = c.encode("utf-8", "surrogateescape"); k += 1
            pos += w
        out = bytes(b)
    return out, k


def _funcs(ctx):
    return (lambda t: ctx.mapping.sub_text(t, ctx.source)), (lambda v: ctx.mapping.sub_field(v, ctx.source))


@register("lockfile", lambda p, h: p.rsplit("/", 1)[-1].startswith("~$") and len(h) <= 512, priority=95)
def handle_lock(data, ctx):
    sub, fld = _funcs(ctx)
    out, n = lockfile(data, fld)
    return Result(out if n else None, n, "ok" if n else "unchanged")


@register("vti", lambda p, h: "/_vti_cnf/" in p or "/_vti_pvt/" in p, priority=94)
def handle_vti(data, ctx):
    sub, fld = _funcs(ctx)
    out, n = vti(data, fld, sub)
    return Result(out if n else None, n, "ok" if n else "unchanged")


def _is_text(h):
    return h and b"\x00" not in h and sum(1 for b in h if b < 9 or 13 < b < 32) <= len(h) // 100


@register("text", lambda p, h: (p.endswith(TEXT_EXT) or p.endswith(".txt")) and _is_text(h), priority=10)
def handle_text(data, ctx):
    try:
        sub, fld = _funcs(ctx)
        out, n = plain(data, sub, fld)
        return Result(out if n else None, n, "ok" if n else "unchanged")
    except Exception as e:
        return Result(None, status="failed", notes=["text rewrite error %s" % type(e).__name__])


# ------------------------------------------------------------------ readable runs inside binaries (F3D members, headers)
_ASC = re.compile(rb"[\x20-\x7e]{6,}")
_U16 = re.compile(rb"(?:[\x20-\x7e]\x00){4,}")


def readable_runs(data, sub, field=None):
    """Same-length substitution inside readable ASCII runs (6+) and UTF-16LE runs (4+) of a binary blob only.
    Bytes outside those runs are never touched. Returns (bytes, n)."""
    buf = bytearray(data); n = 0
    for rx, enc in ((_ASC, "latin-1"), (_U16, "utf-16-le")):
        runs = [(mm.start(), mm.end(), mm.group(0).decode(enc)) for mm in rx.finditer(data)]
        if not runs:
            continue
        joined = "\x00".join(t for a, z, t in runs)
        new, k = scrub(joined, sub, field)
        if not k or len(new) != len(joined):
            continue
        off = 0
        for a, z, t in runs:
            nt = new[off:off + len(t)]; off += len(t) + 1
            if nt != t:
                b = nt.encode(enc, "replace")
                if len(b) == z - a:
                    buf[a:z] = b; n += 1
    return bytes(buf), n


_XB_FIELD = re.compile(rb"(?m)^((?:USER|MC_ID|SITE)=)([^;\r\n]*)(;)")


def rewrite_xb(data, sub, field=None, names=None):
    """Binary Parasolid (.x_b, .xmt_bin): edit only the readable header up to **END_OF_HEADER, same length.
    USER, MC_ID and SITE are identity fields; KEY and FILE follow the path plan through names and sub."""
    end = data.find(b"**END_OF_HEADER", 0, 1 << 16)
    if end < 0:
        return readable_head(data, sub, field, names)
    head = data[:end]
    if any(c < 9 or 13 < c < 32 or c > 126 for c in head):
        return data, 0
    n = 0
    if field is not None:
        def f(mm):
            nonlocal n
            v = mm.group(2).decode("latin-1"); nv = field(v)
            if nv != v and len(nv.encode("latin-1", "replace")) == len(mm.group(2)):
                n += 1; return mm.group(1) + nv.encode("latin-1", "replace") + mm.group(3)
            return mm.group(0)
        head = _XB_FIELD.sub(f, head)
    t = head.decode("latin-1"); nt, k = scrub(t, sub, field, names)
    if k and len(nt) == len(t):
        head = nt.encode("latin-1", "replace"); n += k
    return (head + data[end:]) if n else data, n


def readable_head(data, sub, field=None, names=None, minlen=32, maxlen=1 << 16):
    """Generic: the leading run of printable bytes of a binary file, edited in place, same length."""
    i = 0
    while i < len(data) and i < maxlen and (32 <= data[i] < 127 or data[i] in (9, 10, 13)):
        i += 1
    if i < minlen:
        return data, 0
    t = data[:i].decode("latin-1"); nt, k = scrub(t, sub, field, names)
    if not k or len(nt) != len(t):
        return data, 0
    return nt.encode("latin-1", "replace") + data[i:], k


F3D_BIN = re.compile(r"(?:InstanceProperties\.bin|DefinitionIteratorProperties\.bin|BulkStream\.dat|MetaStream\.dat|"
                     r"Manifest\.dat|Properties\.dat)$")


@register("parasolid_bin", lambda p, h: p.endswith((".x_b", ".xmt_bin")), priority=50)
def handle_xb(data, ctx):
    sub, fld = _funcs(ctx)
    old = ctx.old_path.rsplit("/", 1)[-1]; new = ctx.new_path.rsplit("/", 1)[-1]
    names = [(old, new), (old.rsplit(".", 1)[0], new.rsplit(".", 1)[0])] if old != new and len(old) == len(new) else None
    out, n = rewrite_xb(data, sub, fld, names)
    return Result(out if n else None, n, "ok" if n else "unchanged")


@register("f3d_member", lambda p, h: ".f3d!/" in p and F3D_BIN.search(p) is not None, priority=60)
def handle_f3d_member(data, ctx):
    sub, fld = _funcs(ctx)
    out, n = readable_runs(data, sub)
    return Result(out if n else None, n, "ok" if n else "unchanged")
