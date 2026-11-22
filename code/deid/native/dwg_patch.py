"""DWG same-length identity edit on decoded sections, using the shared Editor. Owner: NATIVE builder. MIT.

Strings inside DWG objects are bit-packed, so every decoded section is searched in its 8 bit-shifted byte views; each
view goes through Editor (latin-1 and UTF-16LE). An edit in a view is accepted only when it looks like text in a
string slot (guard below), because a 4-5 letter name can match random bits of geometry.
Guard: the Editor's own rule (field slots anywhere, free text only in runs of >= 8 readable chars), plus a DWG rule:
a run that starts right after its own length (byte or LE16, run length or + 1) is a parsed string slot.
"""
import struct
import numpy as np
from .textedit import views, readable
from . import dwg_r2004 as D

MAX_REENCODE = 48              # my number: about 25 s of pure-Python encoding per file
MAX_SHIFT_SECTION = 32_000_000   # my number: bit views need about 25x the section size in memory


def _textish(c):
    return c != "\x01" and (" " <= c <= "~" or c >= "\xa0")


def prefixed(text, i, j, B, a, stride):
    """True when the changed chars i..j lie in a run of text that starts right after its own length (a DWG T/TU string
    or a SummaryInfo string: byte, or LE16 = run length or run length + 1): a parsed string slot."""
    lo, hi = i, j
    while lo > 0 and _textish(text[lo - 1]):
        lo -= 1
    while hi < len(text) and _textish(text[hi]):
        hi += 1
    L = hi - lo
    bs = a + stride * lo
    if bs >= 2 and (B[bs - 2] | (B[bs - 1] << 8)) in (L, L + 1):
        return True
    return stride == 1 and bs >= 1 and B[bs - 1] in (L, L + 1)


FIELD_TRIGGERS = ("users", "documents and settings", "dokumente und einstellungen", "home", "\\\\", "sw-author",
                  "sw-last", "hostname", "rechnername", "nom d", "nome host")
CAD_TRIGGERS = (".sld", ".ipt", ".iam", ".idw", ".ipn", ".prt", ".asm", ".drw", ".par", ".psm", ".dft", ".dwg", ".jt",
                ".catp", ".catd")


class Triggers:
    """Cheap prefilter: a view goes through the Editor only if it contains a trigger string (lower case)."""
    def __init__(self, ed):
        t = set(FIELD_TRIGGERS) | set(CAD_TRIGGERS)
        for low, v in ed.vals.items():
            if len(low) >= 4:
                t.add(low)
                try:
                    t.add(v.encode("utf-8").decode("latin-1").lower())
                except Exception:
                    pass
        t |= {k.lower() for k in ed.short}
        t |= {k.lower() for k in getattr(ed, 'stems', {})}
        ids = getattr(ed.m, "ids", None)
        keys = getattr(ed.m, "values", None)
        self.full = False
        if isinstance(ids, dict):
            for k, e in ids.items():
                for f in (e.get("forms") or [k]) if isinstance(e, dict) else [k]:
                    if len(f) >= 3:
                        t.add(f.lower())
        elif isinstance(keys, dict):
            t |= {k.lower() for k in keys if len(k) >= 3}
        else:
            self.full = True                          # mapping keys unknown: no prefilter
        self.t = sorted(t)
        try:
            import ahocorasick
            self.A = ahocorasick.Automaton()
            for x in self.t:
                self.A.add_word(x, x)
            self.A.make_automaton()
        except Exception:
            self.A = None

    def hit(self, text):
        if self.full:
            return True
        low = text.lower()
        if self.A is not None:
            for _ in self.A.iter(low):
                return True
            return False
        return any(x in low for x in self.t)


def guarded_edit(ed, B, st):
    """Editor steps on each view of B. Field-slot changes are kept anywhere (Editor rule); free-text changes only in a
    run of >= 8 readable chars (Editor rule D9) or in a length-prefixed string slot (DWG rule, see prefixed())."""
    b = None
    trig = st.get("_trig")
    for text, a, stride in views(B):
        if trig is not None and not trig.hit(text):
            continue
        t1 = ed.edit_fields(text)
        t2 = ed.edit_free(t1)
        try:
            t3, _ = ed.m.sub_text(t2, ed.src)
            if len(t3) == len(t2):
                t2 = t3
        except Exception as e:
            st["sub_text_error"] = type(e).__name__
        if t2 == text:
            continue
        try:
            o = np.frombuffer(text.encode("latin-1"), np.uint8)
            n1 = np.frombuffer(t1.encode("latin-1"), np.uint8)
            n2 = np.frombuffer(t2.encode("latin-1"), np.uint8)
        except UnicodeEncodeError:
            st["nonlatin"] = st.get("nonlatin", 0) + 1
            continue
        final = n1.copy()
        fidx = np.nonzero(o != n1)[0]
        if len(fidx):
            st["accepted"] = st.get("accepted", 0) + len(np.split(fidx, np.nonzero(np.diff(fidx) > 2)[0] + 1))
        free = np.nonzero(n1 != n2)[0]
        if len(free):
            rd = readable(n1)
            for cl in np.split(free, np.nonzero(np.diff(free) > 2)[0] + 1):
                if rd[cl].all() or prefixed(text, int(cl[0]), int(cl[-1]) + 1, B, a, stride):
                    final[cl] = n2[cl]
                    st["accepted"] = st.get("accepted", 0) + 1
                else:
                    st["rejected"] = st.get("rejected", 0) + 1
        idx = np.nonzero(final != o)[0]
        if not len(idx):
            continue
        if b is None:
            b = bytearray(B)
        for x in idx.tolist():
            b[a + stride * x] = int(final[x])
    return bytes(b) if b is not None else B


ZIP, OLE, PNG, JPG = b"PK\x03\x04", bytes.fromhex("D0CF11E0A1B11AE1"), b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"


def _u32(V, i):
    return V[i] | (V[i + 1] << 8) | (V[i + 2] << 16) | (V[i + 3] << 24)


def _span(V, i, sig):
    """Length of an embedded container starting at V[i], or 0 if the signature is not one. Span rules are mine:
    zip to the end of its end-of-central-directory record, OLE to the FAT-sector bound, PNG to IEND, JPEG to EOI after
    the segment walk, BMP by its size field, DIB (s=0 only) by header arithmetic; unknown end -> end of the buffer."""
    n = len(V)
    try:
        if sig == ZIP:
            e = V.find(b"PK\x05\x06", i)
            return (min(n, e + 22 + (V[e + 20] | (V[e + 21] << 8))) if e >= 0 and e + 22 <= n else n) - i
        if sig == OLE:
            sh, nfat = V[i + 0x1E] | (V[i + 0x1F] << 8), _u32(V, i + 0x2C)
            if sh not in (9, 12) or nfat > 4096:
                return 0
            return min(n - i, 512 + nfat * ((1 << sh) // 4) * (1 << sh))
        if sig == PNG:
            e = V.find(b"IEND", i)
            return (e + 8 if e >= 0 else n) - i
        if sig == JPG:
            q = i + 2
            for _ in range(64):
                if V[q] != 0xFF:
                    return 0
                m = V[q + 1]
                if m == 0xDA:
                    e = V.find(b"\xff\xd9", q)
                    return (e + 2 if e >= 0 else n) - i
                q += 2 + ((V[q + 2] << 8) | V[q + 3])
            return 0
        if sig == b"BM":
            size, off = _u32(V, i + 2), _u32(V, i + 10)
            return size if _u32(V, i + 6) == 0 and 26 <= off < size <= n - i else 0
        if sig == b"\x28\x00\x00\x00":
            w, h = _u32(V, i + 4), _u32(V, i + 8)
            h = h - (1 << 32) if h >= 1 << 31 else h
            planes, bpp, comp = V[i + 12] | (V[i + 13] << 8), V[i + 14] | (V[i + 15] << 8), _u32(V, i + 16)
            if not (0 < w <= 20000 and 0 < abs(h) <= 20000 and planes == 1 and bpp in (1, 4, 8, 16, 24, 32) and comp <= 3):
                return 0
            size = 40 + ((4 << bpp) if bpp <= 8 else 0) + ((w * bpp + 31) // 32 * 4) * abs(h)
            return size if size <= n - i else 0
    except IndexError:
        return n - i
    return 0


def container_mask(views_list, n):
    """Boolean mask over buffer bytes that lie inside an embedded zip, OLE, PNG, JPEG, BMP or DIB (any bit shift)."""
    mask = np.zeros(n + 1, bool)
    found = 0
    for s, V in views_list:
        for sig in (ZIP, OLE, PNG, JPG, b"BM") + ((b"\x28\x00\x00\x00",) if s == 0 else ()):
            i = V.find(sig)
            while i >= 0:
                L = _span(V, i, sig)
                if L > 0:
                    mask[i:i + L + (1 if s else 0)] = True
                    found += 1
                i = V.find(sig, i + max(L, 1))
    return mask[:n], found


def shift_views(buf, full=True):
    """(shift, view): view s byte i = bits 8i+s .. 8i+s+7 of buf (MSB first)."""
    yield 0, buf
    if not full or len(buf) < 2:
        return
    a = np.frombuffer(buf, np.uint8).astype(np.uint16)
    hi, lo = a[:-1], a[1:]
    for s in range(1, 8):
        yield s, (((hi << s) | (lo >> (8 - s))) & 0xFF).astype(np.uint8).tobytes()


def harvest(ed, buf):
    for _, B in shift_views(buf, len(buf) <= MAX_SHIFT_SECTION):
        ed.harvest(B)


def edit_buffer(ed, buf, st):
    """Same-length edited copy of buf (bytes) over all bit alignments."""
    full = len(buf) <= MAX_SHIFT_SECTION
    if not full:
        st.setdefault("notes", []).append("section of %d MB: only byte-aligned strings checked" % (len(buf) >> 20))
    cur = None
    owner = {}
    vl = list(shift_views(buf, full))
    cmask, nfound = container_mask(vl, len(buf))
    if nfound:
        st["containers"] = st.get("containers", 0) + nfound
    for s, B in vl:
        NB = guarded_edit(ed, B, st)
        if NB is B or NB == B:
            continue
        idx = np.nonzero(np.frombuffer(B, np.uint8) != np.frombuffer(NB, np.uint8))[0]
        if nfound and len(idx):
            inside = cmask[idx] | (cmask[np.minimum(idx + 1, len(buf) - 1)] if s else False)
            if inside.any():
                st["in_container"] = st.get("in_container", 0) + int(inside.sum())
                idx = idx[~inside]
        idx = idx.tolist()
        if not idx:
            continue
        span = set(idx) | ({i + 1 for i in idx} if s else set())
        if any(owner.get(x, s) != s for x in span):
            st["shift_conflict"] = st.get("shift_conflict", 0) + 1
            continue
        if cur is None:
            cur = bytearray(buf)
        m = 0xFF >> s
        for i in idx:
            v = NB[i]
            if s == 0:
                cur[i] = v
            else:
                cur[i] = (cur[i] & ~m & 0xFF) | (v >> s)
                cur[i + 1] = (cur[i + 1] & m) | ((v << (8 - s)) & 0xFF)
        for x in span:
            owner[x] = s
    return bytes(cur) if cur is not None else buf


# ---------------------------------------------------------------- SummaryInfo (R2004 layout)
def summary_fields(buf, ver):
    """[(start, nbytes, value)] of the 8 header strings when buf parses as SummaryInfo, else None.
    Layout: 8 x (RS length incl. NUL, chars incl. NUL; UTF-16LE from R2007, 8-bit before), then 4 x RL times,
    then RS custom property count and that many (name, value) string pairs."""
    w = 1 if ver == b"AC1018" else 2
    p = 0
    out = []

    def string(p):
        n = struct.unpack_from("<H", buf, p)[0]
        e = p + 2 + n * w
        if n > 4096 or e > len(buf):
            raise ValueError
        raw = bytes(buf[p + 2:e])
        v = raw.decode("utf-16-le" if w == 2 else "latin-1")
        if n and v[-1] != "\x00":
            raise ValueError
        v = v[:-1] if n else v
        if any(c < " " for c in v):
            raise ValueError
        return (p + 2, len(v) * w, v), e
    try:
        for _ in range(8):
            f, p = string(p)
            out.append(f)
        p += 16 + 8
        cnt = struct.unpack_from("<H", buf, p)[0]
        p += 2
        if cnt > 2000:
            return None
        for _ in range(2 * cnt):
            f, p = string(p)
        if p > len(buf):
            return None
    except (ValueError, struct.error, UnicodeDecodeError):
        return None
    return out


# ---------------------------------------------------------------- R2004-layout patch
def patch_r2004(data, ed):
    """-> (new bytes or None, stats). Never emits a file that fails its own end-to-end check."""
    ver = bytes(data[:6])
    st = dict(notes=[])
    S = D.load_sections(data)
    ko = D.objects_section(S)
    bufs = {k: bytes(s["buf"]) for k, s in S.items()}
    for k, b in bufs.items():
        harvest(ed, b)
    summ = None
    for k, b in bufs.items():
        if k != ko and len(b) < 65536:
            f = summary_fields(b, ver)
            if f:
                summ = (k, f)
                for i in (2, 5):            # author, last saved by
                    ed.add(f[i][2])
                break
    st["summary"] = bool(summ)
    st["_trig"] = Triggers(ed)
    plan = {}
    for k, buf in bufs.items():
        cur = bytearray(edit_buffer(ed, buf, st))
        if summ and k == summ[0]:
            for i in (2, 5):
                p0, nb, v = summ[1][i]
                if not v.strip():
                    continue
                new = ed.sf(v)
                try:
                    enc = new.encode("utf-16-le" if ver != b"AC1018" else "latin-1")
                except UnicodeEncodeError:
                    st["notes"].append("summary field kept: stand-in not encodable")
                    continue
                if len(enc) == nb and bytes(cur[p0:p0 + nb]) != enc:
                    if bytes(cur[p0:p0 + nb]) == bytes(buf[p0:p0 + nb]):
                        st["accepted"] = st.get("accepted", 0) + 1
                    cur[p0:p0 + nb] = enc
        if bytes(cur) == buf:
            continue
        if k == ko:
            objs, endp, bad = D.walk_objects(buf, ver)
            ch = np.nonzero(np.frombuffer(buf, np.uint8) != np.frombuffer(bytes(cur), np.uint8))[0]
            starts = np.array([a for a, e in objs] + [endp], np.int64)
            ends = np.array([e + 2 for a, e in objs] + [endp], np.int64)
            j = np.searchsorted(starts, ch, side="right") - 1
            inside = (j >= 0) & (j < len(objs)) & (ch < ends[np.clip(j, 0, len(ends) - 1)])
            if (~inside).any():
                st["dropped_outside_objects"] = int((~inside).sum())
                for x in ch[~inside].tolist():
                    cur[x] = buf[x]
            for jj in sorted(set(j[inside].tolist())):
                a, e = objs[jj]
                c = D.crc16(cur[a:e])
                cur[e] = c & 0xFF
                cur[e + 1] = c >> 8
                st["crc_fixed"] = st.get("crc_fixed", 0) + 1
        elif buf[:16] in D.SENTINELS:
            pos = D.sentinel_crc_pos(buf)
            if pos is None:
                st["notes"].append("header/classes section: CRC not reproduced, strings there kept")
                continue
            c = D.crc16(cur[16:pos])
            cur[pos] = c & 0xFF
            cur[pos + 1] = c >> 8
        if bytes(cur) != buf:
            plan[k] = bytes(cur)
    if not plan:
        return None, st
    d = bytearray(data)
    for k, cur in plan.items():
        if not _write_section(d, data, S[k], bufs[k], cur, st):
            st["notes"].append("section %d: page could not be re-encoded at its size; section kept" % k)
            for p, lo, hi in S[k]["pages"]:
                a = p["off"]
                d[a:a + 32 + p["csize"]] = data[a:a + 32 + p["csize"]]
            st["refused_sections"] = st.get("refused_sections", 0) + 1
    out = bytes(d)
    # end-to-end check: every section decodes to exactly what was planned, checksums and CRCs are valid
    S2 = D.load_sections(out, want_prov=False)
    for k, s in S.items():
        want = plan.get(k, bufs[k]) if not _refused(out, data, s) else bufs[k]
        if k not in S2 or bytes(S2[k]["buf"]) != want:
            return None, dict(st, failed="section %s decodes differently after the patch" % k)
    f0, f1 = D.failures(data, None), D.failures(out, S2)
    st["audit_failures"] = (len(f0), len(f1))
    if not f1 <= f0:
        return None, dict(st, failed="new audit failures %s" % sorted(map(str, f1 - f0))[:3])
    return (out if out != data else None), st


def _refused(out, data, s):
    return all(out[p["off"]:p["off"] + 32 + p["csize"]] == data[p["off"]:p["off"] + 32 + p["csize"]]
               for p, lo, hi in s["pages"])


def _write_section(d, data, s, buf, cur, st):
    prov = np.frombuffer(s["prov"].tobytes(), np.int32)
    for p, lo, hi in s["pages"]:
        on = np.frombuffer(buf[lo:hi], np.uint8)
        nn = np.frombuffer(cur[lo:hi], np.uint8)
        ch = np.nonzero(on != nn)[0]
        if not len(ch):
            continue
        base = p["off"] + 32
        if not p["comp"]:
            d[base:base + (hi - lo)] = cur[lo:hi]
            D.write_checks(d, p)
            continue
        pv = prov[lo:hi]
        lit = {}
        bad = False
        for o, v in zip(pv[ch].tolist(), nn[ch].tolist()):
            if lit.setdefault(o, v) != v:
                bad = True
        if not bad:
            keys = np.fromiter(lit.keys(), np.int64, len(lit))
            m = np.isin(pv, keys)
            need = nn[m]
            got = np.array([lit[o] for o in pv[m].tolist()], np.uint8)
            bad = bool((need != got).any())
        if not bad:
            for o, v in lit.items():
                d[o] = v
            st["pages_literal"] = st.get("pages_literal", 0) + 1
        else:
            if st.get("pages_reencoded", 0) >= MAX_REENCODE:
                st["pages_fit_failed"] = st.get("pages_fit_failed", 0) + 1
                st["notes"].append("more than %d pages need re-encoding; section kept" % MAX_REENCODE)
                return False
            tail = p["tail"]
            stream = D.fit(cur[lo:hi], p["csize"] - len(tail))
            if stream is None:
                st["pages_fit_failed"] = st.get("pages_fit_failed", 0) + 1
                return False
            d[base:base + p["csize"]] = stream + tail
            st["pages_reencoded"] = st.get("pages_reencoded", 0) + 1
        D.write_checks(d, p)
    return True
