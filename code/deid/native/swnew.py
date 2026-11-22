"""SolidWorks 2015+ container (obfuscated zip). Owner: NATIVE builder. Parser from the team's prior/native/nat.py.

file   : 4-byte per-save key | 00 00 00 04 | records ... | central directory | end record
local  : sig(4) | 14 00 06 00 08 00 | tag(4) | crc32 | csize | usize | namelen(4) | name (nibble-swapped) | raw deflate
central: sig(4) | 00 00 | 14 00 06 00 08 00 | tag | crc32 | csize | usize | namelen(4) | 10 x 00 | offset(4) | name
A record is real iff its payload inflates to usize bytes with the stored CRC-32 (decoys fail). Trailing bytes after the
end of the deflate stream inside csize (our NUL padding) are allowed, as in swformat's accepted form.
"""
import struct
import zlib
import binascii

MARK = b"\x14\x00\x06\x00\x08\x00"


def swap(b):
    return bytes(((x << 4) & 0xF0) | (x >> 4) for x in b)


def is_sw_new(d):
    return d[4:8] == b"\x00\x00\x00\x04" and MARK in d[:64]


def _marks(d):
    out, pos = [], 0
    while True:
        i = d.find(MARK, pos)
        if i < 0:
            return out
        out.append(i)
        pos = i + 1


def inflate(raw):
    dz = zlib.decompressobj(-15)
    out = dz.decompress(raw)
    if not dz.eof:
        raise zlib.error("incomplete")
    return out


def sw_locals(d):
    out = []
    for i in _marks(d):
        if i < 4 or i + 26 > len(d):
            continue
        tag, crc, cs, us, nl = struct.unpack_from("<4sIIII", d, i + 6)
        ds = i + 26 + nl
        if nl == 0 or nl > 512 or ds + cs > len(d):
            continue
        try:
            raw = inflate(d[ds:ds + cs]) if cs else b""
        except Exception:
            continue
        if len(raw) != us or (binascii.crc32(raw) & 0xFFFFFFFF) != crc:
            continue
        if us == 0 and d[i + 26:i + 36] == b"\0" * 10:
            continue
        out.append(dict(name=swap(d[i + 26:ds]).decode("latin1"), crc=crc, csize=cs, usize=us, hdr=i - 4, data=ds,
                        nl=nl, crc_at=i + 10, plain=raw))
    return out


def sw_central(d, locs):
    by = {r["hdr"]: r for r in locs}
    out = []
    for i in _marks(d):
        if d[i - 2:i] != b"\0\0" or i + 40 > len(d):
            continue
        tag, crc, cs, us, nl = struct.unpack_from("<4sIIII", d, i + 6)
        off = struct.unpack_from("<I", d, i + 36)[0]
        r = by.get(off + 8)
        if nl == 0 or nl > 512 or r is None or r["crc"] != crc or r["nl"] != nl:
            continue
        out.append(dict(name=swap(d[i + 40:i + 40 + nl]).decode("latin1"), hdr=i - 6, local=off + 8, crc_at=i + 10,
                        csize=cs, usize=us))
    return out
