"""Creo / Pro-E versioned file parser (verified layout on sample files):
line 1: '#UGC:2 <TYPE> a b c d e f <release> <datecode> <base hex> \\'  base = file offset of the '#Creo ...'/'#Pro/ENGINEER' banner line
then '#- KEY value \\' header lines, '#-END_OF_UGC_HEADER', optional text '#P_OBJECT ... #END_OF_P_OBJECT' (user names / host names),
banner, '#UGC_TOC 2 <n> 81 17###' followed by n 81-byte entries:
  name off(hex, relative to base) stored(hex, incl. '#name\\n' line) unpacked(hex) ver type flag ck1 ck2
flag -1 = stored plain, 1/2 = compressed. NEXT_TOC_ENTRY chains to a second TOC near EOF.
"""
import re, sys


def parse(d):
    first = d[:d.find(b"\n")].decode("latin1").split()
    base = int(first[-2], 16)
    info = dict(type=first[1], release=first[8], datecode=first[9], base=base, hdr={}, secs=[], tocs=[])
    for m in re.finditer(rb"#- (\w+) ([^\\\n]*?)\s*\\\n", d[:base]):
        info["hdr"][m.group(1).decode()] = m.group(2).decode("latin1")
    pos = d.find(b"#UGC_TOC", base)
    while pos >= 0:
        line_end = d.find(b"\n", pos)
        h = d[pos:line_end].rstrip(b"#").split()
        n = int(h[2])
        info["tocs"].append((pos, n))
        nxt = -1
        p = line_end + 1
        for i in range(n):
            ent = d[p:p + 81]
            f = ent.rstrip(b"#\n").split()
            p += 81
            if len(f) < 9:
                continue
            name = f[0].decode("latin1")
            off, stored, unp = int(f[1], 16), int(f[2], 16), int(f[3], 16)
            rec = dict(name=name, off=base + off, stored=stored, unp=unp, ver=f[4].decode(), typ=f[5].decode(),
                       flag=f[6].decode(), ck1=int(f[7], 16), ck2=int(f[8], 16), toc_entry=p - 81)
            if name == "NEXT_TOC_ENTRY":
                nxt = base + off
            else:
                info["secs"].append(rec)
        pos = nxt if nxt > 0 and d[nxt:nxt + 8] == b"#UGC_TOC" else -1
    return info


def body(d, s):
    """Section payload without its '#name\\n' line."""
    raw = d[s["off"]:s["off"] + s["stored"]]
    nl = raw.find(b"\n")
    return raw[nl + 1:]


if __name__ == "__main__":
    d = open(sys.argv[1], "rb").read()
    i = parse(d)
    print(i["type"], i["release"], i["hdr"], "tocs", i["tocs"])
    for s in i["secs"]:
        raw = d[s["off"]:s["off"] + s["stored"]]
        print("%-28s @%8d stored=%7d unp=%7d flag=%2s ck=%04x %04x | %r" % (
            s["name"], s["off"], s["stored"], s["unp"], s["flag"], s["ck1"], s["ck2"], raw[:40]))


# ---- Unix compress (LZW .Z) codec, magic 1f 9d, flags 0x10 = 16 bit max, no block mode (what Creo sections use) ----
def unlzw(buf):
    assert buf[:2] == b"\x1f\x9d", "not LZW"
    flags = buf[2]
    maxbits, block = flags & 0x1F, bool(flags & 0x80)
    maxmax = 1 << maxbits
    data = buf[3:]
    total = len(data) * 8
    n_bits, maxcode = 9, 511
    free = 257 if block else 256
    prefix, suffix = {}, {}
    out = bytearray()
    pos = base = 0
    old = -1
    fin = 0

    def expand(c):
        st = bytearray()
        while c >= 256:
            st.append(suffix[c])
            c = prefix[c]
        st.append(c)
        st.reverse()
        return st

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
            old = fin = code
            out.append(code)
            continue
        if block and code == 256:
            prefix.clear(); suffix.clear()
            grp = n_bits * 8
            pos = base + -(-(pos - base) // grp) * grp
            base = pos
            n_bits, maxcode, free = 9, 511, 256
            continue
        if code >= free:
            if code > free:
                raise ValueError("corrupt LZW at bit %d code %d free %d" % (pos, code, free))
            st = expand(old) + bytes([fin])
        else:
            st = expand(code)
        fin = st[0]
        out += st
        if free < maxmax:
            prefix[free] = old
            suffix[free] = fin
            free += 1
        old = code
    return bytes(out), (pos + 7) // 8 + 3


def lzw(data, maxbits=16):
    """Encoder mirroring unlzw's width schedule (no block mode)."""
    maxmax = 1 << maxbits
    out = bytearray(b"\x1f\x9d" + bytes([maxbits]))
    acc = 0
    nacc = 0
    body = bytearray()
    n_bits, maxcode = 9, 511
    pos = base = 0
    k = 0  # index of the code about to be written
    table = {}
    nxt = 256
    bits = 0  # big int bit buffer
    chunks = []

    def emit(code):
        nonlocal n_bits, maxcode, pos, base, k, acc, nacc
        dec_free = min(256 + max(0, k - 1), maxmax)
        if dec_free > maxcode:
            grp = n_bits * 8
            newpos = base + -(-(pos - base) // grp) * grp
            nacc += newpos - pos
            pos = newpos
            n_bits += 1
            maxcode = maxmax if n_bits == maxbits else (1 << n_bits) - 1
            base = pos
        acc |= code << nacc
        nacc += n_bits
        pos += n_bits
        k += 1
        while nacc >= 8 * 4096:
            body.extend((acc & ((1 << (8 * 4096)) - 1)).to_bytes(4096, "little"))
            acc >>= 8 * 4096
            nacc -= 8 * 4096

    if data:
        w = data[0]
        for c in data[1:]:
            key = (w, c)
            if key in table:
                w = table[key]
            else:
                emit(w)
                if nxt < maxmax:
                    table[key] = nxt
                    nxt += 1
                w = c
        emit(w)
    body.extend(acc.to_bytes((nacc + 7) // 8, "little"))
    return bytes(out + body)


def sums(b):
    s1 = s2 = 0
    for x in b:
        s1 = (s1 + x) & 0xFFFF
        s2 = (s2 + s1) & 0xFFFF
    return s1, s2


def verify(d):
    """Returns (n sections, n checksum ok, n lzw sections, n lzw ok, [failures])."""
    i = parse(d)
    n = ok = nl = lok = 0
    bad = []
    for s in i["secs"]:
        b = body(d, s)
        n += 1
        if sums(b) == (s["ck1"], s["ck2"]):
            ok += 1
        else:
            bad.append(("cksum", s["name"]))
        if b[:2] == b"\x1f\x9d":
            nl += 1
            try:
                u, used = unlzw(b)
                if len(u) == s["unp"]:
                    lok += 1
                else:
                    bad.append(("lzwlen", s["name"]))
            except Exception:
                bad.append(("lzwfail", s["name"]))
    return n, ok, nl, lok, bad


def rebuild(d, sec_edits=None, header_edits=None):
    """sec_edits: {section name: new unpacked payload}. header_edits: [(old bytes, new bytes)] applied to the text before the banner.
    Recompresses, recomputes both checksums, rewrites TOC entries (fixed 81-byte lines) and the base offset in line 1."""
    sec_edits = sec_edits or {}
    info = parse(d)
    base = info["base"]
    head = d[:base]
    for o, n in header_edits or []:
        head = head.replace(o, n)
    nl1 = head.find(b"\n")
    first = head[:nl1]
    m = re.search(rb"([0-9a-fA-F]+)( \\)$", first)       # V3: old releases write the base in 4 hex digits, not 8
    if m is None:
        raise ValueError("creo: base offset field not found in line 1")
    newbase = len(head)
    w = len(m.group(1))
    nb = b"%0*x" % (w, newbase)
    if len(nb) != w:
        raise ValueError("creo: new base does not fit the field width")
    head = head[:m.start(1)] + nb + head[m.end(1):]
    secs = sorted(info["secs"], key=lambda s: s["off"])
    out = bytearray(head)
    cur = base
    newpos = {}      # old absolute offset of anything -> new absolute offset
    newent = {}
    for s in secs:
        out += d[cur:s["off"]]
        newpos[s["off"]] = len(out)
        raw = d[s["off"]:s["off"] + s["stored"]]
        if s["name"] in sec_edits:
            line = raw[:raw.find(b"\n") + 1]
            payload = sec_edits[s["name"]]
            b = lzw(payload) if body(d, s)[:2] == b"\x1f\x9d" else payload
            raw = line + b
            c1, c2 = sums(b)
            newent[s["toc_entry"]] = (len(raw), len(payload), c1, c2)
        out += raw
        cur = s["off"] + s["stored"]
    out += d[cur:]
    # map TOC positions (they live in the gaps) by cumulative shift
    marks = sorted(newpos.items())

    def shift(oldabs):
        delta = newbase - base
        for o, n in marks:
            if o <= oldabs:
                delta = n - o
            else:
                break
        if oldabs >= cur:
            delta = len(out) - len(d)
        return oldabs + delta

    for tocpos, n in info["tocs"]:
        p = d.find(b"\n", tocpos) + 1
        for k in range(n):
            ent = d[p:p + 81]
            f = ent.rstrip(b"#\n").split()
            if len(f) < 9:
                p += 81
                continue
            name = f[0]
            oldabs = base + int(f[1], 16)
            if name == b"NEXT_TOC_ENTRY":
                newabs = shift(oldabs)
            else:
                newabs = newpos[oldabs]
            f[1] = b"%x" % (newabs - newbase)
            if p in newent:
                st, un, c1, c2 = newent[p]
                f[2], f[3], f[7], f[8] = b"%x" % st, b"%x" % un, b"%04x" % c1, b"%04x" % c2
            line = b" ".join(f)
            line = line + b"#" * (80 - len(line)) + b"\n"
            np_ = shift(p)
            out[np_:np_ + 81] = line
            p += 81
    return bytes(out)
