"""DWG R13-R2000 layout (AC1012, AC1014, AC1015): uncompressed, same-length in-place edit. Owner: NATIVE builder. MIT.

Written from the ODA Open Design Specification section 3 (file header with section locators, header variables and
classes sections between sentinels with a CRC-16, object map, objects with a CRC-16). No LibreDWG code.
Edits are kept only inside objects found through the object map, the header-variables section and the classes
section; their CRC-16s (seed 0xC0C1) are recomputed. Any other changed byte is put back.
The object-map details that the spec leaves open (does the section size count its own 2 bytes; does the running
location restart in each map section) are chosen per file by which reading gives objects with valid CRCs.
"""
import struct
import numpy as np
from . import dwg_r2004 as D
from . import dwg_patch as P

VERS = (b"AC1012", b"AC1014", b"AC1015")


def _mc(d, p):
    v = sh = 0
    while True:
        b = d[p]
        p += 1
        if b & 0x80:
            v |= (b & 0x7F) << sh
            sh += 7
        else:
            v |= (b & 0x3F) << sh
            return (-v if b & 0x40 else v), p


def locators(d):
    n = struct.unpack_from("<I", d, 0x15)[0]
    if not 3 <= n <= 8:
        raise ValueError("section locator count %d" % n)
    return {d[0x19 + 9 * i]: struct.unpack_from("<II", d, 0x1A + 9 * i) for i in range(n)}


def _obj_ok(d, a):
    try:
        size, n = D._ms(d, a)
        e = a + n + size
        if a < 0 or e + 2 > len(d):
            return None
        return e if D.crc16(d[a:e]) == d[e] | (d[e + 1] << 8) else None
    except IndexError:
        return None


def _map(d, seek, size, incl, reset):
    locs = []
    p, end, last = seek, seek + size, 0
    while p + 2 <= end:
        sz = (d[p] << 8) | d[p + 1]
        if sz <= 2:
            break
        q = p + 2
        e = p + sz if incl else p + 2 + sz
        if reset:
            last = 0
        while q < e:
            _, q = _mc(d, q)
            dl, q = _mc(d, q)
            last += dl
            locs.append(last)
        p = e + 2
    return locs


def objects(d, recs):
    """[(start, crc position)] of every object in the map whose CRC is valid, and the count of invalid ones."""
    seek, size = recs[2]
    best = (-1, [])
    for incl in (True, False):
        for reset in (False, True):
            try:
                locs = _map(d, seek, size, incl, reset)
            except IndexError:
                continue
            ok = sum(_obj_ok(d, a) is not None for a in locs[:60])
            if ok > best[0]:
                best = (ok, locs)
    objs, bad = [], []
    for a in sorted(set(best[1])):
        e = _obj_ok(d, a)
        if e is None:
            bad.append(a)
        else:
            objs.append((a, e))
    return objs, bad


def regions(d):
    recs = locators(d)
    objs, bad = objects(d, recs)
    reg = [(a, e + 2, "obj", e) for a, e in objs]
    for k in (0, 1):
        if k in recs:
            seek, size = recs[k]
            pos = D.sentinel_crc_pos(d[seek:seek + size])
            if pos is not None:
                reg.append((seek, seek + pos + 2, "sent", seek + pos))
    reg.sort()
    return reg, len(objs), set(bad)


def audit(d):
    reg, n, bad = regions(d)
    return dict(objects=n, obj_bad=len(bad), sent=sum(r[2] == "sent" for r in reg))


def patch(data, ed):
    st = dict(notes=[])
    reg, n0, bad0 = regions(data)
    if n0 == 0:
        return None, dict(st, failed="object map not readable")
    P.harvest(ed, data)
    st["_trig"] = P.Triggers(ed)
    cur = bytearray(P.edit_buffer(ed, data, st))
    if bytes(cur) == data:
        return None, st
    ch = np.nonzero(np.frombuffer(data, np.uint8) != np.frombuffer(bytes(cur), np.uint8))[0]
    starts = np.array([r[0] for r in reg], np.int64)
    ends = np.array([r[1] for r in reg], np.int64)
    j = np.searchsorted(starts, ch, side="right") - 1
    inside = (j >= 0) & (ch < ends[np.clip(j, 0, len(ends) - 1)])
    # a change inside a CRC field itself is not an edit we want
    if (~inside).any():
        st["dropped_outside_objects"] = int((~inside).sum())
        for x in ch[~inside].tolist():
            cur[x] = data[x]
    for jj in sorted(set(j[inside].tolist())):
        a, b, kind, crcpos = reg[jj]
        lo = a if kind == "obj" else a + 16
        c = D.crc16(cur[lo:crcpos])
        cur[crcpos] = c & 0xFF
        cur[crcpos + 1] = c >> 8
        st["crc_fixed"] = st.get("crc_fixed", 0) + 1
    out = bytes(cur)
    if out == data:
        return None, st
    reg1, n1, bad1 = regions(out)
    st["audit"] = dict(objects=n1, obj_bad=bad1)
    st["audit_failures"] = (len(bad0), len(bad1))
    # "no new failures": every object CRC failure in the output is one the original has at the same place
    if not bad1 <= bad0 or n1 < n0 or sum(r[2] == "sent" for r in reg1) < sum(r[2] == "sent" for r in reg):
        return None, dict(st, failed="new audit failures: objects %d/%d, bad %d/%d" % (n1, n0, len(bad1), len(bad0)))
    return out, st
