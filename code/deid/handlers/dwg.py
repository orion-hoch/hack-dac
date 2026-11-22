"""DWG handler. Owner: NATIVE builder. MIT.

R2004 layout (AC1018, AC1024, AC1027, AC1032): decode each page, edit the decoded sections (all 8 bit alignments,
guarded), write back through literal provenance, or re-encode a page to its exact compressed size when a back-reference
reuses a patched literal; then fix object CRC-16s, header/classes CRC-16s and the two page checksums.
R13-R2000 (AC1012, AC1014, AC1015): uncompressed; edit objects, header and classes sections, fix their CRC-16s.
AC1021 (R2007, Reed-Solomon pages) and pre-R13 files: not handled ("failed", input copied).
Every output passes an end-to-end self-check (sections decode to exactly what was planned; all page checksums and all
CRCs valid) or the original is kept with status "partial" (ODA refuses files with stale CRCs or checksums).
"""
from ..api import register, Result
from ..native import dwg_r2004 as D, dwg_patch as P, dwg_r2000 as R0
from ..native.textedit import Editor
from ..native.plain import make_transform
from ..native import dwg_gate as G


def run(data, ctx):
    """-> (Result, Editor). The Editor carries the identity values harvested from this file (ed.vals)."""
    ed = Editor(ctx)
    try:
        ver = bytes(data[:6])
        if ver in D.VERS:
            out, st = P.patch_r2004(data, ed)
        elif ver in R0.VERS:
            out, st = R0.patch(data, ed)
        elif ver == b"AC1021":
            return Result(None, status="failed", notes=["dwg AC1021 (R2007 Reed-Solomon layout) not supported"]), ed
        else:
            return Result(None, status="failed", notes=["dwg version %r not supported" % ver.decode("latin-1")]), ed
    except Exception as e:
        return Result(None, status="failed", notes=["dwg: %s %s" % (type(e).__name__, str(e)[:120])]), ed
    notes = list(st.get("notes", [])) + ed.notes[:5]
    if st.get("rejected"):
        notes.append("dwg: %d short unconfirmed matches in binary data left unchanged" % st["rejected"])
    if st.get("failed"):
        return Result(None, status="partial", notes=notes + ["dwg: patch withheld, original kept: " + st["failed"]]), ed
    if st.get("in_container"):
        notes.append("dwg: %d identity bytes inside embedded containers (zip/OLE/image) left; not recursed" % st["in_container"])
    partial = st.get("refused_sections") or st.get("dropped_outside_objects") or st.get("shift_conflict") \
        or st.get("in_container")
    if st.get("dropped_outside_objects"):
        notes.append("dwg: %d changed bytes outside objects/CRC-covered sections put back" % st["dropped_outside_objects"])
    if out is None:
        return Result(None, status="partial" if partial else "unchanged", notes=notes), ed
    n = st.get("accepted", 0)
    for k in ("pages_reencoded", "crc_fixed"):
        if st.get(k):
            notes.append("dwg: %s %d" % (k, st[k]))
    return Result(out, changed=n, status="partial" if partial else "ok", notes=notes), ed


def _handle_unchecked(data, ctx):
    try:
        return run(data, ctx)[0]
    except Exception as e:  # never raise
        return Result(None, status="failed", notes=["dwg: %s" % type(e).__name__])


@register("dwg", lambda path, head: path.endswith(".dwg") and head[:4] == b"AC10", priority=60)
def handle(data, ctx):
    """The patcher, then the strict gate (council, leg 3): the edit goes out only if the independent LibreDWG reader
    sees nothing but same-length string changes; otherwise, or without the checker, the original goes out."""
    r = _handle_unchecked(data, ctx)
    if r.data is None:
        return r
    try:
        ok, note = G.gate(data, r.data)
    except Exception as e:
        ok, note = False, "gate error %s" % type(e).__name__
    if not ok:
        return Result(None, status="partial", notes=list(r.notes) + ["dwg: strict gate refused the edit, original kept: " + note])
    return Result(r.data, changed=r.changed, status=r.status, notes=list(r.notes) + ["dwg: " + note])


transform = make_transform(handle)
