"""Creo / Pro-E handler (.prt.N .asm.N .drw.N and unversioned Creo .prt/.asm). Owner: NATIVE builder.

- Text before the banner (header, #P_OBJECT): same-length edit in place.
- Plain sections: same-length edit in place, both TOC checksums recomputed.
- LZW sections that decode to their TOC size: edited, recompressed (size changes), all offsets rebuilt.
- LZW-magic sections that do not decode (TOC type 'c', another coder): left byte-identical and counted.
Matched by content: '#UGC:2' (the TOC container), whatever the extension or version number. '#UGC:1 NEUTRAL' (.neu)
files are plain text without a TOC and are left to the text handler.
Every output is re-parsed: every section's checksums must match and every LZW section must decode to its TOC size,
and untouched sections must be byte-identical; otherwise the file is reported failed and the input is kept.
"""
import os
import re
from deid.api import register, Result
from deid.native import creofmt as F
from deid.native.textedit import Editor, NAME_RX, views
from deid.native.plain import make_transform

CREO_FIELDS = [
    re.compile(r"user_name\x00([^\x00\x01]{1,64})\x00"),
    re.compile(r"\xf6\xe1([A-Za-z][A-Za-z0-9._ -]{0,63})\x00\xe2"),
]


def _stem(name):
    b = os.path.basename(name)
    b = re.sub(r"\.\d+$", "", b)
    return b.rsplit(".", 1)[0] if "." in b else b


@register("creo", lambda path, head: head[:6] == b"#UGC:2", priority=60)
def handle(data, ctx):
    try:
        return _handle(data, ctx)
    except Exception as e:
        return Result(None, status="failed", notes=["creo: %s: %s" % (type(e).__name__, str(e)[:100])])


def _handle(d, ctx):
    info = F.parse(d)
    base = info["base"]
    head = d[:base]
    E = Editor(ctx, extra_fields=CREO_FIELDS)
    secs, opaque, bad_ck = [], 0, 0
    for s in info["secs"]:
        b = F.body(d, s)
        if F.sums(b) != (s["ck1"], s["ck2"]):
            bad_ck += 1
        if b[:2] == b"\x1f\x9d":
            try:
                u, _ = F.unlzw(b)
                ok = len(u) == s["unp"]
            except Exception:
                ok = False
            if not ok:
                opaque += 1
                secs.append((s, None, True))
                continue
            secs.append((s, u, True))
        else:
            secs.append((s, b, False))
    blobs = [head] + [u for _, u, _ in secs if u is not None]
    for b in blobs:
        E.harvest(b)
    # path plan: own name and every referenced model name, also as bare stems
    own = os.path.basename(ctx.old_path)
    for nm in [re.sub(r"\.\d+$", "", own)] + sorted({m.group(1) for b in blobs for t, _, _ in views(b)
                                                       for m in NAME_RX.finditer(t)}):
        new = E.rename(nm)
        if new != nm:
            E.add_stem(_stem(nm), _stem(new))
    new_head, _ = E.edit(head)
    edits = {}
    for s, u, lz in secs:
        if u is None:
            continue
        nu, _ = E.edit(u)
        if nu != u:
            edits[s["off"]] = (nu, lz)
    notes = list(dict.fromkeys(E.notes))
    if opaque:
        notes.append("creo: %d LZW-magic sections do not decode; left byte-identical" % opaque)
    if bad_ck:
        notes.append("creo: %d sections had wrong checksums in the input" % bad_ck)
    if new_head == head and not edits:
        return Result(None, status="unchanged", notes=notes)
    d2 = new_head + d[base:]
    out = F.rebuild(d2, edits)
    # re-parse and check
    n, ok, lzok, lz = F.check(out)
    info2 = F.parse(out)
    if n != len(info["secs"]) or ok < n - bad_ck or lzok < lz - opaque or out[:base] != new_head:
        return Result(None, status="failed", notes=notes + ["creo: rebuilt file failed its check (%d/%d ck, %d/%d lzw)"
                                                            % (ok, n, lzok, lz)])
    old_by = {s["toc_entry"]: s for s in info["secs"]}
    for s2, s in zip(sorted(info2["secs"], key=lambda x: x["toc_entry"]), sorted(info["secs"], key=lambda x: x["toc_entry"])):
        if s["off"] not in edits and F.body(out, s2) != F.body(d, s):
            return Result(None, status="failed", notes=notes + ["creo: untouched section changed: " + s["name"]])
        if s["off"] in edits and edits[s["off"]][1]:
            if F.unlzw(F.body(out, s2))[0] != edits[s["off"]][0]:
                return Result(None, status="failed", notes=notes + ["creo: LZW round trip differs: " + s["name"]])
    return Result(out, changed=E.count, status="ok", notes=notes)


transform = make_transform(handle)
