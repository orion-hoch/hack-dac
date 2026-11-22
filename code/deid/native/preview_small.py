"""Small drawing previews -> flat tile of the same pixel size and byte length (council ruling D6). Owner: NATIVE2.

Formats covered: raw JPEGs inside CATIA V5 drawings (.CATDrawing, 130x110 seen) and the per-sheet JPEG of old NX
drawings (OLE stream `images/drawings/SH<n>/preview`, 247x185). Part and assembly previews are never touched
(Creo part 256x256, NX part views): callers decide by file kind; catia_drawing() only acts on .CATDrawing.

flat_jpeg(w, h, components, T)   -> JPEG bytes of a flat neutral grey, w x h, same colour components, EXACTLY T bytes
                                    (COM segments after SOI pad the size), or None when T is too small.
jpeg_spans(blob)                 -> [(start, end, w, h, components)] of complete baseline/progressive JPEGs.
flat_same_size(blob, start, end) -> blob with that JPEG replaced (same length) or None. Gate: Pillow decodes the new
                                    JPEG to the same size and mode; length kept; bytes outside the span identical.
catia_drawing(data)              -> (new data or None, notes). Container check: `V5_CFV2` head and `CB__END` trailer
                                    kept, length kept, bytes outside the replaced JPEGs identical.
nx_preview_stream(stream)        -> new stream bytes (same length) or None, for the old-NX OLE preview stream.
"""
import io
import struct

from PIL import Image

GREY = 224


def _sof(b, s):
    """(w, h, components, progressive, end) of the JPEG starting at s, or None."""
    p, n = s + 2, len(b)
    info = None
    while p + 4 <= n:
        if b[p] != 0xFF:
            return None
        m = b[p + 1]
        if m == 0xFF:
            p += 1
            continue
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            p += 2
            continue
        if m == 0xD9:
            return None
        ln = struct.unpack_from(">H", b, p + 2)[0]
        if m in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack_from(">HH", b, p + 5)
            info = (w, h, b[p + 9], m == 0xC2)
        if m == 0xDA:                                # scan: entropy data until a marker that is not RST / stuffing
            q = p + 2 + ln
            while True:
                q = b.find(b"\xff", q)
                if q < 0 or q + 1 >= n:
                    return None
                c = b[q + 1]
                if c == 0x00 or 0xD0 <= c <= 0xD7 or c == 0xFF:
                    q += 1 if c == 0xFF else 2
                    continue
                if c == 0xD9:
                    return info + (q + 2,) if info else None
                p = q                                # another segment (progressive: more scans)
                break
            continue
        p += 2 + ln
    return None


def jpeg_spans(blob):
    out, p = [], 0
    while True:
        s = blob.find(b"\xff\xd8\xff", p)
        if s < 0:
            return out
        r = _sof(blob, s)
        if r:
            w, h, c, prog, e = r
            out.append((s, e, w, h, c))
            p = e
        else:
            p = s + 3


def flat_jpeg(w, h, comps, T):
    mode = "L" if comps == 1 else "RGB"
    img = Image.new(mode, (w, h), GREY if mode == "L" else (GREY, GREY, GREY))
    for q in (75, 70, 60, 50, 40, 30, 20, 10):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q, optimize=False, progressive=False)
        j = buf.getvalue()
        P = T - len(j)
        if P == 0:
            return j
        if P < 4:
            continue
        pad, rest = bytearray(), P
        while rest:
            k = min(rest, 65535 + 2)
            if rest - k in (1, 2, 3):                # leave at least 4 bytes for the last COM segment
                k = rest - 4
            pad += b"\xff\xfe" + struct.pack(">H", k - 2) + b"\x00" * (k - 4)
            rest -= k
        return j[:2] + bytes(pad) + j[2:]
    return None


def flat_same_size(blob, start, end):
    old = blob[start:end]
    r = _sof(old, 0)
    if not r:
        return None
    w, h, c = r[0], r[1], r[2]
    new = flat_jpeg(w, h, c, end - start)
    if new is None or len(new) != end - start:
        return None
    try:
        im = Image.open(io.BytesIO(new))
        im.load()
        im0 = Image.open(io.BytesIO(old))
        if im.size != (w, h) or im.mode != im0.mode:
            return None
    except Exception:
        return None
    out = blob[:start] + new + blob[end:]
    if len(out) != len(blob) or out[:start] != blob[:start] or out[end:] != blob[end:]:
        return None
    return out


def catia_drawing(data):
    if not data.startswith(b"V5_CFV2") or b"CB__END" not in data[-64:]:
        return None, ["catia preview: not a V5 container"]
    out, notes, k = bytes(data), [], 0
    for s, e, w, h, c in jpeg_spans(data):
        if w * h > 400 * 400:
            notes.append("catia preview: %dx%d JPEG left (not a small preview)" % (w, h))
            continue
        x = flat_same_size(out, s, e)
        if x is None:
            notes.append("catia preview: %dx%d JPEG could not be replaced at the same size" % (w, h))
            continue
        out, k = x, k + 1
    if not k:
        return None, notes
    if len(out) != len(data) or out[:16] != data[:16] or out[-64:] != data[-64:]:
        return None, notes + ["catia preview: container check failed"]
    return out, notes + ["catia preview: %d drawing preview(s) replaced by a flat tile" % k]


def nx_preview_stream(stream):
    sp = jpeg_spans(stream)
    if len(sp) != 1:
        return None
    return flat_same_size(stream, sp[0][0], sp[0][1])
