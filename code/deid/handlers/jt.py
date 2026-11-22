"""JT 10.x (JT Open). Owner: NATIVE2 builder.

Where names are: UTF-16 MbStrings inside XZ-compressed segments (LSG type 1, metadata type 4, type 31 in this
corpus): source paths (c:/users/<user>/desktop/<project>/...), part and file names. A plain byte search sees nothing.

Edit: each XZ segment is decompressed, edited at the same length by textedit.Editor (field slots such as the folder
after Users, harvested values anywhere, 8.3 forms, CAD file names through ctx.rename, the file's own stem, mapping),
recompressed with liblzma and re-framed as an XZ stream of EXACTLY the original byte length (block-header padding:
up to 1,012 bytes per block; more blocks when one header is not enough). No offset, segment length, compressed
length or table-of-contents entry changes. Segments that are not XZ-framed (shape LOD, type 7) are never touched.
Fallback, only when a segment cannot be fitted: segments are laid out again and the TOC, segment lengths and the
compressed-length field are rewritten (note "resized").
A text file named .jt (one in the corpus: a 494-byte placeholder) is edited as text, same length.

Gate before emitting (else the input goes out unchanged with a note): the output re-parses; every XZ segment passes
our own container check (CRC32 of stream header, block headers, index, footer) and decodes with liblzma to exactly the
intended payload; every other segment, the file header and the TOC are byte-identical (same size: the whole file
outside the edited XZ spans is byte-identical).
"""
import lzma
import os
import re
import struct
import zlib

from deid.api import register, Result
from deid.native.textedit import Editor
from deid.native.plain import make_transform

XZ = b"\xfd7zXZ\x00"
CHECK = {0: (lzma.CHECK_NONE, 0), 1: (lzma.CHECK_CRC32, 4), 4: (lzma.CHECK_CRC64, 8), 10: (lzma.CHECK_SHA256, 32)}


# ---------------------------------------------------------------- XZ container (own reader and writer)
def _vli(n):
    out = bytearray()
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def _rvli(b, p):
    n = s = 0
    for _ in range(9):
        c = b[p]
        p += 1
        n |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return n, p
    raise ValueError("xz varint")


def parse_xz(s):
    """One XZ stream -> (stream header (12 bytes), [(filter_flags, raw_lzma2, check, usize, header_size)]).
    Raises ValueError on any inconsistency (every CRC32 of the container is checked)."""
    s = bytes(s)
    if len(s) < 32 or s[:6] != XZ or s[-2:] != b"YZ":
        raise ValueError("xz magic")
    flags = s[6:8]
    if flags[0] != 0 or (flags[1] & 15) not in CHECK or struct.unpack_from("<I", s, 8)[0] != zlib.crc32(flags):
        raise ValueError("xz stream header")
    ft = s[-12:]
    if struct.unpack_from("<I", ft, 0)[0] != zlib.crc32(ft[4:10]) or ft[8:10] != flags:
        raise ValueError("xz footer")
    isz = (struct.unpack_from("<I", ft, 4)[0] + 1) * 4
    ix = len(s) - 12 - isz
    if ix < 12 or s[ix] != 0 or struct.unpack_from("<I", s, len(s) - 16)[0] != zlib.crc32(s[ix:len(s) - 16]):
        raise ValueError("xz index")
    n, p = _rvli(s, ix + 1)
    recs = []
    for _ in range(n):
        up, p = _rvli(s, p)
        us, p = _rvli(s, p)
        recs.append((up, us))
    if any(s[p:len(s) - 16]):
        raise ValueError("xz index padding")
    csz = CHECK[flags[1] & 15][1]
    blocks, q = [], 12
    for up, us in recs:
        hs = (s[q] + 1) * 4
        hdr = s[q:q + hs]
        if s[q] == 0 or struct.unpack_from("<I", hdr, hs - 4)[0] != zlib.crc32(hdr[:hs - 4]):
            raise ValueError("xz block header")
        bf = hdr[1]
        if bf & 0x3C:
            raise ValueError("xz block flags")
        r = 2
        if bf & 0x40:
            _, r = _rvli(hdr, r)
        if bf & 0x80:
            _, r = _rvli(hdr, r)
        f0 = r
        for _ in range((bf & 3) + 1):
            _, r = _rvli(hdr, r)
            ps, r = _rvli(hdr, r)
            r += ps
        if r > hs - 4 or any(hdr[r:hs - 4]):
            raise ValueError("xz block header padding")
        cs = up - hs - csz
        if cs <= 0:
            raise ValueError("xz block size")
        raw = s[q + hs:q + hs + cs]
        pad = (-cs) % 4
        if any(s[q + hs + cs:q + hs + cs + pad]):
            raise ValueError("xz block padding")
        chk = s[q + hs + cs + pad:q + hs + cs + pad + csz]
        blocks.append((hdr[f0:r], raw, chk, us, hs, (bf & 3) + 1))
        q += hs + cs + pad + csz
    if q != ix:
        raise ValueError("xz block sizes")
    return s[:12], blocks


def build_xz(sh, blocks, extra):
    """XZ stream from parsed blocks; extra[i] = bytes of NUL padding added to block i's header (multiple of 4)."""
    out = bytearray(sh)
    recs = []
    for (filt, raw, chk, us, _, nf), x in zip(blocks, extra):
        hs = ((2 + len(filt) + 4 + 3) // 4) * 4 + x
        if hs > 1024:
            raise ValueError("header too large")
        h = bytearray([hs // 4 - 1, nf - 1]) + filt
        h += b"\0" * (hs - 4 - len(h))
        h += struct.pack("<I", zlib.crc32(h))
        out += h + raw + b"\0" * ((-len(raw)) % 4) + chk
        recs.append((hs + len(raw) + len(chk), us))
    ix = bytearray(b"\0") + _vli(len(recs))
    for up, us in recs:
        ix += _vli(up) + _vli(us)
    ix += b"\0" * ((-len(ix)) % 4)
    ix += struct.pack("<I", zlib.crc32(ix))
    out += ix
    bs = struct.pack("<I", len(ix) // 4 - 1) + sh[6:8]
    out += struct.pack("<I", zlib.crc32(bs)) + bs + b"YZ"
    return bytes(out)


def _dict_size(filt):
    """LZMA2 dictionary size from the block's filter flags (id 0x21, 1 property byte)."""
    if len(filt) >= 3 and filt[0] == 0x21 and filt[1] == 1 and filt[2] <= 40:
        d = filt[2]
        return 0xFFFFFFFF if d == 40 else (2 | (d & 1)) << (d // 2 + 11)
    return None


def _pad_to(sh, bl, target):
    """Distribute NUL header padding over the blocks so the stream is exactly `target` bytes, or None."""
    maxx = [1024 - ((2 + len(b[0]) + 4 + 3) // 4) * 4 for b in bl]
    extra = [0] * len(bl)
    for _ in range(16):
        cur = build_xz(sh, bl, extra)
        diff = target - len(cur)
        if diff == 0:
            return cur
        if diff > 0:
            for i in range(len(extra)):
                a = min(diff, maxx[i] - extra[i])
                extra[i] += a
                diff -= a
            if diff > 0:
                return None             # not enough header room with these blocks
        else:
            if not any(extra):
                return None             # minimal stream already too large
            for i in reversed(range(len(extra))):
                a = min(-diff, extra[i])
                extra[i] -= a
                diff += a
    return None


PRESETS = (6, 9 | lzma.PRESET_EXTREME, 9, 8, 7, 5, 4, 3, 2, 1, 0)


def fit_xz(orig, payload):
    """XZ stream holding `payload`, exactly len(orig) bytes, same check type and LZMA2 dictionary size as `orig`,
    or None. Size control: block-header padding (up to 1,012 bytes per block), the number of blocks, the preset.
    First preset 6 in one block; else the presets whose one-block stream is not larger than the target, least
    slack first, each with as many blocks as the slack needs."""
    sh, ob = parse_xz(orig)
    target = len(orig)
    chk = CHECK[sh[7] & 15][0]
    ds = _dict_size(ob[0][0])
    if ds is None:
        return None

    def comp(preset, k):
        step = max(1, -(-len(payload) // k))
        pieces = [payload[i:i + step] for i in range(0, len(payload), step)] or [b""]
        bl = []
        for pc in pieces:
            bl += parse_xz(lzma.compress(pc, format=lzma.FORMAT_XZ, check=chk,
                                         filters=[{"id": lzma.FILTER_LZMA2, "preset": preset, "dict_size": ds}]))[1]
        return bl

    cands = []
    for preset in PRESETS:
        bl = comp(preset, 1)
        r = _pad_to(sh, bl, target)
        if r is not None:
            return r
        s0 = len(build_xz(sh, bl, [0]))
        if s0 <= target:
            cands.append((target - s0, preset))
    for slack, preset in sorted(cands)[:4]:
        k0 = max(2, -(-slack // 1000))
        for k in range(k0, min(k0 + 4, 129)):
            if k > len(payload) // 64:
                break
            r = _pad_to(sh, comp(preset, k), target)
            if r is not None:
                return r
    return None


# ---------------------------------------------------------------- JT
def parse_jt(raw):
    """[(index, toc_entry_pos, offset, length, type, xz_start or None, xz_len)] and the TOC offset."""
    if raw[:8] != b"Version " or len(raw) < 109 or raw[80] not in (0, 1):
        raise ValueError("not a JT 10 header")
    if raw[80] != 0:
        raise ValueError("big-endian JT not handled")
    toc = struct.unpack_from("<Q", raw, 85)[0]
    n = struct.unpack_from("<i", raw, toc)[0]
    if not 0 < n < 100000 or toc + 4 + 32 * n > len(raw):
        raise ValueError("JT TOC")
    segs = []
    for i in range(n):
        q = toc + 4 + 32 * i
        off = struct.unpack_from("<Q", raw, q + 16)[0]
        ln = struct.unpack_from("<i", raw, q + 24)[0]
        if off + ln > len(raw) or ln < 24 or raw[off:off + 16] != raw[q:q + 16]:
            raise ValueError("JT segment %d header" % i)
        st, sl = struct.unpack_from("<ii", raw, off + 16)
        if sl != ln:
            raise ValueError("JT segment %d length" % i)
        xs = xl = None
        if ln > 48 and struct.unpack_from("<i", raw, off + 24)[0] == 3 and raw[off + 32] == 3 \
                and raw[off + 33:off + 39] == XZ:
            cl = struct.unpack_from("<i", raw, off + 28)[0]
            if off + 33 + cl - 1 != off + ln:
                raise ValueError("JT segment %d compressed length" % i)
            xs, xl = off + 33, cl - 1
        segs.append((i, q, off, ln, st, xs, xl))
    return segs, toc


def _text(data, ctx):
    ed = Editor(ctx)
    ed.harvest(data)
    _own_stem(ed, ctx)
    new, k = ed.edit(data)
    if new == data:
        return Result(None, status="unchanged", notes=["jt: text placeholder"] + ed.notes[:3])
    return Result(new, changed=k, status="ok", notes=["jt: text placeholder edited"] + ed.notes[:3])


def _own_stem(ed, ctx):
    """Names the path plan changed (source folder, its owner and repo parts, deeper folders, the file's own stem)
    follow inside the content as whole tokens, same length (D2: a name on disk and inside a file agree)."""
    a = ctx.old_path.split("!/")[-1].split("/")
    b = (ctx.new_path or ctx.old_path).split("!/")[-1].split("/")
    if len(a) != len(b):
        return
    a[-1], b[-1] = os.path.splitext(a[-1])[0], os.path.splitext(b[-1])[0]
    for x, y in zip(a, b):
        if x != y and len(x) == len(y):
            ed.add_stem(x, y)
            for xp, yp in zip(x.split("__"), y.split("__")):
                if xp != yp and len(xp) == len(yp):
                    ed.add_stem(xp, yp)


_U16RUN = re.compile(rb"(?:[\x20-\x7e\xa0-\xff]\x00)+")


def mb_slots(p):
    """JT MbString slots in a decoded payload: i32 character count followed by that many UTF-16LE characters."""
    out = []
    for m in _U16RUN.finditer(p):
        a = m.start()
        if a >= 4:
            n = struct.unpack_from("<i", p, a - 4)[0]
            if 1 <= n <= (m.end() - a) // 2:
                out.append((a, n))
    return out


def slot_pass(ed, orig, cur, mapping, source):
    """Edit every MbString as a whole field (parsed string slot, so D9's 8-character mask does not apply), starting
    from the ORIGINAL text so each occurrence is replaced once; the result overwrites what the masked pass did."""
    sl = [(a, n, orig[a:a + 2 * n].decode("utf-16-le")) for a, n in mb_slots(orig)]
    if not sl:
        return cur, 0
    joined = "\x00".join(t for _, _, t in sl)
    c0 = ed.count
    j2 = ed.edit_free(ed.edit_fields(joined))
    try:
        j3, k = mapping.sub_text(j2, source)
        if len(j3) == len(j2):
            j2, ed.count = j3, ed.count + k
    except Exception:
        pass
    if j2 == joined or len(j2) != len(joined):
        return cur, 0
    out, q = bytearray(cur), 0
    for a, n, t in sl:
        t2 = j2[q:q + len(t)]
        q += len(t) + 1
        nb = t2.encode("utf-16-le", "replace")
        if len(nb) == 2 * n:
            out[a:a + 2 * n] = nb
    return bytes(out), ed.count - c0


def _resize(raw, segs, toc, newseg):
    """Lay out segments again in file order (only when they are contiguous to the end of the file)."""
    order = sorted(segs, key=lambda s: s[2])
    if any(a[2] + a[3] != b[2] for a, b in zip(order, order[1:])) or order[-1][2] + order[-1][3] != len(raw):
        raise ValueError("segments not contiguous; resize refused")
    first = order[0][2]
    if toc >= first:
        raise ValueError("TOC after the segments; resize refused")
    out = bytearray(raw[:first])
    for s in order:
        i, q, off, ln = s[:4]
        seg = bytearray(raw[off:off + ln])
        if i in newseg:
            x = newseg[i]
            seg = seg[:33] + x
            struct.pack_into("<i", seg, 20, len(seg))
            struct.pack_into("<i", seg, 28, len(x) + 1)
        no = len(out)
        out += seg
        struct.pack_into("<Q", out, q + 16, no)
        struct.pack_into("<i", out, q + 24, len(seg))
    return bytes(out)


def process(data, ctx):
    data = bytes(data)
    if not data.startswith(b"Version "):
        if data[:1] in (b"#", b"!") and all(c in (9, 10, 13) or 32 <= c for c in data[:4096]):
            return _text(data, ctx)
        return Result(None, status="unchanged", notes=["jt: not a JT 10 file"])
    segs, toc = parse_jt(data)
    pay = {}
    for s in segs:
        if s[5] is not None:
            pay[s[0]] = lzma.decompress(data[s[5]:s[5] + s[6]], format=lzma.FORMAT_XZ)
    ed = Editor(ctx)
    for p in pay.values():
        ed.harvest(p)
    _own_stem(ed, ctx)
    newpay, k = {}, 0
    for i, p in pay.items():
        q, c = ed.edit(p)
        q, c2 = slot_pass(ed, p, q, ctx.mapping, ctx.source)
        c += c2
        if q != p:
            if len(q) != len(p):
                return Result(None, status="failed", notes=["jt: edit changed a payload length"])
            newpay[i], k = q, k + c
    if not newpay:
        return Result(None, status="unchanged", notes=ed.notes[:3])
    by = {s[0]: s for s in segs}
    fitted, misfit = {}, []
    for i, q in newpay.items():
        s = by[i]
        x = fit_xz(data[s[5]:s[5] + s[6]], q)
        if x is None:
            misfit.append(i)
            x = lzma.compress(q, format=lzma.FORMAT_XZ, check=CHECK[data[s[5] + 7] & 15][0],
                              filters=[{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME,
                                        "dict_size": _dict_size(parse_xz(data[s[5]:s[5] + s[6]])[1][0][0])}])
        fitted[i] = x
    notes = ["jt: %d segments edited (types %s)" % (len(newpay), sorted({by[i][4] for i in newpay}))]
    if misfit:
        out = _resize(data, segs, toc, fitted)
        notes.append("jt: resized %d segments (no exact-size fit): TOC and lengths rewritten" % len(misfit))
    else:
        out = bytearray(data)
        for i, x in fitted.items():
            s = by[i]
            out[s[5]:s[5] + s[6]] = x
        out = bytes(out)
    # ---- gate
    segs2, _ = parse_jt(out)
    if len(segs2) != len(segs):
        raise ValueError("gate: segment count")
    for s, s2 in zip(segs, segs2):
        if s[4] != s2[4] or (s[5] is None) != (s2[5] is None):
            raise ValueError("gate: segment %d kind" % s[0])
        if s[5] is None:
            if out[s2[2]:s2[2] + s2[3]] != data[s[2]:s[2] + s[3]]:
                raise ValueError("gate: untouched segment %d differs" % s[0])
            continue
        x = out[s2[5]:s2[5] + s2[6]]
        parse_xz(x)
        if lzma.decompress(x, format=lzma.FORMAT_XZ) != newpay.get(s[0], pay[s[0]]):
            raise ValueError("gate: segment %d decodes to other bytes" % s[0])
        if s[0] not in newpay and x != data[s[5]:s[5] + s[6]]:
            raise ValueError("gate: unedited segment %d differs" % s[0])
        if out[s2[2]:s2[5]] != data[s[2]:s[5]] and not misfit:
            raise ValueError("gate: segment %d header differs" % s[0])
    if not misfit:
        if len(out) != len(data):
            raise ValueError("gate: length")
        spans = sorted((by[i][5], by[i][5] + by[i][6]) for i in fitted)
        p = 0
        for a, b in spans:
            if out[p:a] != data[p:a]:
                raise ValueError("gate: bytes outside edited XZ spans differ")
            p = b
        if out[p:] != data[p:]:
            raise ValueError("gate: tail differs")
    return Result(out, changed=k, status="ok", notes=notes + ed.notes[:3])


def _is_jt(path, head):
    return path.endswith(".jt") and (head[:8] == b"Version " or head[:1] in (b"#", b"!"))


@register("jt", _is_jt, priority=60)
def handle(data, ctx):
    try:
        return process(data, ctx)
    except Exception as e:
        return Result(None, status="failed", notes=["jt: %s: %s" % (type(e).__name__, str(e)[:120])])


transform = make_transform(handle)
