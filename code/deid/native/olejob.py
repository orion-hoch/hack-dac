"""One same-size pass over an OLE native file (old SolidWorks, Inventor, Solid Edge, old NX). Owner: NATIVE builder.

1. Read every stream; find the zlib streams inside it (any offset, Adler-32 checked by zlib).
2. Harvest identity values from field slots in raw and inflated bytes, and from person properties (author, last
   saved by, manager, company, Inventor designer).
3. Edit raw bytes outside the zlib spans and the inflated bytes of each span (Editor, same length); refit each edited
   span to its exact original size (empty deflate blocks in front, Adler-32 at the end; NUL fill as last resort);
   a span that cannot fit is left unedited and the file is "partial" (council D5: no shortened stand-ins).
4. Person properties are written at their exact offsets (this also covers values under 4 characters).
5. Drawings (drawing=True): every embedded PNG and plausible DIB, in raw streams and inside zlib blocks, becomes a
   flat neutral picture of the same pixel size and byte length (council D6). Optional named-preview callback too.
6. Stream sizes never change. Free sectors, slack, unused mini sectors and the file tail are zeroed.
7. The output is re-parsed: same entries and sizes; every edited span inflates to the intended bytes.
"""
import bz2
import re
import struct
import zlib as _zl
from collections import Counter
from deid.api import Result
from deid.native.ole import Ole, prop_strings
from deid.native.textedit import Editor
from deid.native.zfit import fit_zlib_exact, zlib_spans, ZLIB_ONLY
from deid.native.preview import neutral_all

PERSON = {"SummaryInformation": {4, 8}, "DocumentSummaryInformation": {14, 15},
          "Zrxrt4arFafyu34gYa3l3ohgHg": {4, 8}, "Aaalpg0m0wzvuhc41dwauxbwJc": {14, 15},
          "PypkizqiUjudbposAayal4qdGf": {41}}


_SIGS = re.compile(rb"BZh[1-9]1AY&SY|\x28\xb5\x2f\xfd|PK\x03\x04|\x1f\x8b\x08|\x89PNG\r\n\x1a\n|\xff\xd8\xff")


def embedded_spans(s):
    """Spans of embedded containers and compressed data inside a raw stream (bzip2, zstd, zip member, gzip, PNG,
    JPEG). Raw edits never touch them (council: never edit inside an embedded container without recursing)."""
    out = []
    for m in _SIGS.finditer(s):
        a, sig = m.start(), m.group(0)
        end = None
        try:
            if sig[:3] == b"BZh":
                d = bz2.BZ2Decompressor()
                d.decompress(s[a:])
                end = len(s) - len(d.unused_data) if d.eof else len(s)
            elif sig == b"\x28\xb5\x2f\xfd":
                end = len(s)                      # no zstd decoder here: protect to the end of the stream
            elif sig == b"PK\x03\x04" and a + 30 <= len(s):
                cs, nl, el = struct.unpack_from("<I", s, a + 18)[0], *struct.unpack_from("<HH", s, a + 26)
                end = min(len(s), a + 30 + nl + el + cs)
            elif sig == b"\x1f\x8b\x08":
                d = _zl.decompressobj(31)
                d.decompress(s[a:])
                end = len(s) - len(d.unused_data) if d.eof else None
            elif sig[:4] == b"\x89PNG":
                e = s.find(b"IEND", a)
                end = e + 8 if e > 0 else len(s)
            else:
                e = s.find(b"\xff\xd9", a + 3)
                end = e + 2 if e > 0 else len(s)
        except Exception:
            end = None
        if end:
            out.append((a, end))
    return out


def process(data, ctx, tag, preview=None, extra_fields=(), drawing=False):
    o = Ole(data)
    E = Editor(ctx, extra_fields)
    items = []
    for e in o.streams():
        s = o.read(e)
        spans = zlib_spans(s) if len(s) >= 16 else []
        items.append((e, s, spans))
        E.harvest(s)
        for _, _, pl in spans:
            E.harvest(pl)
        nm = e["name"][1:] if e["name"][:1] == "\x05" else None
        if nm in PERSON:
            for fmtid, pid, off, ln, enc, v in prop_strings(s):
                if pid in PERSON[nm]:
                    E.add(v)
    st = Counter()
    notes = []
    expect = []
    for e, s, spans in items:
        new = bytearray(s)
        if preview is not None:
            pv = preview(e.get("path", e["name"]), s)
            if pv is not None and pv != s and len(pv) == len(s):
                o.write(e, pv)
                st["previews"] += 1
                continue
        if drawing:
            s0 = neutral_all(s)
            if s0 != s:
                st["pictures"] += 1
                new[:] = s0
        prot = embedded_spans(s)
        pos = 0
        for a, used, pl in spans + [(len(s), 0, None)]:
            if a > pos:
                w0 = pos
                for p0, p1 in sorted(x for x in prot if x[1] > pos and x[0] < a) + [(a, a)]:
                    if p0 > w0:
                        seg, k = E.edit(bytes(new[w0:p0]))
                        new[w0:p0] = seg
                    w0 = max(w0, p1)
                    if p0 != a:
                        st["embedded_spans_kept"] += 1
            if pl is not None:
                npl, k = E.edit(neutral_all(pl) if drawing else pl)
                if npl != pl:
                    fitted, how = fit_zlib_exact(npl, used, s[a:a + 2])
                    if fitted is None:
                        st["span_nofit"] += 1
                        notes.append("%s: zlib block in %s does not fit (%s); left" % (tag, e["name"][:30], how))
                    else:
                        new[a:a + used] = fitted
                        st["span_" + how.split("+")[0]] += 1
                        if "+nul" in how:
                            st["span_nul_fill"] += 1
                        if "+squeeze" in how:
                            st["span_squeezed"] += 1
                        if how.split("+")[0] not in ZLIB_ONLY:
                            st["span_needs_strong"] += 1
                        expect.append((e["i"], a, npl))
            pos = a + used
        nm = e["name"][1:] if e["name"][:1] == "\x05" else None
        if nm in PERSON:
            for fmtid, pid, off, ln, enc, v in prop_strings(bytes(new)):
                if pid in PERSON[nm] and v.strip(" \x00"):
                    r = E.sf(v)
                    if r != v:
                        b = r.encode(enc, "replace")
                        if len(b) == ln:
                            new[off:off + ln] = b
                            st["person_fields"] += 1
        if s[:2] == b"\xfe\xff":
            # every string property is a parsed slot (D9): edited whole, names of any length the editor allows
            for fmtid, pid, off, ln, enc, v in prop_strings(bytes(new)):
                if not v.strip(" \x00"):
                    continue
                v2 = E.edit_free(E.edit_fields(v))
                try:
                    v3, k = E.m.sub_text(v2, E.src)
                    if len(v3) == len(v2):
                        v2 = v3
                        E.count += k
                except Exception:
                    pass
                if v2 != v:
                    b = v2.encode(enc, "replace")
                    if len(b) == ln:
                        new[off:off + ln] = b
                        st["prop_slots"] += 1
        if bytes(new) != s:
            o.write(e, bytes(new))
            st["streams_edited"] += 1
    st["free_bytes_zeroed"] = o.blank_free(_name_pred(ctx, E))
    out = o.data()
    if out == data:
        return Result(None, status="unchanged", notes=list(dict.fromkeys(E.notes)))
    # re-parse and check
    o2 = Ole(out)
    if [(e["path"], e["size"], e["typ"]) for e in o2.entries if "path" in e] != \
            [(e["path"], e["size"], e["typ"]) for e in o.entries if "path" in e] or len(out) != len(data):
        return Result(None, status="failed", notes=["%s: re-parse differs" % tag])
    import zlib
    for i, a, npl in expect:
        s2 = o2.read(o2.entries[i])
        d = zlib.decompressobj()
        if d.decompress(s2[a:]) != npl:
            return Result(None, status="failed", notes=["%s: edited zlib block does not inflate back" % tag])
    notes = list(dict.fromkeys(E.notes + notes))
    notes.append("%s stats: %s" % (tag, ",".join("%s=%d" % kv for kv in sorted(st.items()))))
    return Result(out, changed=E.count + st["previews"] + st["pictures"], status="partial" if st["span_nofit"] else "ok", notes=notes)


def _name_pred(ctx, E):
    """True when a free region holds a name: a hit of the mapping's matcher (global table) or one of the identity-field
    values harvested from this file, in Latin-1/UTF-8 or UTF-16LE. Free bytes without a name are left untouched."""
    vals = [v for v in getattr(E, 'vals', {}).values() if v and len(v.strip()) >= 2]
    pats = [x for v in vals for x in (v.encode('latin-1', 'ignore'), v.encode('utf-16-le'))]
    eng = getattr(getattr(ctx.mapping, 'S', None), 'E', None)

    def pred(b):
        if not b.strip(b'\x00'):
            return False
        low = b.lower()
        if any(p and p.lower() in low for p in pats):
            return True
        if eng is not None:
            return bool(eng.find(b))
        return ctx.mapping.sub_bytes(b)[1] > 0
    return pred

