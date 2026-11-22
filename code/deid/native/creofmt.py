"""Creo / Pro-E container: parse, Unix-compress LZW codec, checksums, rebuild. Owner: NATIVE builder.

parse/body/unlzw/lzw/sums are taken from the team's prior/native/creo.py (same project, no outside code).
rebuild() is new: sections are keyed by file offset (names can repeat across the two TOCs), and every position in a
gap is shifted by the size change of all sections that END before it (the prior version used section starts).

Layout: line 1 '#UGC:2 <TYPE> ... <base hex> \\', header '#- KEY value \\' lines, optional '#P_OBJECT' text, banner at
<base>, '#UGC_TOC 2 <n> 81 17###' + n 81-byte entries
  name off(hex, rel. to base) stored(hex, incl. '#name\\n') unpacked(hex) ver type flag ck1 ck2
ck1 = sum of payload bytes mod 65536, ck2 = sum of running sums mod 65536. NEXT_TOC_ENTRY chains a second TOC.
"""
import re


def parse(d):
    first = d[:d.find(b"\n")].decode("latin1").split()
    base = int(first[-2], 16)
    info = dict(type=first[1], base=base, secs=[], tocs=[])
    pos = d.find(b"#UGC_TOC", base)
    seen = set()
    while pos >= 0 and pos not in seen:
        seen.add(pos)
        line_end = d.find(b"\n", pos)
        h = d[pos:line_end].rstrip(b"#").split()
        n = int(h[2])
        info["tocs"].append((pos, n))
        nxt = -1
        p = line_end + 1
        for _ in range(n):
            f = d[p:p + 81].rstrip(b"#\n").split()
            p += 81
            if len(f) < 9:
                continue
            name = f[0].decode("latin1")
            off, stored, unp = int(f[1], 16), int(f[2], 16), int(f[3], 16)
            rec = dict(name=name, off=base + off, stored=stored, unp=unp, typ=f[5].decode(), flag=f[6].decode(),
                       ck1=int(f[7], 16), ck2=int(f[8], 16), toc_entry=p - 81)
            if name == "NEXT_TOC_ENTRY":
                nxt = base + off
            else:
                info["secs"].append(rec)
        pos = nxt if nxt > 0 and d[nxt:nxt + 8] == b"#UGC_TOC" else -1
    return info


def body(d, s):
    raw = d[s["off"]:s["off"] + s["stored"]]
    return raw[raw.find(b"\n") + 1:]


def sums(b):
    s1 = s2 = 0
    for x in b:
        s1 = (s1 + x) & 0xFFFF
        s2 = (s2 + s1) & 0xFFFF
    return s1, s2


def unlzw(buf):
    if buf[:2] != b"\x1f\x9d":
        raise ValueError("not LZW")
    flags = buf[2]
    maxbits, block = flags & 0x1F, bool(flags & 0x80)
    maxmax = 1 << maxbits
    data = buf[3:] + b"\0\0\0\0"
    total = (len(buf) - 3) * 8
    n_bits, maxcode = 9, 511
    free = 257 if block else 256
    tab = [bytes([i]) for i in range(256)] + [b""] * (maxmax - 256 + 1)
    out = []
    pos = base = 0
    old = -1
    while True:
        if free > maxcode:
            grp = n_bits * 8
            pos = base + -(-(pos - base) // grp) * grp
            n_bits += 1
            maxcode = maxmax if n_bits == maxbits else (1 << n_bits) - 1
            base = pos
        if pos + n_bits > total:
            break
        byte = pos >> 3
        code = (int.from_bytes(data[byte:byte + 4], "little") >> (pos & 7)) & ((1 << n_bits) - 1)
        pos += n_bits
        if old == -1:
            if code > 255:
                raise ValueError("first code above 255")
            old = code
            out.append(tab[code])
            continue
        if block and code == 256:
            grp = n_bits * 8
            pos = base + -(-(pos - base) // grp) * grp
            base = pos
            n_bits, maxcode, free = 9, 511, 256
            continue
        if code < free:
            st = tab[code]
        elif code == free:
            st = tab[old] + tab[old][:1]
        else:
            raise ValueError("corrupt LZW code %d free %d" % (code, free))
        out.append(st)
        if free < maxmax:
            tab[free] = tab[old] + st[:1]
            free += 1
        old = code
    return b"".join(out), (pos + 7) // 8 + 3


def lzw(data, maxbits=16):
    """Encoder mirroring unlzw's width schedule (no block mode)."""
    maxmax = 1 << maxbits
    body_ = bytearray()
    st = dict(n_bits=9, maxcode=511, pos=0, base=0, k=0, acc=0, nacc=0)

    def emit(code):
        dec_free = min(256 + max(0, st["k"] - 1), maxmax)
        if dec_free > st["maxcode"]:
            grp = st["n_bits"] * 8
            newpos = st["base"] + -(-(st["pos"] - st["base"]) // grp) * grp
            st["nacc"] += newpos - st["pos"]
            st["pos"] = newpos
            st["n_bits"] += 1
            st["maxcode"] = maxmax if st["n_bits"] == maxbits else (1 << st["n_bits"]) - 1
            st["base"] = st["pos"]
        st["acc"] |= code << st["nacc"]
        st["nacc"] += st["n_bits"]
        st["pos"] += st["n_bits"]
        st["k"] += 1
        if st["nacc"] >= 8 * 4096:
            body_.extend((st["acc"] & ((1 << (8 * 4096)) - 1)).to_bytes(4096, "little"))
            st["acc"] >>= 8 * 4096
            st["nacc"] -= 8 * 4096

    table, nxt = {}, 256
    if data:
        w = data[0]
        for c in data[1:]:
            key = (w << 8) | c
            v = table.get(key)
            if v is not None:
                w = v
            else:
                emit(w)
                if nxt < maxmax:
                    table[key] = nxt
                    nxt += 1
                w = c
        emit(w)
    body_.extend(st["acc"].to_bytes((st["nacc"] + 7) // 8, "little"))
    return b"\x1f\x9d" + bytes([maxbits]) + bytes(body_)


def rebuild(d, edits):
    """edits: {section file offset: (new payload, is_lzw)}. Recompresses LZW payloads, recomputes both checksums,
    rewrites sizes and offsets in every 81-byte TOC entry, including NEXT_TOC_ENTRY. The text before the banner must
    keep its length (the caller edits it same-length), so the base field in line 1 stays valid."""
    info = parse(d)
    base = info["base"]
    secs = sorted(info["secs"], key=lambda s: s["off"])
    out = bytearray(d[:base])
    cur = base
    spans = []          # (old start, old end, new start, new end)
    newent = {}
    for s in secs:
        if s["off"] < cur:
            raise ValueError("overlapping sections")
        out += d[cur:s["off"]]
        ns = len(out)
        raw = d[s["off"]:s["off"] + s["stored"]]
        if s["off"] in edits:
            line = raw[:raw.find(b"\n") + 1]
            payload, lz = edits[s["off"]]
            b = lzw(payload) if lz else payload
            raw = line + b
            c1, c2 = sums(b)
            newent[s["toc_entry"]] = (len(raw), len(payload), c1, c2)
        out += raw
        spans.append((s["off"], s["off"] + s["stored"], ns, len(out)))
        cur = s["off"] + s["stored"]
    out += d[cur:]
    starts = {o: n for o, _, n, _ in spans}

    def shift(p):
        delta = 0
        for os_, oe, ns, ne in spans:
            if oe <= p:
                delta = ne - oe
            elif os_ <= p:
                return p + (ns - os_)
            else:
                break
        return p + delta

    for tocpos, n in info["tocs"]:
        p = d.find(b"\n", tocpos) + 1
        for _ in range(n):
            f = d[p:p + 81].rstrip(b"#\n").split()
            if len(f) >= 9:
                oldabs = base + int(f[1], 16)
                newabs = shift(oldabs) if f[0] == b"NEXT_TOC_ENTRY" else starts[oldabs]
                f[1] = b"%x" % (newabs - base)
                if p in newent:
                    st, un, c1, c2 = newent[p]
                    f[2], f[3], f[7], f[8] = b"%x" % st, b"%x" % un, b"%04x" % c1, b"%04x" % c2
                line = b" ".join(f)
                if len(line) > 80:
                    raise ValueError("TOC entry overflow")
                q = shift(p)
                out[q:q + 81] = line + b"#" * (80 - len(line)) + b"\n"
            p += 81
    return bytes(out)


def check(d):
    """(sections, checksum ok, lzw decodable, lzw total)."""
    info = parse(d)
    n = ok = lz = lzok = 0
    for s in info["secs"]:
        b = body(d, s)
        n += 1
        ok += sums(b) == (s["ck1"], s["ck2"])
        if b[:2] == b"\x1f\x9d":
            lz += 1
            try:
                lzok += len(unlzw(b)[0]) == s["unp"]
            except Exception:
                pass
    return n, ok, lzok, lz
