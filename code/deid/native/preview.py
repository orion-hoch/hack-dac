"""Neutral replacements for preview pictures inside drawing files, same pixel size, same byte length.
Owner: NATIVE builder. A PNG becomes a light-grey 8-bit greyscale PNG of the same width and height, padded with a
private ancillary chunk 'paDd' of zeros (decoders skip unknown ancillary chunks) so the byte length is unchanged;
bytes after IEND are kept. A DIB (optionally behind a 4-byte length) keeps its header and becomes light grey."""
import re
import struct
import zlib

SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(t, b):
    return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)


def neutral_png(orig, grey=0xF0):
    if orig[:8] != SIG or orig[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", orig[16:24])
    end = orig.find(b"IEND")
    if end < 0 or w * h > 64_000_000:
        return None
    tail = orig[end + 8:]
    body_len = end + 8
    idat = zlib.compress((b"\x00" + bytes([grey]) * w) * h, 9)
    png = SIG + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)) + _chunk(b"IDAT", idat)
    iend = _chunk(b"IEND", b"")
    pad = body_len - len(png) - len(iend) - 12
    if pad < 0:
        return None
    out = png + _chunk(b"paDd", bytes(pad)) + iend + tail
    return out if len(out) == len(orig) else None


def neutral_dib(orig):
    for o in (0, 4):
        if len(orig) >= o + 40 and struct.unpack_from("<I", orig, o)[0] in (40, 108, 124):
            hs, w, h, planes, bpp, comp, isz, _, _, clr = struct.unpack_from("<IiiHHIIiiI", orig, o)
            if planes != 1 or comp not in (0, 3) or bpp not in (1, 4, 8, 16, 24, 32):
                return None
            pal = (clr or (1 << bpp)) * 4 if bpp <= 8 else 0
            start = o + hs + pal + (12 if comp == 3 and hs == 40 else 0)
            stride = ((w * bpp + 31) // 32) * 4
            n = min(len(orig) - start, stride * abs(h))
            if n <= 0:
                return None
            b = bytearray(orig)
            if bpp <= 8:
                b[o + hs:o + hs + 4] = b"\xf0\xf0\xf0\x00"
                b[start:start + n] = bytes(n)
            else:
                b[start:start + n] = b"\xf0" * n
            return bytes(b)
    return None


def neutral(name, data):
    """For a stream or chunk named like a SolidWorks preview, its neutral form; None if not a preview."""
    base = name.rsplit("/", 1)[-1]
    if base == "PreviewPNG" or name.startswith("Images/Sheet_") or data[:8] == SIG and "review" in name:
        return neutral_png(data)
    if base == "Preview":
        return neutral_dib(data)
    return None


_DIBH = re.compile(rb"\x28\x00\x00\x00")


def neutral_all(b):
    """Every embedded PNG (signature .. IEND) and every plausible DIB in a drawing's blob made neutral, same length."""
    out = None
    i = 0
    while True:
        i = b.find(SIG, i)
        if i < 0:
            break
        e = b.find(b"IEND", i)
        if e < 0:
            break
        n = neutral_png(b[i:e + 8])
        if n is not None:
            out = out or bytearray(b)
            out[i:e + 8] = n
        i = e + 8
    for m in _DIBH.finditer(b):
        o = m.start()
        if o + 40 > len(b):
            break
        hs, w, h, planes, bpp, comp, isz, _, _, clr = struct.unpack_from("<IiiHHIIiiI", b, o)
        if planes != 1 or bpp not in (1, 4, 8, 16, 24, 32) or comp not in (0, 3) or not (16 <= w <= 8000) \
                or not (16 <= abs(h) <= 8000) or clr > 256:
            continue
        pal = (clr or (1 << bpp)) * 4 if bpp <= 8 else 0
        img = ((w * bpp + 31) // 32) * 4 * abs(h)
        size = 40 + pal + (12 if comp == 3 else 0) + img
        if o + size > len(b) or isz not in (0, img):
            continue
        n = neutral_dib(bytes(b[o:o + size]))
        if n is not None:
            out = out or bytearray(b)
            out[o:o + size] = n
    return bytes(out) if out is not None else b
