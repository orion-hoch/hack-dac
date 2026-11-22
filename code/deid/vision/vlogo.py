"""Logos drawn as vector shapes (item 2). Owned by the PDF builder.

A logo in a drawing template is re-emitted in every file of a source as the same sequence of filled curved paths.
page_groups() cuts a page's content into spatial clusters of consecutive filled paths, signs each cluster by its shape
relative to its own bounding box (position-free, 0.5 pt grid), and form XObjects by their decoded content.
A table data/vector_logos.tsv (sig, label) built from recurring clusters, labelled by eye, drives remove_vector_logos().
"""
import hashlib, os, re
import pikepdf
from pikepdf import Name, Operator

from . import pdftext

FILL = {'f', 'F', 'f*', 'B', 'B*', 'b', 'b*'}
PAINT = FILL | {'S', 's', 'n'}
GRID = 0.5


def _events(instrs):
    """Paint events: dict(ii=[instruction indexes], pts, ops, curves, fill, col, bb) in page space."""
    num = pdftext.num; mul = pdftext.mul
    ctm = pdftext.I6; stack = []; cur = []; idx = []; col = None; ev = []; brk = 0

    def T(x, y): return (x * ctm[0] + y * ctm[2] + ctm[4], x * ctm[1] + y * ctm[3] + ctm[5])
    for ii, (ops, op) in enumerate(instrs):
        o = str(op)
        try:
            if o == 'q': stack.append((ctm, col))
            elif o == 'Q':
                if stack: ctm, col = stack.pop()
            elif o == 'cm': ctm = mul([num(x) for x in ops], ctm)
            elif o in ('g', 'rg', 'k'): col = tuple(round(num(x), 2) for x in ops)
            elif o in ('sc', 'scn'): col = tuple(round(num(x), 2) for x in ops if not isinstance(x, Name))
            elif o in ('BT', 'Do', 'BI', 'sh'): brk += 1
            elif o in ('m', 'l'):
                cur.append((o, [T(num(ops[0]), num(ops[1]))])); idx.append(ii)
            elif o in ('c', 'v', 'y'):
                cur.append(('c', [T(num(ops[k]), num(ops[k + 1])) for k in range(0, len(ops) - 1, 2)])); idx.append(ii)
            elif o == 're':
                x, y, w, h = [num(v) for v in ops]
                cur.append(('re', [T(x, y), T(x + w, y + h)])); idx.append(ii)
            elif o == 'h':
                cur.append(('h', [])); idx.append(ii)
            elif o in ('W', 'W*'):
                idx.append(ii)
            elif o in PAINT:
                if cur and o in FILL:
                    pts = [p for _, ps in cur for p in ps]
                    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                    ev.append({'ii': idx + [ii], 'ops': cur, 'curves': sum(1 for k, _ in cur if k == 'c'), 'col': col,
                               'bb': (min(xs), min(ys), max(xs), max(ys)), 'brk': brk, 'paint': o})
                cur = []; idx = []
        except Exception:
            cur = []; idx = []
    return ev


def _sig(evs):
    bx0 = min(e['bb'][0] for e in evs); by0 = min(e['bb'][1] for e in evs)
    h = hashlib.sha1()
    for e in evs:
        h.update(('%s|%s|' % (e['paint'], e['col'])).encode())
        for k, ps in e['ops']:
            h.update(k.encode())
            for x, y in ps:
                h.update(b'%d,%d;' % (round((x - bx0) / GRID), round((y - by0) / GRID)))
    return h.hexdigest()[:20]


def page_groups(instrs, min_curves=8):
    """Spatial clusters of consecutive filled paths -> list of dict(sig, ii, bb, curves, n)."""
    ev = _events(instrs); out = []; cl = []

    def close():
        if cl:
            c = sum(e['curves'] for e in cl)
            if c >= min_curves:
                bb = (min(e['bb'][0] for e in cl), min(e['bb'][1] for e in cl), max(e['bb'][2] for e in cl), max(e['bb'][3] for e in cl))
                if 3 < max(bb[2] - bb[0], bb[3] - bb[1]) < 400:
                    out.append({'sig': _sig(cl), 'ii': sorted({i for e in cl for i in e['ii']}), 'bb': bb, 'curves': c,
                                'n': len(cl), 'evs': cl})
    for e in ev:
        if cl:
            bb = (min(x['bb'][0] for x in cl), min(x['bb'][1] for x in cl), max(x['bb'][2] for x in cl), max(x['bb'][3] for x in cl))
            gap = 0.5 * max(bb[2] - bb[0], bb[3] - bb[1], e['bb'][2] - e['bb'][0], e['bb'][3] - e['bb'][1]) + 2.0
            far = e['bb'][0] > bb[2] + gap or e['bb'][2] < bb[0] - gap or e['bb'][1] > bb[3] + gap or e['bb'][3] < bb[1] - gap
            if far or e['brk'] != cl[-1]['brk']:
                close(); cl = []
        cl.append(e)
    close()
    return out


def form_sigs(pdf, page):
    """Form XObjects drawn by the page that contain curved fills and no text: sig 'form:' + content hash."""
    out = []
    try:
        res = pdftext.inherited(page.obj, '/Resources'); xo = res.get('/XObject') if res is not None else None
        for k in (xo.keys() if xo is not None else []):
            f = xo[k]
            if f.get('/Subtype') != '/Form':
                continue
            data = f.read_bytes()
            if b'BT' in data or len(data) > 400000 or not re.search(rb'\bc\b', data):
                continue
            out.append({'sig': 'form:' + hashlib.sha1(data).hexdigest()[:20], 'name': str(k), 'obj': f})
    except Exception:
        pass
    return out


def render_group(evs, path_png, size=160):
    """Thumbnail of one cluster, rebuilt from its own path data (colour as captured, else black)."""
    import pypdfium2 as pdfium
    from PIL import Image
    bx0 = min(e['bb'][0] for e in evs); by0 = min(e['bb'][1] for e in evs)
    bx1 = max(e['bb'][2] for e in evs); by1 = max(e['bb'][3] for e in evs)
    w = max(bx1 - bx0, 1.0); h = max(by1 - by0, 1.0)
    ops = []
    for e in evs:
        c = e['col'] or (0,)
        ops.append(('%s %s' % (' '.join('%.3f' % v for v in c), {1: 'g', 3: 'rg', 4: 'k'}.get(len(c), 'g'))) if len(c) in (1, 3, 4) else '0 g')
        for k, ps in e['ops']:
            P = ['%.2f %.2f' % (x - bx0, y - by0) for x, y in ps]
            if k == 're':
                (x0, y0), (x1, y1) = ps; ops.append('%.2f %.2f %.2f %.2f re' % (x0 - bx0, y0 - by0, x1 - x0, y1 - y0))
            elif k == 'h': ops.append('h')
            elif k == 'c': ops.append(' '.join(P) + (' c' if len(P) == 3 else ' v' if len(P) == 2 else ' l'))
            else: ops.append(P[0] + ' ' + k)
        ops.append(e['paint'] if e['paint'] in FILL else 'f')
    pdf = pikepdf.new(); pdf.add_blank_page(page_size=(w, h))
    pdf.pages[0].obj['/Contents'] = pdf.make_stream('\n'.join(ops).encode())
    import io
    bio = io.BytesIO(); pdf.save(bio)
    doc = pdfium.PdfDocument(bio.getvalue()); sc = size / max(w, h)
    doc[0].render(scale=sc).to_pil().convert('RGB').save(path_png)


_KS = {}


def load_table(path=None):
    """sig -> label. Column 6 (if present) is the logo's event count k, kept in _KS for window matching."""
    p = path or os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'vector_logos.tsv')
    t = {}
    if os.path.exists(p):
        for line in open(p, encoding='utf-8'):
            a = line.rstrip('\n').split('\t')
            if len(a) >= 2 and not a[0].startswith('#'):
                t[a[0]] = a[1]
                if len(a) >= 6 and a[5].isdigit():
                    _KS[a[0]] = int(a[5])
    return t


def match_windows(g, logo, ks, max_events=400):
    """A labelled logo merged into a bigger cluster (e.g. with the file's own title text): compare every window of k
    consecutive filled paths with the labelled signatures. -> list of (events, bbox)."""
    out = []; evs = g['evs']; used = set()
    if len(evs) > max_events:
        return out
    for k in ks:
        if k <= 0 or k >= len(evs):
            continue
        i = 0
        while i + k <= len(evs):
            if not (used & set(range(i, i + k))) and _sig(evs[i:i + k]) in logo:
                w = evs[i:i + k]; used |= set(range(i, i + k))
                out.append((w, (min(e['bb'][0] for e in w), min(e['bb'][1] for e in w), max(e['bb'][2] for e in w), max(e['bb'][3] for e in w))))
                i += k
            else:
                i += 1
    return out


def remove_vector_logos(pdf, table=None, max_pages=3):
    """Drop clusters and blank forms whose signature is labelled 'logo'; draw a light grey box in their place."""
    t = load_table() if table is None else table
    logo = {k for k, v in t.items() if v == 'logo'}
    n = 0
    if not logo:
        return 0
    for pno, page in enumerate(pdf.pages):
        if pno >= max_pages:
            break
        try:
            instrs = list(pikepdf.parse_content_stream(page))
            ks = sorted({_KS[x] for x in logo if x in _KS})
            hits = []
            for g in page_groups(instrs):
                if g['sig'] in logo:
                    hits.append(g)
                elif ks:
                    for w, bb in match_windows(g, logo, ks):
                        hits.append({'ii': sorted({i for e in w for i in e['ii']}), 'bb': bb})
            if hits:
                drop = {i for g in hits for i in g['ii']}
                new = [ins for i, ins in enumerate(instrs) if i not in drop]
                tail = []
                for g in hits:
                    x0, y0, x1, y1 = g['bb']
                    tail += [([], Operator('q')), ([0.92], Operator('g')), ([round(x0, 2), round(y0, 2), round(x1 - x0, 2), round(y1 - y0, 2)], Operator('re')),
                             ([], Operator('f')), ([], Operator('Q'))]
                page.obj['/Contents'] = pdf.make_stream(pikepdf.unparse_content_stream([([], Operator('q'))] + new + [([], Operator('Q'))] + tail))
                n += len(hits)
            for f in form_sigs(pdf, page):
                if f['sig'] in logo:
                    bb = [float(v) for v in f['obj'].get('/BBox', [0, 0, 0, 0])]
                    f['obj'].write(('q 0.92 g %.2f %.2f %.2f %.2f re f Q' % (bb[0], bb[1], bb[2] - bb[0], bb[3] - bb[1])).encode())
                    n += 1
        except Exception:
            continue
    return n
