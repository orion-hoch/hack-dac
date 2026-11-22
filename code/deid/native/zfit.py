"""Deflate refitting at a fixed size, and a finder for zlib streams inside binary streams. Owner: NATIVE builder.

fit_raw_nul(plain, T): raw deflate of `plain` padded with trailing NUL bytes to exactly T bytes. This is the form
  github.com/KenM76/swformat (Apache-2.0) reports real SolidWorks accepting (its writer.py: comp + b"\\x00" * (csz -
  len(comp))). No swformat code is copied; only the padding rule is followed.
fit_zlib_exact(plain, T, head2): zlib stream of exactly T bytes whose Adler-32 ends at the last byte: empty non-final
  deflate blocks are put in front (5, 6, 7, 9, 10, ... bytes; gaps 1-4 and 8 cannot be built this way, so other
  encoder outputs are tried). Falls back to NUL padding after the Adler-32 when no exact form is found.
Encoders, cheapest first: zlib 6, zlib 9, zlib 9 memLevel 9, libdeflate 12, zopfli (inputs up to 1 MB).
"""
import struct
import zlib


def _z(p, lvl, mem=8):
    c = zlib.compressobj(lvl, zlib.DEFLATED, -15, mem)
    return c.compress(p) + c.flush()


def _ld(p, lvl=12):
    import deflate
    return bytes(deflate.deflate_compress(p, lvl))


def _zop(p):
    import zopfli.zlib
    return bytes(zopfli.zlib.compress(p))[2:-4]


ENCODERS = [("zlib6", lambda p: _z(p, 6)), ("zlib9", lambda p: _z(p, 9)), ("zlib9m9", lambda p: _z(p, 9, 9)),
            ("libdeflate12", _ld), ("zopfli", _zop)]
ZLIB_ONLY = {"zlib6", "zlib9", "zlib9m9"}


def candidates(plain, zop_max=1 << 20):
    for name, fn in ENCODERS:
        if name == "zopfli" and len(plain) > zop_max:
            continue
        yield name, fn(plain)


def _check_raw(raw, plain):
    try:
        d = zlib.decompressobj(-15)
        out = d.decompress(raw)
        return d.eof and out == plain and set(d.unused_data) <= {0}
    except zlib.error:
        return False


def fit_raw_nul(plain, T):
    """(bytes of length T, encoder name) or (None, 'too_big:<bytes over>')."""
    best = None
    for name, c in candidates(plain):
        if len(c) <= T:
            out = c + b"\x00" * (T - len(c))
            if _check_raw(out, plain):
                return out, name
        best = len(c) if best is None else min(best, len(c))
    return None, "too_big:%d" % (best - T)


def _prefix(gap):
    """Empty non-final deflate blocks, byte aligned, exactly `gap` bytes, or None."""
    def group(m):  # m empty fixed blocks + one empty stored block
        bits = []
        for _ in range(m):
            bits += [0, 1, 0] + [0] * 7
        bits += [0, 0, 0]
        while len(bits) % 8:
            bits.append(0)
        by = bytes(sum(b << i for i, b in enumerate(bits[k:k + 8])) for k in range(0, len(bits), 8))
        return by + b"\x00\x00\xff\xff"
    if gap == 0:
        return b""
    sizes = {len(group(m)): group(m) for m in range(8)}
    for s, g in sorted(sizes.items()):
        r = gap - s
        if r >= 0 and r % 5 == 0:
            return g + group(0) * (r // 5)
    for s1, g1 in sizes.items():
        for s2, g2 in sizes.items():
            r = gap - s1 - s2
            if r >= 0 and r % 5 == 0:
                return g1 + g2 + group(0) * (r // 5)
    return None


def fit_zlib_exact(plain, T, head2=b"\x78\x9c"):
    """(zlib stream of exactly T bytes, method) or (None, reason)."""
    adler = struct.pack(">I", zlib.adler32(plain) & 0xFFFFFFFF)
    best, nul = None, None
    for name, c in candidates(plain):
        inner = T - 6
        if len(c) <= inner:
            p = _prefix(inner - len(c))
            if p is not None:
                out = head2 + p + c + adler
                try:
                    if zlib.decompress(out) == plain:
                        return out, name
                except zlib.error:
                    pass
            if nul is None:
                nul = (head2 + c + adler + b"\x00" * (inner - len(c)), name + "+nul")
        best = len(c) if best is None else min(best, len(c))
    if nul is not None:
        return nul
    return None, "too_big:%d" % (best + 6 - T)


def zlib_spans(buf, min_out=16, max_spans=4096):
    """[(start, consumed, plain)] of zlib streams in buf that end cleanly with a valid Adler-32 (checked by zlib)."""
    out = []
    i, n = 0, len(buf)
    mv = memoryview(buf)
    while len(out) < max_spans:
        i = buf.find(b"\x78", i)
        if i < 0 or i + 8 > n:
            break
        flg = buf[i + 1]
        if (0x7800 | flg) % 31 == 0 and not flg & 0x20:
            d = zlib.decompressobj()
            try:
                plain = d.decompress(mv[i:])
                if d.eof and len(plain) >= min_out:
                    used = n - i - len(d.unused_data)
                    out.append((i, used, plain))
                    i += used
                    continue
            except zlib.error:
                pass
        i += 1
    return out
