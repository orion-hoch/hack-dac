"""Containers embedded inside native CAD streams (Inventor, SolidWorks). Owner: NATIVE2 builder.

Survey of the full corpus (findings/build_native2.md): REAL embedded zip packages exist (xlsx design tables in
SolidWorks `Contents/OleItems` and OLE `Embedding N/Package`, xlsx in Inventor `UFRxDoc`, Autodesk asset packages in
Inventor `Protein` (u32 length + zip) and `MemberDocs/*`); bzip2 streams are Inventor graphics caches
(`CacheGraphics/*`, binary tessellation, no text slots); zstd frames are Inventor 2023+ `RSeStorage/B*|M*` segment
payloads. Only zip packages are edited here: bzip2 and zstd spans are located and reported, never touched.

spans(blob)                   -> [Span] of verified containers (zip: zipfile.testzip passes; bzip2 / zstd: the frame
                                 decodes to its end). Callers that run a text sweep over `blob` must skip these spans.
zip_same_size(z, member_fn)   -> (new zip bytes of EXACTLY len(z), replacements, notes). Every member is inflated,
                                 member_fn(name, plain) returns new plain bytes; a member whose new bytes have another
                                 length, or whose deflate cannot be refitted to its exact compressed size, is kept
                                 byte-identical (note). CRC-32 is updated in the local header, the data descriptor and
                                 the central directory. No offset or size field changes.
process(blob, ctx, label)     -> (new blob of the same length, replacements, notes): zip_same_size on every zip span,
                                 members handed to ctx.recurse(plain, "<old_path>!/<label>/<member>"); a member result of
                                 another length is not used.
Gate inside zip_same_size: the new package is re-read with zipfile (testzip clean, same names and sizes) and every
member inflates to exactly the intended bytes; else the span is returned unchanged.
"""
import bz2
import io
import struct
import zipfile
import zlib
from dataclasses import dataclass, field

try:
    import zstandard
except ImportError:        # zstd spans are then only located by magic, not verified
    zstandard = None

from deid.native import zfit

SIG_ZIP, SIG_EOCD = b"PK\x03\x04", b"PK\x05\x06"


@dataclass
class Span:
    kind: str
    start: int
    end: int
    info: dict = field(default_factory=dict)


def _zip_at(b, h):
    e = b.find(SIG_EOCD, h)
    while e >= 0 and e + 22 <= len(b):
        cl = struct.unpack_from("<H", b, e + 20)[0]
        try:
            z = zipfile.ZipFile(io.BytesIO(b[h:e + 22 + cl]))
            if z.testzip() is None and z.infolist():
                return e + 22 + cl, [i.filename for i in z.infolist()]
        except Exception:
            pass
        e = b.find(SIG_EOCD, e + 4)
    return None, None


def spans(blob):
    out, p = [], 0
    b = bytes(blob)
    while True:
        cands = [x for x in (b.find(SIG_ZIP, p), b.find(b"BZh", p), b.find(b"\x28\xb5\x2f\xfd", p)) if x >= 0]
        if not cands:
            return out
        h = min(cands)
        if b.startswith(SIG_ZIP, h):
            end, names = _zip_at(b, h)
            if end:
                out.append(Span("zip", h, end, {"members": len(names)}))
                p = end
                continue
        elif b.startswith(b"BZh", h) and b[h + 3:h + 4].isdigit() and b[h + 4:h + 10] == b"1AY&SY":
            d = bz2.BZ2Decompressor()
            try:
                d.decompress(b[h:])
                if d.eof:
                    end = len(b) - len(d.unused_data)
                    out.append(Span("bzip2", h, end))
                    p = end
                    continue
            except Exception:
                pass
        elif b.startswith(b"\x28\xb5\x2f\xfd", h) and zstandard is not None:
            try:
                o = zstandard.ZstdDecompressor().decompressobj()
                o.decompress(b[h:])
                if o.eof:
                    end = len(b) - len(o.unused_data)
                    out.append(Span("zstd", h, end))
                    p = end
                    continue
            except Exception:
                pass
        p = h + 1


def _deflate_exact(plain, T):
    """Raw deflate of `plain`, exactly T bytes, ending at the last byte (empty non-final blocks in front), or None."""
    try:
        r = zfit.fit_zlib_exact(plain, T + 6)
    except Exception:
        return None
    if not r or len(r) != T + 6 or r[-4:] != struct.pack(">I", zlib.adler32(plain)):
        return None
    raw = r[2:-4]
    d = zlib.decompressobj(-15)
    try:
        if d.decompress(raw) != plain or not d.eof or d.unused_data:
            return None
    except zlib.error:
        return None
    return raw


def zip_same_size(z, member_fn):
    z = bytes(z)
    zf = zipfile.ZipFile(io.BytesIO(z))
    out = bytearray(z)
    notes, n, want = [], 0, {}
    # central directory records, in order
    e = z.rfind(SIG_EOCD)
    cd_n, cd_size, cd_off = struct.unpack_from("<HII", z, e + 10)
    delta = (e - cd_size) - cd_off          # offsets may count from a prefix the package was stored after
    q, cds = e - cd_size, []
    for _ in range(cd_n):
        if z[q:q + 4] != b"PK\x01\x02":
            return z, 0, ["zip: central directory not where the EOCD says"]
        meth, crc, cs, us, nl, xl, cl = struct.unpack_from("<H4xIIIHHH", z, q + 10)
        lo = struct.unpack_from("<I", z, q + 42)[0] + delta
        cds.append((q, z[q + 46:q + 46 + nl], meth, crc, cs, us, lo))
        q += 46 + nl + xl + cl
    for cq, name, meth, crc, cs, us, lo in cds:
        if z[lo:lo + 4] != SIG_ZIP:
            notes.append("zip: local header missing")
            continue
        fl = struct.unpack_from("<H", z, lo + 6)[0]
        nl, xl = struct.unpack_from("<HH", z, lo + 26)
        ds = lo + 30 + nl + xl
        if fl & 1 or meth not in (0, 8) or us == 0:
            continue
        raw = z[ds:ds + cs]
        try:
            plain = raw if meth == 0 else zlib.decompress(raw, -15)
        except zlib.error:
            notes.append("zip: member does not inflate")
            continue
        nm = name.decode("utf-8" if fl & 0x800 else "cp437", "replace")
        new = member_fn(nm, plain)
        if new is None or new == plain:
            continue
        if len(new) != len(plain):
            notes.append("zip: %s: edited member has another length; kept" % nm[-40:])
            continue
        comp = new if meth == 0 else _deflate_exact(new, cs)
        if comp is None or len(comp) != cs:
            notes.append("zip: %s: no exact-size deflate; kept" % nm[-40:])
            continue
        c2 = struct.pack("<I", zlib.crc32(new))
        out[ds:ds + cs] = comp
        out[lo + 14:lo + 18] = c2
        out[cq + 16:cq + 20] = c2
        if fl & 8:                                        # data descriptor: [sig] crc csize usize
            dd = ds + cs
            if out[dd:dd + 4] == b"PK\x07\x08":
                dd += 4
            if struct.unpack_from("<I", z, dd)[0] == crc:
                out[dd:dd + 4] = c2
        want[nm] = new
        n += 1
    if not want:
        return z, 0, notes
    out = bytes(out)
    try:                                                  # gate
        z2 = zipfile.ZipFile(io.BytesIO(out))
        if z2.testzip() is not None or [i.filename for i in z2.infolist()] != [i.filename for i in zf.infolist()]:
            raise ValueError("member list")
        for i in z2.infolist():
            if i.filename in want and z2.read(i) != want[i.filename]:
                raise ValueError("member bytes")
    except Exception as x:
        return z, 0, notes + ["zip: gate refused the edited package (%s); kept" % str(x)[:60]]
    return out, n, notes


def process(blob, ctx, label="stream"):
    """Same-size edit of every zip package inside `blob` through ctx.recurse; bzip2 and zstd spans untouched."""
    b = bytes(blob)
    out, total, notes = bytearray(b), 0, []
    base = ctx.old_path + "!/" + label

    def member_fn(name, plain):
        try:
            r = ctx.recurse(plain, base + "/" + name)
        except Exception as x:
            notes.append("zip: recurse raised %s" % type(x).__name__)
            return None
        return r.data if r is not None and r.data is not None else None

    for s in spans(b):
        if s.kind != "zip":
            continue
        new, k, nt = zip_same_size(b[s.start:s.end], member_fn)
        notes += nt
        if k and len(new) == s.end - s.start:
            out[s.start:s.end] = new
            total += k
    return bytes(out), total, notes
