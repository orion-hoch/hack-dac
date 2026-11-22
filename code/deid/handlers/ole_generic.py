"""Solid Edge (.par .psm .asm .dft .pwd) and old NX (.prt) in OLE containers. Owner: NATIVE builder.
Same pass as Inventor (deid.native.olejob): raw strings (UTF-16 and ASCII 8.3 paths), zlib blocks refitted at exact
size, person properties, free space zeroed. Matched only when the file is really OLE (Creo .prt/.asm and assembler
source .asm are not)."""
from deid.api import register, Result
from deid.native.ole import MAGIC
from deid.native import olejob
from deid.native.plain import make_transform

EXT = (".par", ".psm", ".asm", ".dft", ".pwd", ".prt")


@register("ole_generic", lambda p, h: p.endswith(EXT) and h[:8] == MAGIC, priority=55)
def handle(data, ctx):
    try:
        return olejob.process(data, ctx, "ole", drawing=ctx.old_path.lower().endswith(".dft"))
    except Exception as e:
        return Result(None, status="failed", notes=["ole: %s: %s" % (type(e).__name__, str(e)[:100])])


transform = make_transform(handle)
