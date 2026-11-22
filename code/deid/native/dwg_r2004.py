"""DWG R2004 file layout (AC1018, AC1024, AC1027, AC1032): same-length in-place edit. Owner: NATIVE builder. MIT.

Written from the ODA "Open Design Specification for .dwg files" (section 4) and the team's notes in
work/gaps/dwg_inplace.py. No LibreDWG code is used or copied.

Model: data pages are found by their masked tag at 0x20-aligned offsets (page map and section map are never changed).
Each page is decompressed on its own (byte-oriented LZ77), recording for every output byte the file offset of the
literal it came from ("provenance"). An edit of the decompressed section is written back by overwriting literals.
When a back-reference reuses a patched literal (collateral) or two output bytes want different values from one
literal (conflict), the page is re-encoded with our own LZ77 encoder to EXACTLY the original compressed size, so no
size, offset, page map or section map entry moves. Then object CRC-16s, header/classes CRC-16s and both page checksums
are recomputed.
"""
import struct, zlib
from array import array
import numpy as np

TAG, MASK = 0x4163043B, 0x4164536B
VERS = (b"AC1018", b"AC1024", b"AC1027", b"AC1032")
SENTINELS = (bytes.fromhex("CF7B1F23FDDE38A95F7C68B84E6D335F"),   # AcDb:Header
             bytes.fromhex("8DA1C4B8C4A9F8C5C0DCF45FE7CFB68A"))   # AcDb:Classes


class Bad(Exception):
    pass


_T = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0xA001 if _c & 1 else _c >> 1
    _T.append(_c)


def crc16(data, seed=0xC0C1):
    c = seed
    T = _T
    for b in data:
        c = (c >> 8) ^ T[(c ^ b) & 0xFF]
    return c


def scan_pages(d):
    pages = []
    n = len(d)
    for a in range(0x100, n - 32, 0x20):
        m = MASK ^ a
        if struct.unpack_from("<I", d, a)[0] ^ m != TAG:
            continue
        h = [x ^ m for x in struct.unpack_from("<8I", d, a)]
        if a + 32 + h[2] > n:
            continue
        pages.append(dict(off=a, sec=h[1], csize=h[2], psize=h[3], start=h[4], unk=h[5], hck=h[6], dck=h[7]))
    return pages


def checks(d, p, csize=None):
    """(header checksum, data checksum): word 7 = Adler-32(seed 0) of the compressed bytes, word 6 = Adler-32(seed =
    word 7) of the unmasked header with word 6 zeroed (fixed empirically on 8,965 pages)."""
    cs = p["csize"] if csize is None else csize
    dck = zlib.adler32(bytes(d[p["off"] + 32:p["off"] + 32 + cs]), 0)
    return zlib.adler32(struct.pack("<8I", TAG, p["sec"], cs, p["psize"], p["start"], p["unk"], 0, dck), dck), dck


def write_checks(d, p):
    hck, dck = checks(d, p)
    m = MASK ^ p["off"]
    struct.pack_into("<2I", d, p["off"] + 0x18, hck ^ m, dck ^ m)


# ---------------------------------------------------------------- LZ77 (decoder with provenance, encoder)
def decompress(d, p, want_prov=True):
    """-> (decompressed bytes, provenance array or None, position after the terminator)"""
    s = p["off"] + 32
    end = s + p["csize"]
    out = bytearray()
    prov = array("i") if want_prov else None

    def rd():
        nonlocal s
        if s >= end:
            raise Bad("overrun")
        b = d[s]
        s += 1
        return b

    def litlen():
        b = rd()
        if b == 0:
            t = 0x0F
            b = rd()
            while b == 0:
                t += 0xFF
                b = rd()
            return t + b + 3, 0
        if b & 0xF0:
            return 0, b
        return b + 3, 0

    def longoff():
        t = 0
        b = rd()
        if b == 0:
            t = 0xFF
            b = rd()
            while b == 0:
                t += 0xFF
                b = rd()
        return t + b

    def two():
        a = rd()
        b = rd()
        return (a >> 2) | (b << 6), a & 3

    def lits(n):
        nonlocal s
        if s + n > end:
            raise Bad("literal overrun")
        out.extend(d[s:s + n])
        if want_prov:
            prov.extend(range(s, s + n))
        s += n

    n, op = litlen()
    lits(n)
    while True:
        if op == 0:
            op = rd()
        if op >= 0x40:
            cb = ((op & 0xF0) >> 4) - 1
            co = (rd() << 2) | ((op & 0x0C) >> 2)
            ll = op & 3
        elif op == 0x11:
            break
        elif 0x21 <= op <= 0x3F:
            cb = op - 0x1E
            co, ll = two()
        elif op == 0x20:
            cb = longoff() + 0x21
            co, ll = two()
        elif 0x12 <= op <= 0x1F:
            cb = (op & 0x0F) + 2
            co, ll = two()
            co += 0x3FFF
        elif op == 0x10:
            cb = longoff() + 9
            co, ll = two()
            co += 0x3FFF
        else:
            raise Bad("opcode %02x" % op)
        if ll:
            op = 0
        else:
            ll, op = litlen()
        src = len(out) - co - 1
        if src < 0:
            raise Bad("offset")
        if co + 1 >= cb:
            out += out[src:src + cb]
            if want_prov:
                prov.extend(prov[src:src + cb])
        else:
            for i in range(cb):
                out.append(out[src + i])
                if want_prov:
                    prov.append(prov[src + i])
        lits(ll)
        if len(out) > 0x400000:
            raise Bad("too long")
    return bytes(out), prov, s


def _litlen_bytes(n):
    if n <= 18:
        return bytes([n - 3])
    r = n - 18
    o = bytearray([0])
    while r > 0xFF:
        o.append(0)
        r -= 0xFF
    o.append(r)
    return bytes(o)


def _copy_bytes(cb, dist, ll):
    co = dist - 1
    if 3 <= cb <= 14 and co <= 0x3FF:
        return bytes([((cb + 1) << 4) | ((co & 3) << 2) | ll, co >> 2])
    t = bytes([((co & 0x3F) << 2) | ll, co >> 6])
    if cb <= 33:
        return bytes([cb + 0x1E]) + t
    v = cb - 0x21
    z = (v - 1) // 0xFF
    return b"\x20" + b"\x00" * z + bytes([v - 0xFF * z]) + t


def _parse(data, K=4, depth=16):
    """Greedy LZ77 parse: [(position, length, distance)]. The first K bytes are literals."""
    n = len(data)
    K = max(4, min(K, n))
    head = {}
    prev = array("i", [-1]) * n if n else array("i")
    copies = []
    i = 0
    while i < K:
        if i + 3 <= n:
            h = data[i:i + 3]
            prev[i] = head.get(h, -1)
            head[h] = i
        i += 1
    while i < n:
        best = bd = 0
        if i + 3 <= n:
            key = data[i:i + 3]
            c = head.get(key, -1)
            dleft = depth
            mx = n - i
            while c >= 0 and dleft:
                dist = i - c
                if dist > 0x4000:
                    break
                L = 3
                if best >= 3 and (best >= mx or data[c + best] != data[i + best]):
                    pass
                else:
                    step = 32
                    while L < mx:
                        e = min(mx, L + step)
                        if data[c + L:c + e] == data[i + L:i + e]:
                            L = e
                            step *= 2
                        else:
                            if step == 1:
                                break
                            step = 1 if step <= 4 else step // 4
                    if L > best:
                        best, bd = L, dist
                c = prev[c]
                dleft -= 1
            prev[i] = head.get(key, -1)
            head[key] = i
        if best >= 4 or (best == 3 and bd <= 0x400):
            copies.append((i, best, bd))
            for j in range(i + 1, min(i + best, n - 2)):
                h = data[j:j + 3]
                prev[j] = head.get(h, -1)
                head[h] = j
            i += best
        else:
            i += 1
    return copies


def _emit(data, copies, offs=None):
    n = len(data)
    o = bytearray()
    first = copies[0][0] if copies else n
    o += _litlen_bytes(first) if first >= 4 else b""
    o += data[:first]
    for k, (pos, cb, dist) in enumerate(copies):
        if offs is not None:
            offs.append(len(o))
        nxt = copies[k + 1][0] if k + 1 < len(copies) else n
        r = nxt - pos - cb
        o += _copy_bytes(cb, dist, r if 1 <= r <= 3 else 0)
        if r >= 4:
            o += _litlen_bytes(r)
        o += data[pos + cb:nxt]
    o += b"\x11"
    return bytes(o)


def compress(data, K=4, depth=16):
    return _emit(data, _parse(data, K, depth))


def _dec_stream(stream):
    p = dict(off=0, csize=len(stream))
    out, _, e = decompress(b"\x00" * 32 + stream, p, want_prov=False)
    return out, e - 32


def fit(data, target):
    """Encoded stream of data with length EXACTLY target, verified by decoding, or None. One greedy parse; the size is
    raised by cutting the parse at copy k (shortened to c2 bytes) and sending the rest as one literal run, whose size
    is known arithmetically. No clock: cost is one parse plus a scan of the copy list."""
    n = len(data)
    if n < 4:
        return None
    copies = _parse(data)
    offs = []
    st = _emit(data, copies, offs)
    if len(st) > target:
        return None
    cand = copies if len(st) == target else None
    k = len(copies) - 1
    while cand is None and k >= 0:
        pos, cb, dist = copies[k]
        first = None
        for c2 in range(cb, 2, -1):
            R = n - pos - c2
            size = offs[k] + len(_copy_bytes(c2, dist, 0)) + (len(_litlen_bytes(R)) if R >= 4 else 0) + R + 1
            if first is None:
                first = size
            if size == target:
                cand = copies[:k] + [(pos, c2, dist)]
                break
        if cand is None and first is not None and first > target + 8:
            break
        k -= 1
    if cand is None:
        return None
    st = _emit(data, cand)
    out, used = _dec_stream(st)
    return st if out == data and used == target == len(st) else None


# ---------------------------------------------------------------- sections
def load_sections(d, want_prov=True):
    """{section number: dict(pages=[(page, lo, hi)], buf=bytearray, prov=array)}. A page counts as compressed only when
    the decoder stops at the 0x11 two bytes before its compressed size and those bytes are 00 00; otherwise raw (1:1)."""
    S = {}
    for p in sorted(scan_pages(d), key=lambda p: (p["sec"], p["start"])):
        base = p["off"] + 32
        comp = False
        try:
            out, prov, e = decompress(d, p, want_prov)
            # every compressed page of the corpus ends with the LZO1X end marker 11 00 00 (12,561 of 12,561)
            comp = e == base + p["csize"] - 2 and d[e:e + 2] == b"\x00\x00"
        except (Bad, IndexError):
            pass
        if not comp:
            out = bytes(d[base:base + p["csize"]])
            prov = array("i", range(base, base + p["csize"])) if want_prov else None
        p["comp"] = comp
        p["tail"] = b"\x00\x00"
        s = S.setdefault(p["sec"], dict(pages=[], buf=bytearray(), prov=array("i"), gaps=0))
        s["gaps"] += p["start"] != len(s["buf"])
        s["pages"].append((p, len(s["buf"]), len(s["buf"]) + len(out)))
        s["buf"] += out
        if want_prov:
            s["prov"].extend(prov)
    return S


def _ms(b, p):
    v = sh = n = 0
    while True:
        w = b[p + n] | (b[p + n + 1] << 8)
        n += 2
        v |= (w & 0x7FFF) << sh
        sh += 15
        if not w & 0x8000:
            return v, n


def _umc(b, p):
    v = sh = n = 0
    while True:
        c = b[p + n]
        n += 1
        v |= (c & 0x7F) << sh
        sh += 7
        if not c & 0x80:
            return v, n


def walk_objects(buf, ver):
    """[(start, crc position)] of the AcDbObjects section; stops at the first bad CRC."""
    objs = []
    p = 4 if buf[:4] == b"\xca\x0d\x00\x00" else 0
    bad = 0
    while p + 6 < len(buf):
        try:
            size, n = _ms(buf, p)
            if size == 0:
                break
            e = p + n + size
            if ver != b"AC1018":
                _, n2 = _umc(buf, p + n)
                e += n2
            if e + 2 > len(buf):
                break
            if crc16(buf[p:e]) != (buf[e] | (buf[e + 1] << 8)):
                bad += 1
                break
        except IndexError:
            break
        objs.append((p, e))
        p = e + 2
    return objs, p, bad


def objects_section(S):
    for k, s in S.items():
        if s["buf"][:4] == b"\xca\x0d\x00\x00":
            return k


def sentinel_crc_pos(buf):
    """CRC-16 position of a sentinel section (header, classes), chosen by matching the stored CRC, or None."""
    if bytes(buf[:16]) not in SENTINELS or len(buf) < 24:
        return None
    size = struct.unpack_from("<I", buf, 16)[0]
    for pos in (20 + size, 24 + size):
        if pos + 2 <= len(buf) and crc16(buf[16:pos]) == buf[pos] | (buf[pos + 1] << 8):
            return pos
    return None


def failures(d, S=None):
    """Set of (kind, place) audit failures: bad page checksum (page offset), object walk broken by a bad CRC (where,
    after how many objects), sentinel section whose CRC does not match (section number)."""
    S = S or load_sections(d, want_prov=False)
    f = {("page", p["off"]) for p in scan_pages(d) if checks(d, p) != (p["hck"], p["dck"])}
    k = objects_section(S)
    if k is not None:
        objs, endp, bad = walk_objects(S[k]["buf"], bytes(d[:6]))
        if bad:
            f.add(("objwalk", endp, len(objs)))
    for kk, s in S.items():
        if bytes(s["buf"][:16]) in SENTINELS and sentinel_crc_pos(s["buf"]) is None:
            f.add(("sent", kk))
    return f


def audit(d, S=None):
    """Independent re-check of a whole file: page checksums, object CRCs, sentinel CRCs."""
    P = scan_pages(d)
    S = S or load_sections(d, want_prov=False)
    k = objects_section(S)
    objs, endp, bad = walk_objects(S[k]["buf"], bytes(d[:6])) if k is not None else ([], 0, -1)
    sent = [sentinel_crc_pos(s["buf"]) is not None for s in S.values() if bytes(s["buf"][:16]) in SENTINELS]
    return dict(pages=len(P), pages_ok=sum(checks(d, p) == (p["hck"], p["dck"]) for p in P), objects=len(objs),
                obj_bad=bad, sent=len(sent), sent_ok=sum(sent))
