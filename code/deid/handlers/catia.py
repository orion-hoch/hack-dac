"""CATIA V5 (.CATPart .CATProduct .CATDrawing, `V5_CFV2` container). Owner: NATIVE2 builder.

Survey (all 335 corpus files): no user, host or folder path inside; the only name is the file's own part name (ASCII,
136 files). So this handler does two things only:
 1. when the path plan renames the file (3 of 335 under the final table: the old stem -> same-length stand-in), the old stem
    is replaced by the new one inside, as a whole ASCII token inside readable runs (same length);
 2. .CATDrawing: the small JPEG preview (130x110) becomes a flat tile of the same pixel size and byte length (D6).
It deliberately does NOT run the identity mapping over the file: with the final table, textedit/mapping would turn
the CATIA keyword "CATStdMechanicalDesign" into "CATStdTidgukaduvBiwaqk" in 85 of 335 files.
Gate: same length; `V5_CFV2` head and the last 64 bytes (`CB__END` trailer) unchanged; changed bytes only in the
own-name tokens and the JPEG span.
"""
import os
import re

from deid.api import register, Result
from deid.native import preview_small
from deid.native.plain import make_transform

EXT = (".catpart", ".catproduct", ".catdrawing")


def process(data, ctx):
    data = bytes(data)
    if not data.startswith(b"V5_CFV2") or b"CB__END" not in data[-64:]:
        return Result(None, status="failed", notes=["catia: not a V5 container"])
    out, notes, k = data, [], 0
    a = os.path.splitext(os.path.basename(ctx.old_path.split("!/")[-1]))[0]
    b = os.path.splitext(os.path.basename((ctx.new_path or ctx.old_path).split("!/")[-1]))[0]
    if a != b:
        try:
            ae, be = a.encode("latin-1"), b.encode("latin-1")
        except UnicodeEncodeError:
            ae = be = None
        if ae and len(ae) == len(be):
            rx = re.compile(rb"(?<![A-Za-z0-9_])" + re.escape(ae) + rb"(?![A-Za-z0-9_])")
            out, n = rx.subn(be, out)
            k += n
            notes.append("catia: own name followed the path plan (%d)" % n)
        else:
            notes.append("catia: renamed stem has another length; inside name kept")
    if ctx.old_path.lower().endswith(".catdrawing"):
        x, nt = preview_small.catia_drawing(out)
        notes += nt
        if x is not None:
            out, k = x, k + 1
    if out == data:
        return Result(None, status="unchanged", notes=notes)
    if len(out) != len(data) or out[:16] != data[:16] or out[-64:] != data[-64:]:
        return Result(None, status="failed", notes=notes + ["catia: container check failed"])
    return Result(out, changed=k, status="ok", notes=notes)


@register("catia", lambda p, h: p.endswith(EXT) and h[:7] == b"V5_CFV2", priority=55)
def handle(data, ctx):
    try:
        return process(data, ctx)
    except Exception as e:
        return Result(None, status="failed", notes=["catia: %s: %s" % (type(e).__name__, str(e)[:120])])


transform = make_transform(handle)
