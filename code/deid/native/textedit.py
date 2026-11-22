"""Same-length identity replacement inside decoded native-file content. Owner: NATIVE builder.

A byte blob is searched through three "views": single-byte text (latin-1, one char per byte) and UTF-16LE at both
byte alignments (one char per two bytes; a char whose high byte is not 0 becomes \\x01 and never matches). Every edit
keeps the length of every view, so the blob keeps its length. Changed chars are written back at their byte offsets.

Order inside one view:
 1. field slots (user folder after C:\\Users\\, UNC host, SW-Author / SW-Last Saved By, Creo Hostname): value of ANY
    length -> mapping.sub_field.
 2. values harvested from field slots anywhere in the file (two passes: harvest the whole file, then edit):
    >= 6 chars: as a substring; 4-5 chars: only with no letter or digit on either side; <= 3 chars: field slot only.
 3. Windows 8.3 short forms (JLEMBU~1) of harvested values -> short form of the long value's stand-in.
 4. names of CAD files found in the text -> ctx.rename(name), only when the new name has the same length.
 5. mapping.sub_text(view, source) for everything the mapping knows.
Plain function use: Editor(ctx) needs only ctx.mapping, ctx.source and ctx.rename (may be None).
"""
import re
import struct
import numpy as np

MFC_RX = re.compile(rb"\xff\xfe\xff[\x01-\xff]")   # MFC CString slot: ff fe ff <n> (n = ff: u16 length follows) + UTF-16

OS_DIRS = {"public", "default", "default user", "all users", "defaultapppool", "localservice", "networkservice",
           "systemprofile"}
_P = r"[^\\/\x00-\x1f\x01\"<>|:*?]"
FIELD_RX = [
    re.compile(r"(?i)(?<=[\\/])(?:Users|Documents and Settings|Dokumente und Einstellungen|home)[\\/](" + _P +
               r"{1,40}?)(?=[\\/])"),
    re.compile(r"\\\\([A-Za-z0-9][A-Za-z0-9._-]{0,62})(?=\\[^\\\x00-\x1f\x01])"),   # UNC; see _UNC_GUARD
    re.compile(r"name=\"SW-(?:Author|Last Saved By)\"[^>]{0,40}><vt:lpstr>([^<\x00\x01]{1,80})</vt:lpstr>"),
    re.compile(r"(?:Hostname|Rechnername|Nom d.h.te|Nome host): '([^'\x00\x01\n]{1,64})'"),
]
# literal prefilters: a field regex only runs on a view that contains one of these strings
FIELD_PRE = [("Users", "users", "USERS", "Settings", "SETTINGS", "settings", "Einstellungen", "home", "HOME"),
             ("\\\\",), ("SW-",), ("name: '", "Nom d", "Nome host")]
CAD_EXT = (r"(?:sldprt|sldasm|slddrw|ipt|iam|idw|ipn|prt|asm|drw|par|psm|dft|dwg|jt|catpart|catproduct|catdrawing)"
           r"(?:\.\d{1,3})?")
_EXT_LITS = tuple("." + e for e in ("sldprt", "sldasm", "slddrw", "ipt", "iam", "idw", "ipn", "prt", "asm", "drw",
                                     "par", "psm", "dft", "dwg", "jt", "catpart", "catproduct", "catdrawing"))
EXT_RX = re.compile(r"(?i)\." + CAD_EXT + r"(?![A-Za-z0-9])")
_NAME_STOP = set('\\/:*?"<>|' + "".join(map(chr, range(0x20))) + "\x01" + "".join(map(chr, range(0x7f, 0xa0))))


class _M:
    def __init__(self, s, a, b):
        self.s, self.a, self.b = s, a, b

    def group(self, i=0):
        return self.s

    def start(self, i=0):
        return self.a

    def end(self, i=0):
        return self.b


def find_names(t):
    """CAD file names in text: anchored on the extension, extended back to a path separator (at most 120 chars)."""
    out = []
    for m in EXT_RX.finditer(t):
        j = k = m.start()
        while k > 0 and j - k < 120 and t[k - 1] not in _NAME_STOP:
            k -= 1
        if k < j:
            out.append(_M(t[k:m.end()], k, m.end()))
    return out


class _NameRx:
    def finditer(self, t):
        return find_names(t)

    def sub(self, fn, t):
        parts, pos = [], 0
        for m in find_names(t):
            if m.a < pos:
                continue
            parts.append(t[pos:m.a])
            parts.append(fn(m))
            pos = m.b
        parts.append(t[pos:])
        return "".join(parts)


NAME_RX = _NameRx()
SHORT_RX = re.compile(r"(?i)(?<![A-Za-z0-9])([A-Z0-9_$&#!%'@^{}-]{1,6})~([0-9])")
_SHORT_DROP = re.compile(r"[^A-Za-z0-9_$&#!%'@^{}~-]")


def shortprefix(long):
    """Windows 8.3 base of a long name: drop spaces, dots and invalid chars, upper-case, first 6 chars."""
    return _SHORT_DROP.sub("", long).upper()[:6]


def case_like(src, new):
    """new with the letter case of src, char by char (same length)."""
    return "".join(n.upper() if s.isupper() else n.lower() if s.islower() else n for s, n in zip(src, new))


_U16_4 = re.compile(rb"[\x20-\xff]\x00[\x20-\xff]\x00[\x20-\xff]\x00[\x20-\xff]\x00")


def _pr(c):
    return 0x20 <= c < 0x7F or c >= 0xA0


def run_ok(get, i, L, minrun=8):
    """True if position i of a view (get(j) -> char code, length L) lies in a run of >= minrun readable chars.
    Same answer as readable(...)[i], computed locally (at most 2 * minrun lookups)."""
    if not _pr(get(i)):
        return False
    k, j = 1, i - 1
    while j >= 0 and k < minrun and _pr(get(j)):
        k, j = k + 1, j - 1
    j = i + 1
    while j < L and k < minrun and _pr(get(j)):
        k, j = k + 1, j + 1
    return k >= minrun


def views(blob):
    """[(text, start, stride)] for latin-1 and both UTF-16LE alignments (the latter only if the blob has NULs)."""
    out = [(blob.decode("latin-1"), 0, 1)]
    # every pattern needs >= 4 consecutive readable chars; no such UTF-16 run anywhere -> the UTF-16 views can't match
    if len(blob) >= 8 and blob.count(0) >= 8 and _U16_4.search(blob):
        a8 = np.frombuffer(blob, np.uint8)
        for a in (0, 1):
            n = (len(a8) - a) // 2
            lo, hi = a8[a:a + 2 * n:2], a8[a + 1:a + 2 * n:2]
            v = np.where(hi == 0, lo, 1).astype(np.uint8)
            out.append((v.tobytes().decode("latin-1"), a, 2))
    return out


class Editor:
    def __init__(self, ctx, extra_fields=()):
        self.ctx, self.m, self.src = ctx, ctx.mapping, ctx.source
        self.fields = list(zip(FIELD_RX, FIELD_PRE)) + [(rx, None) for rx in extra_fields]
        self.rename_hits = 0
        self.stems = {}         # old model/file stem (lower) -> new stem, applied as whole tokens
        self._srx = None
        self.vals = {}          # lower form -> original value (harvested from field slots)
        self.short = {}         # 6-char short prefix (upper) -> long value
        self.renames = {}       # name -> new name (cache of ctx.rename)
        self.notes = []
        self.count = 0
        self._rx = None

    # ---------- pass 1 ----------
    def add(self, v):
        v = v.strip(" \x00")
        if not v or v.lower() in OS_DIRS:
            return
        if v.lower() not in self.vals:
            self.vals[v.lower()] = v
            self._rx = None
            if "~" not in v and len(_SHORT_DROP.sub("", v)) >= 6:
                self.short.setdefault(shortprefix(v), v)

    def add_stem(self, old, new):
        """Bare model or file stem renamed by the path plan (same length): replaced as a whole token."""
        if len(old) >= 4 and len(new) == len(old) and old.lower() != new.lower():
            self.stems[old.lower()] = new
            self._srx = None

    def rename(self, name):
        """ctx.rename for a bare name, trying the lower-case form too; same length or the name itself."""
        if name in self.renames:
            return self.renames[name]
        r = name
        for cand in (name, name.lower()):
            try:
                x = self.ctx.rename(cand) if self.ctx.rename is not None else cand
            except Exception:
                x = cand
            if x != cand:
                r = case_like(name, x) if cand != name and len(x) == len(name) else x
                break
        if r != name and len(r) != len(name):
            self.notes.append("referenced name %r: new name has another length; kept" % name[:40])
            r = name
        self.renames[name] = r
        return r

    def harvest(self, blob):
        for text, _, _ in views(blob):
            for rx, pre in self.fields:
                if pre is None or any(k in text for k in pre):
                    for m in rx.finditer(text):
                        if rx is FIELD_RX[1] and m.start() > 0 and text[m.start() - 1] == "\\":
                            continue
                        self.add(m.group(1))

    # ---------- stand-ins ----------
    def sf(self, v):
        """Stand-in of an identity value, same length, or the value itself."""
        m = re.fullmatch(r"(.{1,6})~(\d{1,2})", v)
        if m:
            p = m.group(1)
            L = self.short.get(p.upper())
            L = L if L and "~" not in L else None
            new = shortprefix(self.sf(L))[:len(p)] if L else self.m.sub_field(p, self.src)
            return case_like(p, new) + v[len(p):] if len(new) == len(p) else v
        try:
            r = self.m.sub_field(v, self.src)
        except Exception:
            return v
        if len(r) != len(v):
            self.notes.append("sub_field changed length; value kept")
            return v
        return r

    def _vals_rx(self):
        if self._rx is None:
            pats = []
            for low, v in sorted(self.vals.items(), key=lambda kv: -len(kv[0])):
                forms = {v}
                try:
                    forms.add(v.encode("utf-8").decode("latin-1"))
                except Exception:
                    pass
                for f in forms:
                    if len(f) < 4:
                        continue
                    e = re.escape(f)
                    pats.append(e if len(f) >= 6 else r"(?<![A-Za-z0-9])" + e + r"(?![A-Za-z0-9])")
            self._rx = re.compile("|".join(pats), re.I) if pats else False
        return self._rx

    def _sub_val(self, mt):
        s = mt.group(0)
        try:
            u = s.encode("latin-1").decode("utf-8")
        except Exception:
            u = s
        if u != s and u.lower() in self.vals:
            r = self.sf(u).encode("utf-8").decode("latin-1")
        else:
            r = self.sf(s)
        if len(r) != len(s):
            return s
        if r != s:
            self.count += 1
        return r

    # ---------- pass 2 ----------
    def edit_fields(self, t):
        def fld(m):
            if m.re is FIELD_RX[1] and m.start() > 0 and m.string[m.start() - 1] == "\\":
                return m.group(0)      # _UNC_GUARD: the old pattern had (?<![\\]) in front
            v = m.group(1)
            if v.strip(" \x00").lower() in OS_DIRS:
                return m.group(0)
            r = self.sf(v)
            if r != v:
                self.count += 1
            g = m.group(0)
            return g[:m.start(1) - m.start(0)] + r + g[m.end(1) - m.start(0):]
        for rx, pre in self.fields:
            if pre is None or any(k in t for k in pre):
                t = rx.sub(fld, t)
        return t

    def _lits(self):
        if getattr(self, "_lit_for", None) is not self._rx or getattr(self, "_lit_n", -1) != len(self.vals):
            lits = set()
            for v in self.vals.values():
                if len(v) >= 4:
                    lits.add(v.lower())
                    try:
                        lits.add(v.encode("utf-8").decode("latin-1").lower())
                    except Exception:
                        pass
            self._lits_c, self._lit_for, self._lit_n = lits, self._rx, len(self.vals)
        return self._lits_c

    def edit_free(self, t, rename=True):
        tl = t.lower()
        rx = self._vals_rx()
        if rx and any(v in tl for v in self._lits()):
            t = rx.sub(self._sub_val, t)
        if self.short and "~" in t and any(k.lower() + "~" in tl for k in self.short):
            def sh(m):
                p = m.group(1)
                L = self.short.get(p.upper())
                if not L or len(p) != len(shortprefix(L)):
                    return m.group(0)
                new = shortprefix(self.sf(L))
                if len(new) != len(p) or new == p.upper():
                    return m.group(0)
                self.count += 1
                return case_like(p, new) + m.group(0)[len(p):]
            t = SHORT_RX.sub(sh, t)
        if rename and self.ctx.rename is not None and any(e in tl for e in _EXT_LITS):
            def rn(m):
                s = m.group(1)
                r = self.rename(s)
                if r != s:
                    self.count += 1
                    self.rename_hits += 1
                return r
            t = NAME_RX.sub(rn, t)
        if self.stems and any(k in tl for k in self.stems):
            if self._srx is None:
                self._srx = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(
                    re.escape(k) for k in sorted(self.stems, key=len, reverse=True)) + r")(?![A-Za-z0-9_])", re.I)
            def st(m):
                self.count += 1
                return case_like(m.group(1), self.stems[m.group(1).lower()])
            t = self._srx.sub(st, t)
        return t

    def edit(self, blob, rename=True, mapping=True):
        """Same-length edited copy of blob and the number of replacements in it.
        Field slots (step 1) are parsed string slots and apply anywhere. Every other change (steps 2-5) is kept only
        where it lies in a run of at least 8 readable characters of some view (council ruling D9: no matcher over
        undecoded binary data; names under 8 characters only with text around them). Step 5 is one
        mapping.sub_bytes call (the mapping engine covers Latin-1, UTF-8 and UTF-16LE); the same mask filters it."""
        c0 = self.count
        n = len(blob)
        if n == 0:
            return blob, 0
        cur = np.frombuffer(blob, np.uint8).copy()
        vinfo = []                      # (start, stride, length) of each view, for the mapping mask
        for text, a, s in views(blob):
            L = len(text)
            t1 = self.edit_fields(text)
            t2 = self.edit_free(t1, rename=rename)
            vinfo.append((a, s, L))
            if t1 is text and t2 is t1 or t2 == text:
                continue
            n1 = np.frombuffer(t1.encode("latin-1", "replace"), np.uint8)
            n2 = np.frombuffer(t2.encode("latin-1", "replace"), np.uint8)
            if len(n1) != L or len(n2) != L:
                self.notes.append("non-latin-1 stand-in; view skipped")
                continue
            cv = cur[a:a + s * L:s]
            ov = np.frombuffer(text.encode("latin-1"), np.uint8)
            f = ov != n1
            cv[f] = n1[f]
            for i in np.nonzero(n1 != n2)[0].tolist():
                if run_ok(n1.__getitem__, i, L):
                    cv[i] = n2[i]
        # parsed string slots (MFC CString, used by SolidWorks and other MFC writers): edited whole, no mask
        sl = []
        for mt in MFC_RX.finditer(blob):
            ln, st = blob[mt.start() + 3], mt.start() + 4
            if ln == 0xFF:
                if st + 2 > n:
                    continue
                ln, st = struct.unpack_from("<H", blob, st)[0], st + 2
            if ln == 0 or st + 2 * ln > n:
                continue
            try:
                t = cur[st:st + 2 * ln].tobytes().decode("utf-16-le")
            except UnicodeDecodeError:
                continue
            if "\x00" in t:
                continue
            sl.append((st, ln, t))
        if sl:
            joined = "\x00".join(t for _, _, t in sl)
            j2 = self.edit_free(self.edit_fields(joined), rename=rename)
            if mapping:
                try:
                    j3, k = self.m.sub_text(j2, self.src)
                    if len(j3) == len(j2):
                        j2, self.count = j3, self.count + k
                except Exception:
                    pass
            if j2 != joined and len(j2) == len(joined):
                q = 0
                for st, ln, t in sl:
                    t2 = j2[q:q + len(t)]
                    q += len(t) + 1
                    if t2 != t:
                        nb = t2.encode("utf-16-le")
                        if len(nb) == 2 * ln:
                            cur[st:st + 2 * ln] = np.frombuffer(nb, np.uint8)
        out = cur.tobytes()
        if mapping:
            try:
                o2, k = self.m.sub_bytes(out, self.src)
                if k and len(o2) == n:
                    a2 = np.frombuffer(o2, np.uint8)
                    ch = (a2 != cur)
                    c8 = cur
                    keep = np.zeros(n, bool)
                    for p in np.nonzero(ch)[0].tolist():
                        for a, s, L in vinfo:
                            if (p - a) % s:
                                continue
                            j = (p - a) // s
                            if s == 1:
                                ok = run_ok(c8.__getitem__, j, L)
                            else:
                                ok = run_ok(lambda q, a=a: int(c8[a + 2 * q]) if c8[a + 2 * q + 1] == 0 else 1, j, L)
                            if ok:
                                keep[p] = True
                                break
                    if keep.any():
                        cur = np.where(keep, a2, cur)
                        out = cur.tobytes()
                        self.count += k if keep.sum() == ch.sum() else max(1, k * int(keep.sum()) // int(ch.sum()))
            except Exception as e:
                self.notes.append("sub_bytes error %s" % type(e).__name__)
        return out, self.count - c0


def readable(arr, minrun=8):
    """Mask of positions inside a run of >= minrun readable chars (printable ASCII, space, Latin-1 letters)."""
    pr = ((arr >= 0x20) & (arr < 0x7F)) | (arr >= 0xA0)
    d = np.diff(np.concatenate(([0], pr.astype(np.int8), [0])))
    st, en = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
    keep = (en - st) >= minrun
    mark = np.zeros(len(arr) + 1, np.int32)
    np.add.at(mark, st[keep], 1)
    np.add.at(mark, en[keep], -1)
    return np.cumsum(mark)[:len(arr)] > 0
