"""do_creo from for_andre/dropins.py (comparison agent), extracted so the shipped code carries only what runs.
Section-aware Creo rewrite: LZW sections decoded, both TOC checksums rebuilt, verified before return."""


def do_creo(engine, data):
    from . import creo
    info = creo.parse(data)
    edits, n = {}, 0
    for s in info['secs']:
        b = creo.body(data, s)
        if b[:2] == b'\x1f\x9d':
            try:
                u, _ = creo.unlzw(b)
            except Exception:
                continue                      # 23% of LZW-magic sections do not decode: leave them
            if len(u) != s['unp']:
                continue
        else:
            u = b
        new, k = engine.sub_bytes(u, same_len=True)
        if k:
            edits[s['name']] = new; n += k
    head = data[:info['base']]
    nh, k = engine.sub_bytes(head, same_len=True)
    n += k
    if not n:
        return data, 0
    out = creo.rebuild(data, edits, [(head, nh)] if k else None)
    secs, ok, nl, lok, bad = creo.verify(out)
    s0, ok0, _, _, _ = creo.verify(data)
    if secs != s0 or ok < ok0:
        raise ValueError('creo rebuild failed verification: %r' % bad[:3])
    return out, n
