"""SolidWorks handler: 2015+ container and the old OLE container. Owner: NATIVE builder.

2015+: every chunk is inflated, edited same-length (Editor), recompressed with the cheapest encoder that fits (zlib 6,
zlib 9, libdeflate 12, zopfli) and padded with trailing NUL bytes to its original compressed size at its original
offset (swformat's accepted form). Size fields are not touched (same length). The CRC-32 is rewritten in the local
AND the central record (SolidWorks itself writes correct CRCs in all 1,995 inspected files; swformat leaves them
stale; council D5). A chunk that cannot fit: insignificant XML whitespace between tags is trimmed (the uncompressed
size field changes, as in swformat's span-preserving write; the span does not); if it still does not fit, that chunk
is left and the file is "partial"; if that chunk holds a renamed file name, the whole file is left unchanged.
Drawings (.slddrw): PreviewPNG, Images/Sheet_N and Preview become neutral pictures of the same size and length.
OLE (2014 and older): deid.native.olejob (streams, zlib blobs at any offset, person properties, free-space zeroing).
Matched by content: any file with the 2015+ container signature (templates .prtdot/.asmdot/.drwdot/.slddrt/.sldlfp
included), OLE files with a SolidWorks extension. Lock files (~$name) and text files are not matched here.
"""
import os
import struct
import zlib
from collections import Counter
from deid.api import register, Result
from deid.native import swnew
from deid.native.ole import MAGIC
from deid.native.textedit import Editor
from deid.native.zfit import fit_raw_nul, ZLIB_ONLY
import re
from deid.native.preview import neutral
from deid.native.plain import make_transform
from deid.native import olejob

SW_EXT = (".sldprt", ".sldasm", ".slddrw", ".prtdot", ".asmdot", ".drwdot", ".slddrt", ".sldlfp", ".sldblk")
DRW_EXT = (".slddrw", ".drwdot", ".slddrt")
_WS = re.compile(rb">[ \t\r\n]+<")


@register("sw", lambda p, h: swnew.is_sw_new(h) or (p.endswith(SW_EXT) and h[:8] == MAGIC), priority=60)
def handle(data, ctx):
    try:
        if data[:8] == MAGIC:
            drw = ctx.old_path.lower().endswith(DRW_EXT)
            return olejob.process(data, ctx, "sw-ole", preview=(lambda name, s: neutral(name, s)) if drw else None,
                                  drawing=drw)
        return _new(data, ctx)
    except Exception as e:
        return Result(None, status="failed", notes=["sw: %s: %s" % (type(e).__name__, str(e)[:100])])


def _new(d, ctx):
    L = swnew.sw_locals(d)
    if not L:
        return Result(None, status="failed", notes=["sw: no chunk inflates with its CRC"])
    C = swnew.sw_central(d, L)
    cen = {c["local"]: c for c in C}
    drw = ctx.old_path.lower().endswith(DRW_EXT) or any(r["name"].startswith("Images/Sheet_") for r in L)
    E = Editor(ctx)
    for r in L:
        E.harvest(r["plain"])
    own = os.path.basename(ctx.old_path)
    E.rename(own)
    out = bytearray(d)
    st = Counter()
    notes = []
    for r in L:
        p = r["plain"]
        np_ = None
        if drw:
            np_ = neutral(r["name"], p)
            if np_ is not None:
                st["previews"] += 1
        rh = E.rename_hits
        if np_ is None:
            np_, k = E.edit(p)
        if np_ == p:
            continue
        st["chunks_edited"] += 1
        fitted, how = fit_raw_nul(np_, r["csize"])
        if fitted is None and p.lstrip()[:1] == b"<":
            # council D5: trim insignificant XML whitespace between tags; usize changes, the span does not
            np2 = _WS.sub(b"><", np_)
            fitted, how2 = fit_raw_nul(np2, r["csize"])
            if fitted is not None:
                np_, how = np2, how2 + "+xmltrim"
                st["xml_trimmed"] += 1
        if fitted is None:
            if E.rename_hits > rh:
                return Result(None, status="partial", notes=["sw: chunk %s holds a renamed file name and does not fit"
                                                             " (%s); whole file left unchanged" % (r["name"][:40], how)])
            st["nofit"] += 1
            notes.append("sw: chunk %s does not fit (%s); left unedited" % (r["name"][:40], how))
            continue
        st["fit_" + how.split("+")[0]] += 1
        if how.split("+")[0] not in ZLIB_ONLY:
            st["needs_strong"] += 1
        out[r["data"]:r["data"] + r["csize"]] = fitted
        crc = struct.pack("<I", zlib.crc32(np_) & 0xFFFFFFFF)
        us = struct.pack("<I", len(np_))
        out[r["crc_at"]:r["crc_at"] + 4] = crc
        out[r["crc_at"] + 8:r["crc_at"] + 12] = us
        if r["hdr"] in cen:
            c = cen[r["hdr"]]
            out[c["crc_at"]:c["crc_at"] + 4] = crc
            out[c["crc_at"] + 8:c["crc_at"] + 12] = us
        else:
            st["no_central"] += 1
    if out == d:
        return Result(None, status="unchanged", notes=list(dict.fromkeys(E.notes)))
    out = bytes(out)
    # re-parse: same records at the same offsets and sizes, every chunk inflates with its (new) CRC
    L2 = swnew.sw_locals(out)
    if [(r["hdr"], r["data"], r["csize"], r["name"]) for r in L2] != \
            [(r["hdr"], r["data"], r["csize"], r["name"]) for r in L]:
        return Result(None, status="failed", notes=["sw: re-parse of output differs"])
    if len(swnew.sw_central(out, L2)) != len(C):
        return Result(None, status="failed", notes=["sw: central directory no longer matches"])
    notes = list(dict.fromkeys(E.notes + notes))
    notes.append("sw-new stats: " + ",".join("%s=%d" % kv for kv in sorted(st.items())))
    return Result(out, changed=E.count + st["previews"], status="partial" if st["nofit"] else "ok", notes=notes)


transform = make_transform(handle)
