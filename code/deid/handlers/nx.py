"""Newer Siemens NX parts (.prt, `SPLMSSTR` container, version byte 6). Owner: NATIVE2 builder.

Layout seen in all 97 corpus files: "SPLMSSTR", version byte, u64 at 9, u64 directory offset at 17 (directory =
last 150+ bytes: /Root/... names with u64 offset and size), then a 4-byte trailer that differs per file.
The trailer's meaning is UNKNOWN: not CRC-32 in 6 polynomials x reflected/normal x 2 inits x 2 final XORs, not
Adler-32, byte or word sums, nor the first or last 4 bytes of MD5/SHA-1/SHA-256, over 13 start x 4 end offsets.
If it is a content hash we cannot compute, any edit is detectable by NX.

Edit (mode "patched"): textedit.Editor over the raw file: field slots (folder after Users, UNC host) and every other
change only inside runs of >= 8 readable characters (council D9: no matcher over undecoded binary); same length; the
first 26 bytes, the directory and the trailer are never changed (trailer left as it was). zlib members inside are
NOT decoded (not done; names inside them stay).
Mode "unchanged": the file goes out as it is. Choose with DEID_NX_MODE=patched|unchanged (default patched), or call
transform_patched / transform_unchanged directly.
Gate: same length; header, directory and trailer byte-identical; every changed byte lies in a readable run of the
ORIGINAL or in a field slot.
"""
import os
import struct

import numpy as np

from deid.api import register, Result
from deid.native.textedit import Editor, readable
from deid.native.plain import make_transform

MAGIC = b"SPLMSSTR"


def _dir_off(data):
    if not data.startswith(MAGIC) or len(data) < 64:
        raise ValueError("not SPLMSSTR")
    d = struct.unpack_from("<Q", data, 17)[0]
    if not 26 <= d < len(data) - 4:
        raise ValueError("directory offset out of range")
    return d


def patched(data, ctx):
    data = bytes(data)
    d = _dir_off(data)
    ed = Editor(ctx)
    ed.harvest(data)
    from deid.handlers.jt import _own_stem
    _own_stem(ed, ctx)
    body = data[26:d]
    nb, k = ed.edit(body)
    if nb == body:
        return Result(None, status="unchanged", notes=ed.notes[:3])
    out = data[:26] + nb + data[d:]
    # gate
    if len(out) != len(data) or out[:26] != data[:26] or out[d:] != data[d:]:
        raise ValueError("gate: header, directory or trailer changed")
    a, b = np.frombuffer(data, np.uint8), np.frombuffer(out, np.uint8)
    diff = np.nonzero(a != b)[0]
    ok = readable(a)
    u16 = np.zeros(len(a), bool)                         # readable UTF-16LE runs (either alignment)
    for al in (0, 1):
        lo, hi = a[al::2], a[al + 1::2]
        m = min(len(lo), len(hi))
        v = np.where(hi[:m] == 0, lo[:m], 1).astype(np.uint8)
        r = readable(v)
        u16[al:al + 2 * m:2] |= r
    bad = int((~(ok | u16)[diff]).sum())
    if bad:
        raise ValueError("gate: %d changed bytes outside readable runs" % bad)
    return Result(out, changed=k, status="ok",
                  notes=["nx: same-length patch in readable runs; 4-byte trailer UNKNOWN, left as it was; "
                         "zlib members not decoded"] + ed.notes[:3])


def _mode():
    return os.environ.get("DEID_NX_MODE", "patched")


@register("nx_splm", lambda p, h: p.endswith((".prt",)) and h[:8] == MAGIC, priority=56)
def handle(data, ctx):
    if _mode() == "unchanged":
        return Result(None, status="unchanged", notes=["nx: DEID_NX_MODE=unchanged (trailer meaning unknown)"])
    try:
        return patched(data, ctx)
    except Exception as e:
        return Result(None, status="failed", notes=["nx: %s: %s" % (type(e).__name__, str(e)[:120])])


def _handle_patched(data, ctx):
    try:
        return patched(data, ctx)
    except Exception as e:
        return Result(None, status="failed", notes=["nx: %s: %s" % (type(e).__name__, str(e)[:120])])


transform = make_transform(handle)
transform_patched = make_transform(_handle_patched)


def transform_unchanged(data, old_path, mapping=None, source=None, rename=None):
    return data
