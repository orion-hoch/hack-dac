"""Archives: zip (and the zip-based CAD containers FCStd, 3MF, F3D, F3Z), tar, gzip, .Z, RAR. CORE builder.

Every member goes through recurse(bytes, "<archive>!/<member>"), so a member and its unpacked copy in
"<name>.extracted/" get the same handler, the same cache key and the same output bytes.
  zip  raw-copy rewriter: untouched members are copied byte for byte (method, extras, descriptor); an edited
       member is recompressed with its own method (stored, deflate, zstd 93, bzip2 12); a renamed member gets its
       0x7075 unicode-path extra rebuilt with the new name (otherwise the old name survives there); the archive
       comment is scrubbed (a 40-hex git commit id is replaced digit-for-digit); encrypted members keep their
       bytes and only their names change; members of generic .zip are renamed, members of CAD containers never
       (their names are referenced from inside, e.g. FCStd file="...", F3Z Manifest.json root).
  tar  headers patched in place: uname/gname are identity fields; member names and GNU long names renamed
       same length; checksum recomputed; data re-padded only if a member's size changed.
  gzip FNAME renamed, FCOMMENT scrubbed, payload recompressed only if the member changed.
  .Z   LZW decoded (unlzw3, MIT), re-encoded by our own LZW writer only if the member changed; the result is
       decoded again and must equal the new member, else the file is kept and listed.
  rar  read with the unrar binary (extraction only, as its licence allows), written as stored RAR5 (rar5 store
       writer from the public technote). Encrypted or unreadable: kept, listed.
Plain functions: rewrite_zip(data, recurse, rename, sub, field, member_names=True) etc. Never raise to the runner.
"""
import bz2, os, re, shutil, struct, subprocess, tempfile, zlib
from deid.api import register, Result

HANDLER_VERSION = 3
MAX_MEMBER = 1_500_000_000
CAD_ZIP = (".fcstd", ".3mf", ".f3d", ".f3z", ".protein", ".odg", ".odt", ".ods", ".docx", ".xlsx", ".pptx", ".kmz", ".jar")
UNRAR = os.environ.get("DEID_UNRAR", os.path.expanduser("~/hack-osu/work/gaps/build/unrar/unrar"))
_SHA = re.compile(r"\b[0-9a-f]{40}\b")
CD = "<4sHHHHHHLLLHHHHHLL"; LH = "<4sHHHHHLLLHH"


def _hexsha(text, field):
    return _SHA.sub(lambda m: "".join("0123456789abcdef"[(int(c, 16) * 7 + 3) % 16] for c in m.group(0)), text)


def _extras(extra):
    i, out = 0, []
    while i + 4 <= len(extra):
        hid, ln = struct.unpack("<HH", extra[i:i + 4]); out.append((hid, extra[i + 4:i + 4 + ln])); i += 4 + ln
    return out, extra[i:]


def _fix_extra(extra, newname_raw, newname):
    items, rest = _extras(extra)
    out = b""
    for hid, body in items:
        if hid == 0x7075:                      # Info-ZIP unicode path: version, CRC32 of header name, UTF-8 name
            body = b"\x01" + struct.pack("<L", zlib.crc32(newname_raw) & 0xFFFFFFFF) + newname.encode("utf-8")
        out += struct.pack("<HH", hid, len(body)) + body
    return out + rest


def _decomp(method, raw, usize):
    if method == 0:
        return raw
    if method == 8:
        d = zlib.decompressobj(-15); out = d.decompress(raw, MAX_MEMBER)
        return out
    if method == 93:
        import zstandard
        return zstandard.ZstdDecompressor().decompress(raw, max_output_size=max(usize, 1))
    if method == 12:
        return bz2.decompress(raw)
    raise NotImplementedError("zip method %d" % method)


def _comp(method, data):
    if method == 0:
        return data
    if method == 8:
        c = zlib.compressobj(9, zlib.DEFLATED, -15); return c.compress(data) + c.flush()
    if method == 93:
        import zstandard
        return zstandard.ZstdCompressor(level=3).compress(data)
    if method == 12:
        return bz2.compress(data)
    raise NotImplementedError("zip method %d" % method)


def rewrite_zip(data, recurse, rename, sub, field=None, member_names=True):
    """-> (bytes or None, n, notes). recurse(bytes, member name) -> new bytes or None."""
    notes, n = [], 0
    e = data.rfind(b"PK\x05\x06", max(0, len(data) - 65558))
    if e < 0:
        return None, 0, ["zip: no end record"]
    _, _, _, cnt, _, cdsize, cdoff, clen = struct.unpack("<4sHHHHLLH", data[e:e + 22])
    if 0xFFFFFFFF in (cdsize, cdoff) or cnt == 0xFFFF:
        return None, 0, ["LIMIT zip64 archive kept as is"]
    comment = data[e + 22:e + 22 + clen]
    recs, p = [], cdoff
    for _ in range(cnt):
        h = list(struct.unpack(CD, data[p:p + 46]))
        if h[0] != b"PK\x01\x02":
            return None, 0, ["zip: bad central directory"]
        nlen, elen, cl = h[10], h[11], h[12]
        recs.append({"h": h, "name": data[p + 46:p + 46 + nlen], "extra": data[p + 46 + nlen:p + 46 + nlen + elen],
                     "comment": data[p + 46 + nlen + elen:p + 46 + nlen + elen + cl]})
        p += 46 + nlen + elen + cl
    offs = sorted(r["h"][16] for r in recs) + [cdoff]
    nxt = {offs[i]: offs[i + 1] for i in range(len(offs) - 1)}
    out = bytearray(); enc_names = 0
    for r in recs:
        h = r["h"]; flags, method, crc, csize, usize, off = h[3], h[4], h[7], h[8], h[9], h[16]
        utf8 = bool(flags & 0x800)
        name = r["name"].decode("utf-8" if utf8 else "cp437", "replace")
        lh = data[off:off + 30]
        lnlen, lelen = struct.unpack("<HH", lh[26:30]); ds = off + 30 + lnlen + lelen
        lextra = data[off + 30 + lnlen:ds]
        newname = rename(name) if member_names else name
        if len(newname) != len(name):
            newname = name
        newdata = None
        if not name.endswith("/") and usize:
            if flags & 1:
                enc_names += 1
            elif usize > MAX_MEMBER:
                notes.append("KEPT member over 1.5 GB not opened")
            else:
                try:
                    content = _decomp(method, data[ds:ds + csize], usize)
                    newdata = recurse(content, name)
                    if newdata is None and _F3D_BIN.search(name):          # Fusion binary members: readable runs only
                        from deid.handlers.misc import readable_runs
                        nb, kk = readable_runs(content, sub)
                        newdata = nb if kk else None
                except NotImplementedError as ex:
                    notes.append("KEPT member with %s" % ex)
                except Exception as ex:
                    notes.append("KEPT member not readable (%s)" % type(ex).__name__)
        h2 = list(h); r["newoff"] = len(out)
        if newdata is None and newname == name:
            out += data[off:nxt[off]]
            continue
        raw_name = newname.encode("utf-8") if (utf8 or not _cp437(newname)) else newname.encode("cp437")
        if raw_name != r["name"] and not utf8 and not _cp437(newname):
            flags |= 0x800
        if newdata is not None:
            comp = _comp(method, newdata); crc = zlib.crc32(newdata) & 0xFFFFFFFF; csize, usize = len(comp), len(newdata)
            flags &= ~0x08; n += 1
        else:
            comp = data[ds:ds + csize]
            if flags & 0x08:                     # keep the descriptor bytes after the data
                comp = data[ds:nxt[off]]
        if newname != name:
            n += 1
            lextra = _fix_extra(lextra, raw_name, newname); r["extra"] = _fix_extra(r["extra"], raw_name, newname)
        out += struct.pack(LH, b"PK\x03\x04", h[2], flags, method, h[5], h[6], crc if not flags & 0x08 else 0,
                           csize if not flags & 0x08 else 0, usize if not flags & 0x08 else 0,
                           len(raw_name), len(lextra)) + raw_name + lextra + comp
        h2[3], h2[7], h2[8], h2[9] = flags, crc, csize, usize
        r["h"] = h2; r["name"] = raw_name
    cd0 = len(out)
    for r in recs:
        h = r["h"]
        out += struct.pack(CD, b"PK\x01\x02", h[1], h[2], h[3], h[4], h[5], h[6], h[7], h[8], h[9], len(r["name"]),
                           len(r["extra"]), len(r["comment"]), h[13], h[14], h[15], r["newoff"]) + r["name"] + r["extra"] + r["comment"]
    ctext = comment.decode("latin-1")
    nc, k = sub(ctext)                         # council D2: 40-hex commit ids stay verbatim
    if nc != ctext and len(nc) == len(ctext):
        comment = nc.encode("latin-1", "replace"); n += 1
    out += struct.pack("<4sHHHHLLH", b"PK\x05\x06", 0, 0, len(recs), len(recs), len(out) - cd0, cd0, len(comment)) + comment
    if enc_names:
        notes.append("KEPT %d encrypted member(s): content not readable without the password, names replaced" % enc_names)
    if n:
        ok, why = verify_zip(bytes(out), len(recs))
        if not ok:
            return None, 0, notes + ["KEPT zip: rewritten archive failed its re-read check (%s), original kept" % why]
    return (bytes(out) if n else None), n, notes


_F3D_BIN = re.compile(r"(?:InstanceProperties\.bin|DefinitionIteratorProperties\.bin|BulkStream\.dat|MetaStream\.dat|"
                      r"Manifest\.dat|Properties\.dat)$")


def verify_zip(data, count):
    """Gate: re-read every member from the central directory, decompress it (zstd included) and check CRC and size."""
    e = data.rfind(b"PK\x05\x06", max(0, len(data) - 65558))
    if e < 0:
        return False, "no end record"
    cnt, cdsize, cdoff = struct.unpack("<HLL", data[e + 10:e + 20])
    if cnt != count:
        return False, "member count"
    p = cdoff
    for _ in range(cnt):
        h = struct.unpack(CD, data[p:p + 46])
        if h[0] != b"PK\x01\x02":
            return False, "central directory"
        flags, method, crc, csize, usize, off = h[3], h[4], h[7], h[8], h[9], h[16]
        p += 46 + h[10] + h[11] + h[12]
        if data[off:off + 4] != b"PK\x03\x04":
            return False, "local header"
        if flags & 1 or not usize:
            continue
        ln, le = struct.unpack("<HH", data[off + 26:off + 30]); ds = off + 30 + ln + le
        try:
            c = _decomp(method, data[ds:ds + csize], usize)
        except NotImplementedError:
            continue
        except Exception:
            return False, "member does not decompress"
        if len(c) != usize or (zlib.crc32(c) & 0xFFFFFFFF) != crc:
            return False, "member CRC"
    return True, ""


def same_shape(a, b):
    """Same length, and every changed byte is a letter, digit or non-ASCII byte on both sides."""
    if len(a) != len(b):
        return False
    ok = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789") | set(range(128, 256))
    return all(x in ok and y in ok for x, y in zip(a, b) if x != y)


def _cp437(s):
    try:
        s.encode("cp437"); return True
    except UnicodeEncodeError:
        return False


# ------------------------------------------------------------------ tar
def _oct(b):
    if b and b[0] & 0x80:
        return int.from_bytes(b[1:], "big")
    s = b.split(b"\x00")[0].strip()
    return int(s, 8) if s else 0


def rewrite_tar(data, recurse, rename, sub, field):
    out = bytearray(); pos = 0; n = 0; longname = None; notes = []
    while pos + 512 <= len(data):
        h = bytearray(data[pos:pos + 512])
        if not any(h):
            out += data[pos:]; break
        size = _oct(bytes(h[124:136])); typ = bytes(h[156:157])
        body = data[pos + 512:pos + 512 + size]; pad = (-size) % 512
        ustar = h[257:262] == b"ustar"
        for a, z in ((265, 297), (297, 329)) if ustar else ():
            v = h[a:z].split(b"\x00")[0].decode("latin-1")
            if v:
                nv = field(v)
                if nv != v and len(nv) == len(v):
                    h[a:a + len(v)] = nv.encode("latin-1", "replace"); n += 1
        name = h[0:100].split(b"\x00")[0].decode("utf-8", "surrogateescape")
        if ustar and h[345:346] != b"\x00":
            name = h[345:500].split(b"\x00")[0].decode("utf-8", "surrogateescape") + "/" + name
        full = longname or name
        if typ == b"L":
            ln = body.split(b"\x00")[0].decode("utf-8", "surrogateescape"); nn = rename(ln)
            if nn != ln and len(nn.encode("utf-8", "surrogateescape")) == len(ln.encode("utf-8", "surrogateescape")):
                body = nn.encode("utf-8", "surrogateescape") + body[len(ln.encode("utf-8", "surrogateescape")):]; n += 1
            longname = ln
        elif typ in (b"x", b"g"):
            t = body.decode("utf-8", "surrogateescape"); nt, k = sub(t)
            if k and len(nt.encode("utf-8", "surrogateescape")) == len(body):
                body = nt.encode("utf-8", "surrogateescape"); n += k
        else:
            for a, z in ((0, 100), (345, 500)) if ustar else ((0, 100),):
                seg = h[a:z].split(b"\x00")[0].decode("utf-8", "surrogateescape")
                ns = rename(seg) if seg else seg
                if ns != seg and len(ns.encode("utf-8", "surrogateescape")) == len(seg.encode("utf-8", "surrogateescape")):
                    h[a:a + len(seg.encode("utf-8", "surrogateescape"))] = ns.encode("utf-8", "surrogateescape"); n += 1
            if typ in (b"0", b"\x00", b"7") and size:
                nb = recurse(bytes(body), full)
                if nb is not None:
                    body = nb; n += 1
                    if len(nb) != size:
                        if size >= 8 ** 11:
                            notes.append("KEPT tar member over 8 GB"); body = data[pos + 512:pos + 512 + size]
                        else:
                            h[124:136] = b"%011o\x00" % len(nb); pad = (-len(nb)) % 512
            longname = None
        h[148:156] = b"        "
        h[148:156] = b"%06o\x00 " % sum(h)
        out += h + body + b"\x00" * pad
        pos += 512 + size + ((-size) % 512)
    else:
        out += data[pos:]
    if n:
        ok, why = _verify_tar(data, bytes(out))
        if not ok:
            return None, 0, notes + ["KEPT tar: rewritten archive failed its re-read check (%s), original kept" % why]
    return (bytes(out) if n else None), n, notes


def _verify_tar(old, new):
    """Gate (tar has no data checksum): same member list and types, header checksums valid (tarfile), each member
    either the same length with only letters/digits changed, or itself a re-read archive."""
    import io, tarfile
    try:
        a = tarfile.open(fileobj=io.BytesIO(old)); b = tarfile.open(fileobj=io.BytesIO(new))
        ma, mb = a.getmembers(), b.getmembers()
    except Exception as ex:
        return False, "tarfile: %s" % type(ex).__name__
    if len(ma) != len(mb) or any(x.type != y.type for x, y in zip(ma, mb)):
        return False, "member list"
    for x, y in zip(ma, mb):
        if len(x.name) != len(y.name):
            return False, "member name length"
        if x.isfile():
            da, db = a.extractfile(x).read(), b.extractfile(y).read()
            if len(db) != y.size:
                return False, "member size"
            if da != db and len(da) == len(db) and not same_shape(da, db):
                return False, "member bytes outside edited spans"
    return True, ""


# ------------------------------------------------------------------ gzip and .Z
def rewrite_gzip(data, recurse, rename, sub, own_name):
    flg = data[3]; p = 10; name = None; ctext = None
    if flg & 4:
        xlen = struct.unpack("<H", data[p:p + 2])[0]; p += 2 + xlen
    hdr_end = p
    if flg & 8:
        q = data.index(b"\x00", p); name = data[p:q].decode("latin-1"); p = q + 1
    if flg & 16:
        q = data.index(b"\x00", p); ctext = data[p:q].decode("latin-1"); p = q + 1
    if flg & 2:
        p += 2
    d = zlib.decompressobj(-15); inner = d.decompress(data[p:], MAX_MEMBER)
    notes = []
    if d.unused_data[8:]:
        notes.append("KEPT gzip members after the first unchanged")
    newinner = recurse(inner, name or own_name)
    header = bytearray(data[:hdr_end]); n = 0
    if flg & 8:
        nn = rename(name)
        if nn != name and len(nn) == len(name):
            n += 1
        else:
            nn = name
        header += nn.encode("latin-1", "replace") + b"\x00"
    if flg & 16:
        nc, k = sub(ctext); n += k
        header += (nc if len(nc) == len(ctext) else ctext).encode("latin-1", "replace") + b"\x00"
    if flg & 2:
        header[3] &= ~2
    if newinner is None and n == 0:
        return None, 0, notes
    if newinner is None:
        payload = data[p:len(data) - len(d.unused_data) + 8] if d.unused_data else data[p:]
        return bytes(header) + payload, n, notes
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    body = c.compress(newinner) + c.flush()
    tail = struct.pack("<LL", zlib.crc32(newinner) & 0xFFFFFFFF, len(newinner) & 0xFFFFFFFF)
    outb = bytes(header) + body + tail
    import gzip as _gz
    if _gz.decompress(outb) != newinner:
        return None, 0, notes + ["KEPT gzip: rewritten stream did not verify, original kept"]
    return outb, n + 1, notes


def lzw_compress(data, maxbits=16):
    """Unix compress (.Z) writer, block mode, no CLEAR codes. Mirrors compress.c's code-group padding."""
    out = bytearray(b"\x1f\x9d" + bytes([0x80 | maxbits]))
    if not data:
        return bytes(out)
    n_bits, maxmax = 9, 1 << maxbits
    maxcode = (1 << n_bits) - 1; free_ent = 257; table = {}
    acc = nacc = ngroup = 0; group = bytearray()

    def output(code):
        nonlocal acc, nacc, ngroup, n_bits, maxcode
        acc |= code << nacc; nacc += n_bits
        while nacc >= 8:
            group.append(acc & 255); acc >>= 8; nacc -= 8
        ngroup += 1
        if ngroup == 8:
            out.extend(group); group.clear(); ngroup = 0
        if free_ent > maxcode:
            if ngroup:
                if nacc:
                    group.append(acc & 255); acc = nacc = 0
                group.extend(b"\x00" * (n_bits - len(group))); out.extend(group); group.clear(); ngroup = 0
            n_bits += 1
            maxcode = maxmax if n_bits == maxbits else (1 << n_bits) - 1
    ent = data[0]
    for c in data[1:]:
        k = (ent << 8) | c
        v = table.get(k)
        if v is not None:
            ent = v; continue
        output(ent)
        if free_ent < maxmax:
            table[k] = free_ent; free_ent += 1
        ent = c
    output(ent)
    if nacc:
        group.append(acc & 255)
    out.extend(group)
    return bytes(out)


def rewrite_z(data, recurse, own_name):
    import unlzw3
    inner = unlzw3.unlzw(data)
    new = recurse(inner, own_name)
    if new is None:
        return None, 0, []
    out = lzw_compress(new, data[2] & 0x1F if data[2] & 0x1F >= 9 else 16)
    back = unlzw3.unlzw(out)
    if back != new or (len(new) == len(inner) and not same_shape(inner, new)):
        return None, 0, ["KEPT .Z member: re-encoded stream did not verify, original kept"]
    return out, 1, []


# ------------------------------------------------------------------ rar
def rar5_write(members):
    import time as _t
    SIG = b"Rar!\x1a\x07\x01\x00"

    def vint(v):
        o = bytearray()
        while True:
            b = v & 0x7F; v >>= 7
            if v:
                o.append(b | 0x80)
            else:
                o.append(b); return bytes(o)

    def block(body):
        hs = vint(len(body)); return struct.pack("<I", zlib.crc32(hs + body) & 0xFFFFFFFF) + hs + body
    out = bytearray(SIG + block(vint(1) + vint(0) + vint(0)))
    for name, data, mtime in members:
        nm = name.encode("utf-8")
        if data is None:
            out += block(vint(2) + vint(0) + vint(0x0001 | 0x0002) + vint(0) + vint(0x10) + struct.pack("<I", mtime) + vint(0) + vint(0) + vint(len(nm)) + nm)
        else:
            out += block(vint(2) + vint(0x0002) + vint(len(data)) + vint(0x0002 | 0x0004) + vint(len(data)) + vint(0x20) +
                         struct.pack("<I", mtime) + struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF) + vint(0) + vint(0) + vint(len(nm)) + nm)
            out += data
    out += block(vint(5) + vint(0) + vint(0))
    return bytes(out)


def rewrite_rar(data, recurse, rename):
    if not os.path.exists(UNRAR):
        return None, 0, ["KEPT rar: no unrar reader on this machine"]
    tmp = tempfile.mkdtemp(prefix="deid_rar_")
    try:
        a = os.path.join(tmp, "in.rar"); open(a, "wb").write(data); x = os.path.join(tmp, "x"); os.mkdir(x)
        r = subprocess.run([UNRAR, "x", "-p-", "-y", "-inul", "-o+", a, x + "/"], capture_output=True, timeout=600)
        if r.returncode != 0:
            return None, 0, ["KEPT rar: not readable without a password or damaged (unrar rc %d)" % r.returncode]
        mem = []; n = 0
        for dp, dn, fn in os.walk(x):
            dn.sort()
            rel = os.path.relpath(dp, x)
            if rel != ".":
                nr = rename(rel); mem.append((nr if len(nr) == len(rel) else rel, None, int(os.path.getmtime(dp))))
            for f in sorted(fn):
                p = os.path.join(dp, f)
                if os.path.islink(p):
                    continue
                m = os.path.relpath(p, x); b = open(p, "rb").read()
                nb = recurse(b, m); nm = rename(m)
                n += (nb is not None) + (nm != m)
                mem.append((nm, b if nb is None else nb, int(os.path.getmtime(p))))
        if not n:
            return None, 0, []
        return rar5_write(mem), n, ["rar rewritten as stored RAR5 (no compression)"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ 7z (libarchive, BSD-2, through ctypes)
LIBARCHIVE = os.environ.get("DEID_LIBARCHIVE", "")
SEVENZIP = os.environ.get("DEID_7ZZ", os.path.expanduser("~/hack-osu/work/gaps/tools/7z/7zz"))
_LA = []


def _la():
    if _LA:
        return _LA[0]
    import ctypes, ctypes.util
    cands = [LIBARCHIVE, ctypes.util.find_library("archive") or "", "libarchive.so.13",
             os.path.expanduser("~/hack-osu/tools/libarchive/lib/libarchive.so")]
    L = None
    for c in cands:
        if c:
            try:
                L = ctypes.CDLL(c); break
            except OSError:
                continue
    if L is None:
        raise NotImplementedError("libarchive not found")
    V, P = ctypes.c_void_p, ctypes.c_char_p
    for fn, res, args in (("archive_read_new", V, []), ("archive_read_support_format_7zip", ctypes.c_int, [V]),
                          ("archive_read_support_filter_all", ctypes.c_int, [V]),
                          ("archive_read_open_memory", ctypes.c_int, [V, V, ctypes.c_size_t]),
                          ("archive_read_next_header", ctypes.c_int, [V, ctypes.POINTER(V)]),
                          ("archive_read_data", ctypes.c_ssize_t, [V, V, ctypes.c_size_t]),
                          ("archive_read_free", ctypes.c_int, [V]), ("archive_entry_pathname_utf8", P, [V]),
                          ("archive_entry_pathname", P, [V]), ("archive_entry_filetype", ctypes.c_uint, [V]),
                          ("archive_entry_mtime", ctypes.c_long, [V]), ("archive_entry_is_encrypted", ctypes.c_int, [V]),
                          ("archive_write_new", V, []), ("archive_write_set_format_7zip", ctypes.c_int, [V]),
                          ("archive_write_set_format_option", ctypes.c_int, [V, P, P, P]),
                          ("archive_write_open_filename", ctypes.c_int, [V, P]),
                          ("archive_write_header", ctypes.c_int, [V, V]),
                          ("archive_write_data", ctypes.c_ssize_t, [V, V, ctypes.c_size_t]),
                          ("archive_write_close", ctypes.c_int, [V]), ("archive_write_free", ctypes.c_int, [V]),
                          ("archive_entry_new", V, []), ("archive_entry_free", None, [V]),
                          ("archive_entry_set_pathname_utf8", None, [V, P]), ("archive_entry_set_size", None, [V, ctypes.c_int64]),
                          ("archive_entry_set_filetype", None, [V, ctypes.c_uint]), ("archive_entry_set_perm", None, [V, ctypes.c_uint]),
                          ("archive_entry_set_mtime", None, [V, ctypes.c_long, ctypes.c_long]),
                          ("archive_error_string", P, [V])):
        f = getattr(L, fn); f.restype = res; f.argtypes = args
    _LA.append((L, ctypes))
    return _LA[0]


def read_7z(data):
    """[(name, bytes or None for a directory, mtime)]; raises on encrypted or unreadable archives."""
    L, C = _la()
    a = L.archive_read_new(); L.archive_read_support_format_7zip(a); L.archive_read_support_filter_all(a)
    src = C.create_string_buffer(data, len(data)); out = []
    try:
        if L.archive_read_open_memory(a, src, len(data)) != 0:
            raise ValueError("open: %s" % L.archive_error_string(a))
        ent = C.c_void_p(); chunk = C.create_string_buffer(1 << 20)
        while True:
            r = L.archive_read_next_header(a, C.byref(ent))
            if r == 1:
                break
            if r < -20:
                raise ValueError("header: %s" % L.archive_error_string(a))
            if L.archive_entry_is_encrypted(ent):
                raise PermissionError("encrypted member")
            raw = L.archive_entry_pathname_utf8(ent) or L.archive_entry_pathname(ent) or b""
            name = raw.decode("utf-8", "surrogateescape"); ft = L.archive_entry_filetype(ent); mt = L.archive_entry_mtime(ent)
            if ft == 0o040000:
                out.append((name.rstrip("/"), None, mt)); continue
            parts = []; total = 0
            while True:
                k = L.archive_read_data(a, chunk, 1 << 20)
                if k < 0:
                    raise ValueError("data: %s" % L.archive_error_string(a))
                if k == 0:
                    break
                parts.append(chunk.raw[:k]); total += k
                if total > MAX_MEMBER:
                    raise ValueError("member too large")
            out.append((name, b"".join(parts), mt))
    finally:
        L.archive_read_free(a)
    return out


def write_7z(members, path):
    L, C = _la()
    w = L.archive_write_new(); L.archive_write_set_format_7zip(w)
    L.archive_write_set_format_option(w, b"7zip", b"compression", b"lzma2")
    try:
        if L.archive_write_open_filename(w, path.encode()) != 0:
            raise ValueError("write open")
        for name, data, mt in members:
            e = L.archive_entry_new()
            L.archive_entry_set_pathname_utf8(e, name.encode("utf-8", "surrogateescape"))
            L.archive_entry_set_filetype(e, 0o040000 if data is None else 0o100000)
            L.archive_entry_set_perm(e, 0o755 if data is None else 0o644); L.archive_entry_set_mtime(e, mt, 0)
            if data is not None:
                L.archive_entry_set_size(e, len(data))
            if L.archive_write_header(w, e) != 0:
                raise ValueError("write header: %s" % L.archive_error_string(w))
            if data:
                if L.archive_write_data(w, data, len(data)) != len(data):
                    raise ValueError("write data")
            L.archive_entry_free(e)
        L.archive_write_close(w)
    finally:
        L.archive_write_free(w)


def rewrite_7z(data, recurse, rename):
    try:
        mem = read_7z(data)
    except NotImplementedError as ex:
        return None, 0, ["KEPT 7z: %s" % ex]
    except Exception as ex:
        return None, 0, ["KEPT 7z: header or content not readable (%s), archive copied" % type(ex).__name__]
    n = 0; new = []
    for name, b, mt in mem:
        nm = rename(name)
        if len(nm) != len(name):
            nm = name
        nb = None if b is None else recurse(b, name)
        n += (nm != name) + (nb is not None)
        new.append((nm, b if nb is None else nb, mt))
    if not n:
        return None, 0, []
    tmp = tempfile.mkdtemp(prefix="deid_7z_")
    try:
        p = os.path.join(tmp, "out.7z"); write_7z(new, p); out = open(p, "rb").read()
        back = read_7z(out)                                     # gate 1: libarchive re-read, names and bytes
        if [(x[0], x[1]) for x in back] != [(x[0], x[1]) for x in new]:
            return None, 0, ["KEPT 7z: re-read differs, original kept"]
        if os.path.exists(SEVENZIP):                            # gate 2: an independent reader (7-Zip)
            r = subprocess.run([SEVENZIP, "t", p], capture_output=True, timeout=600)
            if r.returncode != 0:
                return None, 0, ["KEPT 7z: second reader refused the rewrite, original kept"]
        return out, n, []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ contract wrappers
def _funcs(ctx, outer_ext):
    sub = lambda t: ctx.mapping.sub_text(t, ctx.source)
    fld = lambda v: ctx.mapping.sub_field(v, ctx.source)
    base = ctx.old_path

    def rec(b, member):
        r = ctx.recurse(b, base + "!/" + member)
        if r.status in ("failed", "partial", "skipped"):
            ctx.notes.extend("member %s: %s" % (ctx.rename(member), x) for x in r.notes[:3])
        return r.data
    return sub, fld, rec


def _wrap(kind):
    def handle(data, ctx):
        try:
            sub, fld, rec = _funcs(ctx, kind)
            own = ctx.old_path.rsplit("/", 1)[-1]
            if kind == "zip":
                pl = ctx.old_path.lower()
                cad = any(part.rstrip("/").endswith(CAD_ZIP) for part in pl.split("!/"))   # nested in a CAD container too
                out, n, notes = rewrite_zip(data, rec, ctx.rename, sub, fld, member_names=not cad)
            elif kind == "tar":
                out, n, notes = rewrite_tar(data, rec, ctx.rename, sub, fld)
            elif kind == "gz":
                out, n, notes = rewrite_gzip(data, rec, ctx.rename, sub, own[:-3] if own.lower().endswith(".gz") else own[:-4] + ".tar" if own.lower().endswith(".tgz") else own)
            elif kind == "z":
                out, n, notes = rewrite_z(data, rec, own[:-2] if own.lower().endswith(".z") else own)
            elif kind == "7z":
                out, n, notes = rewrite_7z(data, rec, ctx.rename)
            else:
                out, n, notes = rewrite_rar(data, rec, ctx.rename)
            notes = notes + [x for x in ctx.notes if x not in notes]
            st = "partial" if any(x.startswith(("KEPT", "LIMIT")) for x in notes) else ("ok" if n else "unchanged")
            return Result(out, n, st, notes)
        except Exception as e:
            return Result(None, status="failed", notes=["%s rewrite error %s" % (kind, type(e).__name__)])
    return handle


register("zip", lambda p, h: h[:4] in (b"PK\x03\x04", b"PK\x05\x06"), priority=70)(_wrap("zip"))
register("tar", lambda p, h: h[257:262] == b"ustar" or (p.endswith(".tar") and len(h) >= 512), priority=69)(_wrap("tar"))
register("gzip", lambda p, h: h[:2] == b"\x1f\x8b", priority=70)(_wrap("gz"))
register("lzw", lambda p, h: h[:2] == b"\x1f\x9d", priority=70)(_wrap("z"))
register("rar", lambda p, h: h[:7] == b"Rar!\x1a\x07\x01", priority=70)(_wrap("rar"))
register("7z", lambda p, h: h[:6] == b"7z\xbc\xaf\x27\x1c", priority=70)(_wrap("7z"))
