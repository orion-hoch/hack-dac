"""Inventor handler (.ipt .iam .idw .ipn .ide): OLE pass from deid.native.olejob. Owner: NATIVE builder.
Property sets (author, last saved by, company, designer), raw strings in streams (UFRxDoc, RSeDb paths), zlib
segments at any offset inside B/M streams refitted to their exact size, free sectors and slack zeroed.
Previews (180x180) are left, as decided for part, assembly and small drawing previews (D6)."""
from deid.api import register, Result
from deid.native.ole import MAGIC
from deid.native import olejob
from deid.native.plain import make_transform

EXT = (".ipt", ".iam", ".idw", ".ipn", ".ide")


@register("inventor", lambda p, h: p.endswith(EXT) and h[:8] == MAGIC, priority=60)
def handle(data, ctx):
    try:
        return olejob.process(data, ctx, "inventor", drawing=ctx.old_path.lower().endswith(".idw"))
    except Exception as e:
        return Result(None, status="failed", notes=["inventor: %s: %s" % (type(e).__name__, str(e)[:100])])


transform = make_transform(handle)
