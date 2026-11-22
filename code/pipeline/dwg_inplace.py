"""DWG, R2004 file layout (AC1018, AC1024, AC1027, AC1032): same-length string patch WITHOUT a compressor or a DWG writer.
Written from the ODA "Open Design Specification for .dwg files" section 4; field roles were fixed empirically (see checks()).
"Provenance patch": the section compression is byte-oriented LZ77, so each decompressed byte comes from exactly one literal
byte in the file. Decompress while recording that file offset; to change an output byte overwrite its literal. Then fix the
per-object CRC-16 (also through provenance) and the two Adler-style checksums of each touched page. No size, offset or map moves.
Strings inside objects are bit-packed, so needles are searched at all 8 bit alignments."""
import struct, zlib
TAG, MASK = 0x4163043B, 0x4164536B
VERS = (b"AC1018", b"AC1024", b"AC1027", b"AC1032")
class Bad(Exception): pass
_T = []
for _i in range(256):
    _c = _i
    for _ in range(8): _c = (_c >> 1) ^ 0xA001 if _c & 1 else _c >> 1
    _T.append(_c)
def crc16(data, seed=0xC0C1):
    c = seed
    for b in data: c = (c >> 8) ^ _T[(c ^ b) & 0xFF]
    return c
def scan_pages(d):
    """data pages found by their masked tag at 0x20-aligned offsets (no page map needed for an in-place patch)"""
    pages = []
    for a in range(0x100, len(d) - 32, 0x20):
        m = MASK ^ a
        if struct.unpack_from("<I", d, a)[0] ^ m != TAG: continue
        h = [x ^ m for x in struct.unpack_from("<8I", d, a)]
        if a + 32 + h[2] > len(d): continue
        pages.append(dict(off=a, sec=h[1], csize=h[2], psize=h[3], start=h[4], unk=h[5], hck=h[6], dck=h[7]))
    return pages
def checks(d, p):
    """(header checksum, data checksum). Verified on real pages: field 7 = Adler-32(seed 0) of the csize data bytes,
    field 6 = Adler-32(seed = field 7) of the unmasked header with field 6 zeroed."""
    dck = zlib.adler32(bytes(d[p["off"] + 32:p["off"] + 32 + p["csize"]]), 0)
    return zlib.adler32(struct.pack("<8I", TAG, p["sec"], p["csize"], p["psize"], p["start"], p["unk"], 0, dck), dck), dck
def decompress(d, p):
    s = p["off"] + 32; end = s + p["csize"]; out = bytearray(); prov = []
    def rd():
        nonlocal s
        if s >= end: raise Bad("overrun")
        b = d[s]; s += 1; return b
    def litlen():
        b = rd()
        if b == 0:
            t = 0x0F; b = rd()
            while b == 0: t += 0xFF; b = rd()
            return t + b + 3, 0
        if b & 0xF0: return 0, b
        return b + 3, 0
    def longoff():
        t = 0; b = rd()
        if b == 0:
            t = 0xFF; b = rd()
            while b == 0: t += 0xFF; b = rd()
        return t + b
    def two():
        a = rd(); b = rd(); return (a >> 2) | (b << 6), a & 3
    def lits(n):
        nonlocal s
        if s + n > end: raise Bad("literal overrun")
        out.extend(d[s:s + n]); prov.extend(range(s, s + n)); s += n
    n, op = litlen(); lits(n)
    while True:
        if op == 0: op = rd()
        if op >= 0x40: cb = ((op & 0xF0) >> 4) - 1; co = (rd() << 2) | ((op & 0x0C) >> 2); ll = op & 3
        elif op == 0x11: break
        elif 0x21 <= op <= 0x3F: cb = op - 0x1E; co, ll = two()
        elif op == 0x20: cb = longoff() + 0x21; co, ll = two()
        elif 0x12 <= op <= 0x1F: cb = (op & 0x0F) + 2; co, ll = two(); co += 0x3FFF
        elif op == 0x10: cb = longoff() + 9; co, ll = two(); co += 0x3FFF
        else: raise Bad("opcode %02x" % op)
        if ll: op = 0
        else: ll, op = litlen()
        src = len(out) - co - 1
        if src < 0: raise Bad("offset")
        for i in range(cb): out.append(out[src + i]); prov.append(prov[src + i])
        lits(ll)
        if len(out) > 0x100000: raise Bad("too long")
    return bytes(out), prov
def load_sections(d):
    """{section number: dict(pages=[(page, lo, hi)], buf=bytearray, prov=[file offset per byte])}; raw pages map 1:1."""
    S = {}
    for p in sorted(scan_pages(d), key=lambda p: (p["sec"], p["start"])):
        try: out, prov = decompress(d, p)
        except (Bad, IndexError):
            base = p["off"] + 32; out = bytes(d[base:base + p["csize"]]); prov = list(range(base, base + p["csize"]))
        s = S.setdefault(p["sec"], dict(pages=[], buf=bytearray(), prov=[]))
        s["pages"].append((p, len(s["buf"]), len(s["buf"]) + len(out))); s["buf"] += out; s["prov"] += prov
    return S
def find_bits(buf, needle):
    """all (byte index, bit shift 0..7) where needle sits in an MSB-first bit stream"""
    res = []; L = len(needle)
    i = buf.find(needle)
    while i >= 0: res.append((i, 0)); i = buf.find(needle, i + 1)
    for s in range(1, 8):
        inner = bytes(((needle[k - 1] << (8 - s)) | (needle[k] >> s)) & 0xFF for k in range(1, L))
        i = buf.find(inner)
        while i >= 0:
            a = i - 1
            if a >= 0 and i + L - 1 < len(buf) and (buf[a] & (0xFF >> s)) == needle[0] >> s and (buf[i + L - 1] >> (8 - s)) == (needle[-1] & ((1 << s) - 1)):
                res.append((a, s))
            i = buf.find(inner, i + 1)
    return res
def put_bits(cur, i, s, new):
    if s == 0: cur[i:i + len(new)] = new; return
    L = len(new)
    cur[i] = (cur[i] & ~(0xFF >> s) & 0xFF) | (new[0] >> s)
    for k in range(1, L): cur[i + k] = ((new[k - 1] << (8 - s)) | (new[k] >> s)) & 0xFF
    cur[i + L] = (cur[i + L] & (0xFF >> s)) | ((new[-1] << (8 - s)) & 0xFF)
def _ms(b, p):
    v = sh = n = 0
    while True:
        w = b[p + n] | (b[p + n + 1] << 8); n += 2; v |= (w & 0x7FFF) << sh; sh += 15
        if not w & 0x8000: return v, n
def _umc(b, p):
    v = sh = n = 0
    while True:
        c = b[p + n]; n += 1; v |= (c & 0x7F) << sh; sh += 7
        if not c & 0x80: return v, n
def walk_objects(buf, ver):
    """objects of the AcDbObjects section: [(start, crc position)] ; every entry has a verified CRC-16 (seed 0xC0C1)."""
    objs = []; p = 4 if buf[:4] == b"\xca\x0d\x00\x00" else 0; bad = 0
    while p + 6 < len(buf):
        try:
            size, n = _ms(buf, p)
            if size == 0: break
            e = p + n + size
            if ver != b"AC1018":
                hs, n2 = _umc(buf, p + n); e += n2
            if e + 2 > len(buf): break
            if crc16(buf[p:e]) != (buf[e] | (buf[e + 1] << 8)): bad += 1; break
        except IndexError: break
        objs.append((p, e)); p = e + 2
    return objs, p, bad
def find_objects_section(S):
    for k, s in S.items():
        if s["buf"][:4] == b"\xca\x0d\x00\x00": return k
def patch(d, pairs):
    """pairs: [(old bytes, new bytes, same length)]. Returns (new file bytes, stats)."""
    ver = bytes(d[:6]); d = bytearray(d); S = load_sections(bytes(d)); ok = find_objects_section(S)
    st = dict(hits=0, shifted=0, in_objects=0, crc_fixed=0, conflict=0, collateral=0, pages=0, obj_walk_ok=None)
    for k, s in S.items():
        cur = bytearray(s["buf"])
        for old, new in pairs:
            for i, sh in find_bits(s["buf"], old):
                put_bits(cur, i, sh, new); st["hits"] += 1; st["shifted"] += sh != 0; st["in_objects"] += k == ok
        if cur == s["buf"]: continue
        if k == ok:
            objs, endp, bad = walk_objects(s["buf"], ver); st["obj_walk_ok"] = (len(objs), bad)
            ch = [i for i in range(len(cur)) if cur[i] != s["buf"][i]]; j = 0
            for a, e in objs:
                if j >= len(ch): break
                if ch[j] >= e + 2: continue
                hit = False
                while j < len(ch) and ch[j] < e + 2: hit = hit or ch[j] >= a; j += 1
                if hit: c = crc16(cur[a:e]); cur[e] = c & 0xFF; cur[e + 1] = c >> 8; st["crc_fixed"] += 1
        lit = {}
        for i in range(len(cur)):
            if cur[i] != s["buf"][i] and lit.setdefault(s["prov"][i], cur[i]) != cur[i]: st["conflict"] += 1
        for i, o in enumerate(s["prov"]):
            if o in lit and cur[i] != lit[o]: st["collateral"] += 1
        for o, b in lit.items(): d[o] = b
        for p, lo, hi in s["pages"]:
            if any(p["off"] + 32 <= o < p["off"] + 32 + p["csize"] for o in lit):
                hck, dck = checks(d, p); m = MASK ^ p["off"]; struct.pack_into("<2I", d, p["off"] + 0x18, hck ^ m, dck ^ m); st["pages"] += 1
    return bytes(d), st
def audit(d):
    """independent re-check of a file: page checksums and object CRCs"""
    P = scan_pages(d); S = load_sections(d); k = find_objects_section(S)
    objs, endp, bad = walk_objects(S[k]["buf"], bytes(d[:6])) if k is not None else ([], 0, -1)
    return dict(pages=len(P), pages_ok=sum(checks(d, p) == (p["hck"], p["dck"]) for p in P), objects=len(objs), obj_bad=bad,
                walked=endp, objsec_len=len(S[k]["buf"]) if k is not None else 0)
