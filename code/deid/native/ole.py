"""Minimal OLE2 (compound file) reader with same-size in-place stream writes and free-space blanking.
Owner: NATIVE builder. Written from the public [MS-CFB] layout; no third-party code.

Why blank instead of sweep: OLE writers leave stale copies of earlier stream contents in sectors the FAT marks free,
in the slack after a stream's last byte, and in unused mini sectors. Those bytes belong to no stream, so no reader
uses them; zeroing them removes every stale identifier whether or not it is on any name list.
"""
import struct

MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
FREE, ENDC = 0xFFFFFFFF, 0xFFFFFFFE
MAXREG = 0xFFFFFFFA


class Ole:
    def __init__(self, data):
        d = self.d = bytearray(data)
        if d[:8] != MAGIC:
            raise ValueError("not OLE")
        u32 = lambda o: struct.unpack_from("<I", d, o)[0]
        self.ss = 1 << struct.unpack_from("<H", d, 30)[0]
        self.mss = 1 << struct.unpack_from("<H", d, 32)[0]
        if self.ss not in (512, 4096) or self.mss != 64:
            raise ValueError("odd sector sizes")
        nfat, dirstart, self.cutoff = u32(44), u32(48), u32(56)
        mfstart, difstart, ndif = u32(60), u32(68), u32(72)
        per = self.ss // 4
        fatsecs = [x for x in struct.unpack_from("<109I", d, 76) if x < MAXREG]
        self.meta = set(fatsecs)
        s, k = difstart, 0
        while s < MAXREG and k < ndif:
            self.meta.add(s)
            vals = struct.unpack_from("<%dI" % per, d, self.off(s))
            fatsecs += [x for x in vals[:-1] if x < MAXREG]
            s, k = vals[-1], k + 1
        fatsecs = fatsecs[:nfat]
        self.meta |= set(fatsecs)
        self.fat = []
        for s in fatsecs:
            self.fat += struct.unpack_from("<%dI" % per, d, self.off(s))
        self.nsec = max(0, (len(d) - self.ss) // self.ss)
        self.dir_chain = self.chain(dirstart)
        dirdata = b"".join(bytes(d[self.off(s):self.off(s) + self.ss]) for s in self.dir_chain)
        self.entries = []
        for i in range(len(dirdata) // 128):
            e = dirdata[i * 128:(i + 1) * 128]
            nl, typ = struct.unpack_from("<H", e, 64)[0], e[66]
            start = struct.unpack_from("<I", e, 116)[0]
            size = struct.unpack_from("<I", e, 120)[0] if self.ss == 512 else struct.unpack_from("<Q", e, 120)[0]
            name = e[:max(0, min(nl, 64) - 2)].decode("utf-16-le", "replace")
            self.entries.append(dict(i=i, name=name, typ=typ, start=start, size=size))
        root = self.entries[0]
        self.mini_chain = self.chain(root["start"]) if root["start"] < MAXREG else []
        self.mf_chain = self.chain(mfstart) if mfstart < MAXREG else []
        self.minifat = []
        for s in self.mf_chain:
            self.minifat += struct.unpack_from("<%dI" % per, d, self.off(s))
        self._paths()

    def _paths(self):
        """Full storage path of each entry, from the red-black tree (child / left / right links)."""
        raw = self.d
        links = {}
        dirdata = b"".join(bytes(raw[self.off(s):self.off(s) + self.ss]) for s in self.dir_chain)
        for e in self.entries:
            b = dirdata[e["i"] * 128:(e["i"] + 1) * 128]
            links[e["i"]] = struct.unpack_from("<III", b, 68)
        seen = set()

        def walk(i, prefix):
            stack = [i]
            while stack:
                j = stack.pop()
                if j >= len(self.entries) or j in seen:
                    continue
                seen.add(j)
                e = self.entries[j]
                e["path"] = prefix + e["name"]
                l, r, c = links[j]
                stack += [x for x in (l, r) if x < MAXREG]
                if c < MAXREG and e["typ"] in (1, 5):
                    walk(c, e["path"] + "/")
        c = links[0][2]
        self.entries[0]["path"] = ""
        if c < MAXREG:
            walk(c, "")

    def off(self, s):
        return (s + 1) * self.ss

    def chain(self, s):
        out, seen = [], set()
        while s < MAXREG:
            if s in seen or s >= len(self.fat):
                raise ValueError("bad FAT chain")
            seen.add(s)
            out.append(s)
            s = self.fat[s]
        return out

    def is_mini(self, e):
        return e["typ"] == 2 and e["size"] < self.cutoff

    def runs(self, e):
        size, left, res = e["size"], e["size"], []
        if size == 0:
            return res
        if self.is_mini(e):
            ms, seen = e["start"], set()
            while ms < MAXREG and left > 0:
                if ms in seen or ms >= len(self.minifat):
                    raise ValueError("bad mini chain")
                seen.add(ms)
                pos = ms * self.mss
                sec = self.mini_chain[pos // self.ss]
                n = min(self.mss, left)
                res.append((self.off(sec) + pos % self.ss, n))
                left -= n
                ms = self.minifat[ms]
        else:
            for s in self.chain(e["start"]):
                if left <= 0:
                    break
                n = min(self.ss, left)
                res.append((self.off(s), n))
                left -= n
        if left:
            raise ValueError("stream shorter than its size")
        return res

    def streams(self):
        return [e for e in self.entries if e["typ"] == 2]

    def read(self, e):
        return b"".join(bytes(self.d[o:o + n]) for o, n in self.runs(e))

    def write(self, e, data):
        if len(data) != e["size"]:
            raise ValueError("size change")
        p = 0
        for o, n in self.runs(e):
            self.d[o:o + n] = data[p:p + n]
            p += n

    def _zero(self, a, b):
        a, b = max(0, a), min(len(self.d), b)
        if b <= a:
            return 0
        nz = (b - a) - bytes(self.d[a:b]).count(0)
        if nz:
            self.d[a:b] = bytes(b - a)
        return nz

    def blank_free(self):
        """Zero free sectors, stream slack, unused mini sectors and the file tail. Returns non-zero bytes cleared."""
        used = set(self.dir_chain) | set(self.mini_chain) | set(self.mf_chain) | self.meta
        mini_used = set()
        for e in self.streams():
            if self.is_mini(e):
                for o, n in self.runs(e):
                    mini_used.add(o)
            elif e["start"] < MAXREG:
                used |= set(self.chain(e["start"]))
        z = 0
        for s in range(min(len(self.fat), self.nsec)):
            if self.fat[s] == FREE and s not in used:
                z += self._zero(self.off(s), self.off(s) + self.ss)
        for e in self.streams():
            r = self.runs(e)
            if not r:
                continue
            o, n = r[-1]
            unit = self.mss if self.is_mini(e) else self.ss
            if n < unit:
                z += self._zero(o + n, o + unit)
        # unused mini sectors (inside the mini stream's sectors)
        root_size = self.entries[0]["size"]
        for k in range(len(self.mini_chain) * self.ss // self.mss):
            pos = k * self.mss
            o = self.off(self.mini_chain[pos // self.ss]) + pos % self.ss
            if o in mini_used:
                continue
            if pos >= root_size or k >= len(self.minifat) or self.minifat[k] == FREE:
                z += self._zero(o, o + self.mss)
        # bytes after the last whole sector
        z += self._zero(self.off(self.nsec), len(self.d))
        return z

    def data(self):
        return bytes(self.d)


# ---- property sets ([MS-OLEPS]) ----
def prop_strings(s):
    """[(fmtid hex, pid, value offset in s, byte length, encoding, value)] for VT_LPSTR / VT_LPWSTR properties."""
    out = []
    try:
        nsets = struct.unpack_from("<I", s, 24)[0]
        for k in range(min(nsets, 4)):
            fmtid = s[28 + 20 * k:44 + 20 * k].hex()
            so = struct.unpack_from("<I", s, 44 + 20 * k)[0]
            n = struct.unpack_from("<I", s, so + 4)[0]
            for j in range(min(n, 4096)):
                pid, po = struct.unpack_from("<II", s, so + 8 + 8 * j)
                p = so + po
                typ = struct.unpack_from("<I", s, p)[0] & 0xFFFF
                if typ == 0x1E:
                    ln = struct.unpack_from("<I", s, p + 4)[0]
                    v = s[p + 8:p + 8 + ln]
                    v = v[:v.find(b"\0")] if b"\0" in v else v
                    out.append((fmtid, pid, p + 8, len(v), "latin-1", v.decode("latin-1")))
                elif typ == 0x1F:
                    ln = struct.unpack_from("<I", s, p + 4)[0]
                    v = s[p + 8:p + 8 + 2 * ln].decode("utf-16-le", "replace").split("\0")[0]
                    out.append((fmtid, pid, p + 8, 2 * len(v), "utf-16-le", v))
    except (struct.error, IndexError):
        pass
    return out
