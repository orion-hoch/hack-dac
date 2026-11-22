"""Metadata stripping for JPEG and PNG without touching the coded pixels. Owned by the PIXELS builder.

strip_jpeg(data) -> (bytes, removed)   removed: list of short names such as "APP1:Exif", "COM", "trailer:1234"
strip_png(data)  -> (bytes, removed)
Both return the input object unchanged (same bytes) when there is nothing to remove, and never raise on
malformed input: they return (data, ["error:..."]) instead.

JPEG: keeps APP0 JFIF, APP2 ICC_PROFILE, APP14 Adobe (colour transform) and every non-APP marker; drops APP1 (Exif,
XMP), APP2 other than ICC (MPF, FlashPix), APP3..APP13, APP15, COM, APP0 JFXX thumbnails, and bytes after EOI
(appended second images, vendor trailers). An Exif Orientation other than 1 is kept as a minimal Exif segment
holding only that tag, so viewers still show the picture the same way up. Entropy-coded data is copied byte for byte.
PNG: keeps critical chunks and the ancillary chunks that change how pixels look or animate; drops tEXt, zTXt, iTXt,
tIME, eXIf (Orientation kept as a minimal eXIf), and unknown ancillary chunks (Fireworks mkBF and similar private data).
"""
import struct, zlib

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_KEEP_ANC = {b"gAMA", b"cHRM", b"sRGB", b"iCCP", b"sBIT", b"bKGD", b"pHYs", b"tRNS", b"hIST", b"sPLT", b"cICP",
                 b"mDCv", b"cLLi", b"acTL", b"fcTL", b"fdAT", b"oFFs", b"pCAL", b"sCAL", b"sTER"}


def _exif_orientation(tiff):
    """Orientation (tag 0x0112) from a TIFF-structured Exif block, or None."""
    try:
        bo = "<" if tiff[:2] == b"II" else ">"
        off = struct.unpack(bo + "I", tiff[4:8])[0]
        n = struct.unpack(bo + "H", tiff[off:off + 2])[0]
        for i in range(n):
            e = tiff[off + 2 + 12 * i: off + 14 + 12 * i]
            tag, typ, cnt = struct.unpack(bo + "HHI", e[:8])
            if tag == 0x0112:
                return struct.unpack(bo + "H", e[8:10])[0]
    except Exception:
        return None
    return None


def _min_tiff(orientation):
    return b"MM\x00*" + struct.pack(">I", 8) + struct.pack(">H", 1) + struct.pack(">HHIHH", 0x0112, 3, 1, orientation, 0) + struct.pack(">I", 0)


def strip_jpeg(data):
    try:
        if data[:2] != b"\xff\xd8":
            return data, ["error:not a jpeg"]
        out = bytearray(b"\xff\xd8"); removed = []; i = 2; n = len(data); orient = None
        keep_orient_at = None

        def seg_name(m, body):
            if m == 0xFE:
                return "COM"
            tag = body[:12].split(b"\x00")[0][:16].decode("latin-1", "replace")
            return "APP%d:%s" % (m - 0xE0, tag)

        def wanted(m, body):
            if m == 0xFE:
                return False
            if m == 0xE1 and body[:6] == b"Exif\x00\x00" and len(body) == 6 + len(_min_tiff(1)):
                o = _exif_orientation(body[6:])
                if o and body[6:] == _min_tiff(o):
                    return True             # our own minimal Orientation-only segment: keep (idempotent)
            if 0xE0 <= m <= 0xEF:
                a = m - 0xE0
                if a == 0:
                    return body.startswith(b"JFIF\x00")
                if a == 2:
                    return body.startswith(b"ICC_PROFILE\x00")
                if a == 14:
                    return body.startswith(b"Adobe")
                return False
            return True

        sos_seen = False
        while i < n:
            if data[i] != 0xFF:
                return data, ["error:bad marker at %d" % i]
            m = data[i + 1]
            if m == 0xFF:                       # fill byte
                i += 1; continue
            if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                out += data[i:i + 2]; i += 2; continue
            if m == 0xD9:                       # EOI
                out += b"\xff\xd9"; i += 2
                if i < n:
                    removed.append("trailer:%d" % (n - i))
                break
            L = struct.unpack(">H", data[i + 2:i + 4])[0]; body = data[i + 4:i + 2 + L]
            if wanted(m, body):
                out += data[i:i + 2 + L]
                if m == 0xE0 and keep_orient_at is None:
                    keep_orient_at = len(out)
            else:
                removed.append(seg_name(m, body))
                if m == 0xE1 and body.startswith(b"Exif\x00\x00") and orient is None:
                    orient = _exif_orientation(body[6:])
            i += 2 + L
            if m == 0xDA:                       # entropy-coded data follows: copy up to the next real marker
                j = i
                while True:
                    j = data.find(b"\xff", j)
                    if j < 0 or j + 1 >= n:
                        out += data[i:]; i = n; break
                    nx = data[j + 1]
                    if nx == 0x00 or 0xD0 <= nx <= 0xD7 or nx == 0xFF:
                        j += 1 if nx == 0xFF else 2; continue
                    out += data[i:j]; i = j; break
        if not removed:
            return data, []
        if orient and orient != 1:
            body = b"Exif\x00\x00" + _min_tiff(orient)
            seg = b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body
            at = keep_orient_at if keep_orient_at is not None else 2
            out[at:at] = seg; removed.append("kept:Orientation=%d" % orient)
        return bytes(out), removed
    except Exception as e:
        return data, ["error:%s" % type(e).__name__]


def strip_png(data):
    try:
        if not data.startswith(_PNG_SIG):
            return data, ["error:not a png"]
        out = bytearray(_PNG_SIG); removed = []; i = 8; n = len(data); orient = None; first_idat = None
        while i + 8 <= n:
            L, typ = struct.unpack(">I4s", data[i:i + 8]); end = i + 12 + L
            if end > n:
                return data, ["error:truncated chunk"]
            critical = not (typ[0] & 0x20)
            if critical or typ in _PNG_KEEP_ANC:
                if typ == b"IDAT" and first_idat is None:
                    first_idat = len(out)
                out += data[i:end]
            else:
                removed.append(typ.decode("latin-1"))
                if typ == b"eXIf":
                    orient = _exif_orientation(data[i + 8:i + 8 + L])
            i = end
            if typ == b"IEND":
                if i < n:
                    removed.append("trailer:%d" % (n - i))
                break
        if not removed:
            return data, []
        if orient and orient != 1:
            body = _min_tiff(orient); ch = struct.pack(">I", len(body)) + b"eXIf" + body
            ch += struct.pack(">I", zlib.crc32(b"eXIf" + body) & 0xFFFFFFFF)
            p = first_idat if first_idat is not None else 8 + 25
            out[p:p] = ch; removed.append("kept:Orientation=%d" % orient)
        return bytes(out), removed
    except Exception as e:
        return data, ["error:%s" % type(e).__name__]


# ---------------------------------------------------------------------------------------------- TIFF and GIF
# TIFF: identity-bearing tags are zeroed IN PLACE (value bytes set to 0, entry, count and offsets kept), so the file
# keeps its size and every strip and tile byte. Software and DateTime stay (tool strings and dates are not identities).
_TIFF_ZERO = {269, 270, 315, 316, 33432, 700, 33723, 34377, 37724, 0x9C9B, 0x9C9C, 0x9C9D, 0x9C9E, 0x9C9F}
_EXIF_ZERO = {0x9286, 0x927C, 0xA430, 0xA431, 0xA435}
_TSIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
_TNAME = {269: "DocumentName", 270: "ImageDescription", 315: "Artist", 316: "HostComputer", 33432: "Copyright",
          700: "XMP", 33723: "IPTC", 34377: "Photoshop", 37724: "ImageSourceData", 0x9286: "UserComment",
          0x927C: "MakerNote", 0xA430: "CameraOwnerName", 0xA431: "BodySerialNumber", 0xA435: "LensSerialNumber"}


def strip_tiff(data):
    try:
        if data[:4] not in (b"II*\x00", b"MM\x00*"):
            return data, ["error:not a classic tiff"]
        bo = "<" if data[:2] == b"II" else ">"
        out = bytearray(data); removed = []; n = len(data); seen = set()

        def walk(off, zero, sub=False):
            todo = [off]
            while todo:
                off = todo.pop()
                if not off or off in seen or off + 2 > n:
                    continue
                seen.add(off)
                cnt = struct.unpack(bo + "H", data[off:off + 2])[0]
                for i in range(cnt):
                    e = off + 2 + 12 * i
                    if e + 12 > n:
                        break
                    tag, typ, c = struct.unpack(bo + "HHI", data[e:e + 8])
                    size = _TSIZE.get(typ, 1) * c
                    if tag in (34665, 34853) and not sub:           # Exif IFD, GPS IFD
                        p = struct.unpack(bo + "I", data[e + 8:e + 12])[0]
                        walk(p, _EXIF_ZERO if tag == 34665 else None, sub=True)
                        continue
                    if zero is None or tag in zero:
                        at = e + 8 if size <= 4 else struct.unpack(bo + "I", data[e + 8:e + 12])[0]
                        if size and at + size <= n and any(data[at:at + size]):
                            out[at:at + size] = bytes(size)
                            removed.append(_TNAME.get(tag, "GPS" if zero is None else "tag%d" % tag))
                if not sub:
                    nx = off + 2 + 12 * cnt
                    if nx + 4 <= n:
                        todo.append(struct.unpack(bo + "I", data[nx:nx + 4])[0])
        walk(struct.unpack(bo + "I", data[4:8])[0], _TIFF_ZERO)
        return (bytes(out), removed) if removed else (data, [])
    except Exception as e:
        return data, ["error:%s" % type(e).__name__]


def strip_gif(data):
    """Drops comment extensions, application extensions other than looping (NETSCAPE2.0, ANIMEXTS1.0; XMP lives in
    one) and bytes after the trailer. Image data, palettes, graphic control and plain-text extensions are copied."""
    try:
        if data[:6] not in (b"GIF87a", b"GIF89a"):
            return data, ["error:not a gif"]
        n = len(data); flags = data[10]; i = 13 + (3 * (2 << (flags & 7)) if flags & 0x80 else 0)
        out = bytearray(data[:i]); removed = []

        def blocks(j):
            while j < n and data[j]:
                j += 1 + data[j]
            return j + 1
        while i < n:
            b = data[i]
            if b == 0x3B:
                out += b"\x3b"
                if i + 1 < n:
                    removed.append("trailer:%d" % (n - i - 1))
                break
            if b == 0x2C:
                f = data[i + 9]; j = i + 10 + (3 * (2 << (f & 7)) if f & 0x80 else 0)
                j = blocks(j + 1); out += data[i:j]; i = j; continue
            if b == 0x21:
                lab = data[i + 1]; j = blocks(i + 2)
                app = data[i + 3:i + 14] if lab == 0xFF else b""
                if lab == 0xFE:
                    removed.append("comment")
                elif lab == 0xFF and app not in (b"NETSCAPE2.0", b"ANIMEXTS1.0"):
                    removed.append("app:" + app.decode("latin-1", "replace"))
                else:
                    out += data[i:j]
                i = j; continue
            return data, ["error:bad block at %d" % i]
        return (bytes(out), removed) if removed else (data, [])
    except Exception as e:
        return data, ["error:%s" % type(e).__name__]
