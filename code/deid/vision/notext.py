"""Drawings without a text layer (B14): render, OCR the whole page, gate, match, remove, redraw.

Design (chosen over a title-block detector): OCR the full page (tiled, plus a 90-degree pass, deid/vision/ocr.py) and
gate on the OCR text itself: the page is edited only if OCR reads 3+ distinct title-block keywords. The detector's only
benefit would be speed (the block is ~10% of the page); its misses (24% on unseen sources) and false blocks on non-drawings
(4 in 10) are worse than the cost of reading the whole page, and compute is not the constraint.
Edit per hit box:
  vector page: delete glyph-like subpaths whose centre is in the box (rule glyph3 from work/pdf/paths.py: keeps frame and
               cell lines), then draw the stand-in in Helvetica fitted to the box;
  scan page:   paint the box inside the page image itself (component erase + stand-in, ocr.paint), re-encode that image.
After the edit the page is rendered again and every hit box is read a second time (ocr.verify_gone). If an old string is
still read, the page's original content (and image) are put back: st['verify'] = 'failed', st['verify_left'] = [...].
Render: pdfium at DPI (200, vision.md), RGB; OCR results come from the content-addressed cache in ocr.py when present.
Edit rules: DEID_OCR_RULES (comma list of mapping, extra, label; default all three) on pages the gate calls drawings.
"""
import io, os, re, zlib, ctypes
import numpy as np
import pikepdf
from pikepdf import Name, Operator, String

from . import pdftext

from .ocr import KW, keywords  # noqa: F401  (gate lives in ocr.py, shared with the image handler)
DPI = 200
RULES = tuple(r for r in os.environ.get('DEID_OCR_RULES', 'mapping,extra,label').split(',') if r)
PAINT = {'S': (0, 1), 's': (0, 1), 'f': (1, 0), 'F': (1, 0), 'f*': (1, 0), 'B': (1, 1), 'B*': (1, 1), 'b': (1, 1),
         'b*': (1, 1), 'n': (0, 0)}


# ---------------------------------------------------------------- vector shapes (from work/pdf/paths.py, rule glyph3)
def _axis(p, q):
    dx = abs(p[0] - q[0]); dy = abs(p[1] - q[1])
    return dx <= 0.02 * dy + 1e-6 or dy <= 0.02 * dx + 1e-6


def page_paths(instrs):
    num = pdftext.num; mul = pdftext.mul
    ctm = pdftext.I6; lw = 1.0; stack = []; cur = []; sp = None; clip = False; out = []

    def T(x, y): return (x * ctm[0] + y * ctm[2] + ctm[4], x * ctm[1] + y * ctm[3] + ctm[5])

    def new(ii, pts, **kw):
        d = {'i': [ii], 'pts': pts, 'nseg': 0, 'curve': False, 'axis': True, 'closed': False, 'complex': False}
        d.update(kw); cur.append(d); return d
    for ii, (ops, op) in enumerate(instrs):
        o = str(op)
        try:
            if o == 'q': stack.append((ctm, lw))
            elif o == 'Q':
                if stack: ctm, lw = stack.pop()
            elif o == 'cm': ctm = mul([num(x) for x in ops], ctm)
            elif o == 'w': lw = num(ops[0])
            elif o == 'm': sp = new(ii, [T(num(ops[0]), num(ops[1]))])
            elif o == 'l':
                p = T(num(ops[0]), num(ops[1]))
                if sp is None or sp['closed']: sp = new(ii, [p], complex=True)
                else:
                    if not _axis(sp['pts'][-1], p): sp['axis'] = False
                    sp['pts'].append(p); sp['nseg'] += 1; sp['i'].append(ii)
            elif o in ('c', 'v', 'y'):
                pts = [T(num(ops[k]), num(ops[k + 1])) for k in range(0, len(ops) - 1, 2)]
                if sp is None or sp['closed']: sp = new(ii, pts, complex=True)
                else: sp['pts'] += pts; sp['nseg'] += 1; sp['i'].append(ii)
                sp['curve'] = True; sp['axis'] = False
            elif o == 'h':
                if sp is not None:
                    if len(sp['pts']) > 1 and not _axis(sp['pts'][-1], sp['pts'][0]): sp['axis'] = False
                    sp['closed'] = True; sp['i'].append(ii)
            elif o == 're':
                x, y, w, h = [num(v) for v in ops]; pts = [T(x, y), T(x + w, y), T(x + w, y + h), T(x, y + h)]
                new(ii, pts, nseg=4, closed=True, axis=_axis(pts[0], pts[1]) and _axis(pts[1], pts[2])); sp = None
            elif o in ('W', 'W*'): clip = True
            elif o in PAINT:
                f, s = PAINT[o]
                for d in cur:
                    xs = [p[0] for p in d['pts']]; ys = [p[1] for p in d['pts']]
                    d.update(fill=f, stroke=s, paint=ii, clip=clip, bb=(min(xs), min(ys), max(xs), max(ys)), npaint=len(cur))
                    out.append(d)
                cur = []; sp = None; clip = False
        except Exception:
            continue
    return out


def glyph_shapes(paths, box):
    x0, y0, x1, y1 = box

    def inbox(d):
        bx0, by0, bx1, by1 = d['bb']; cx = (bx0 + bx1) / 2; cy = (by0 + by1) / 2
        return x0 <= cx <= x1 and y0 <= cy <= y1
    cand = [d for d in paths if (d['fill'] or d['stroke']) and not d['clip'] and not d['complex'] and inbox(d)]
    sizes = sorted(max(d['bb'][2] - d['bb'][0], d['bb'][3] - d['bb'][1]) for d in cand if not d['axis'])
    href = sizes[int(len(sizes) * 0.9)] if len(sizes) >= 5 else 0.5 * min(x1 - x0, y1 - y0)
    dele = []
    for d in cand:
        bx0, by0, bx1, by1 = d['bb']; w = bx1 - bx0; h = by1 - by0; long = max(w, h); short = min(w, h)
        m = 0.25 * href
        if not (bx0 >= x0 - m and bx1 <= x1 + m and by0 >= y0 - m and by1 <= y1 + m):
            continue
        thr = max(1.6 * href, 1.25 * min(x1 - x0, y1 - y0))
        keep = ((d['axis'] or d['nseg'] == 1 or short < long / 15.0) and long > thr) or \
               (d['stroke'] and not d['fill'] and long > max(2.5 * href, thr))
        if not keep:
            dele.append(d)
    return dele


def remove_paths(instrs, dele):
    kill = set(); bypaint = {}
    for d in dele:
        kill.update(d['i']); bypaint[d['paint']] = bypaint.get(d['paint'], 0) + 1
    for d in dele:
        if bypaint[d['paint']] == d['npaint']:
            kill.add(d['paint'])
    return [ins for ii, ins in enumerate(instrs) if ii not in kill]


# ---------------------------------------------------------------- geometry
def _dev2page(page, W, H, x, y):
    import pypdfium2.raw as raw
    px = ctypes.c_double(); py = ctypes.c_double()
    raw.FPDF_DeviceToPage(page.raw, 0, 0, W, H, 0, int(round(x)), int(round(y)), ctypes.byref(px), ctypes.byref(py))
    return px.value, py.value


def _inv(m):
    a, b, c, d, e, f = m; det = a * d - b * c
    if abs(det) < 1e-12:
        return None
    return [d / det, -b / det, -c / det, a / det, (c * f - d * e) / det, (b * e - a * f) / det]


def _images_on_page(instrs, res):
    """[(name, ctm)] for image XObjects drawn directly by the page."""
    out = []; ctm = pdftext.I6; stack = []
    for ops, op in instrs:
        o = str(op)
        if o == 'q': stack.append(ctm)
        elif o == 'Q':
            if stack: ctm = stack.pop()
        elif o == 'cm': ctm = pdftext.mul([pdftext.num(x) for x in ops], ctm)
        elif o == 'Do':
            try:
                xo = res.XObject[str(ops[0])]
                if xo.get('/Subtype') == '/Image':
                    out.append((str(ops[0]), ctm))
            except Exception:
                pass
    return out


# ---------------------------------------------------------------- main entry
def render(pg, dpi=DPI):
    """The one page render used for OCR, its cache key and the second read: pdfium, scale dpi/72, RGB uint8."""
    return np.ascontiguousarray(np.asarray(pg.render(scale=dpi / 72.0).to_pil().convert('RGB')))


def render_bytes(data, pno, dpi=DPI):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(data)
    try:
        return render(doc[pno], dpi)
    finally:
        doc.close()


def process_page(pdf, data, pno, mapping, source, extra_values=(), dpi=DPI, rules=None, verify=True):
    """Edits pdf (pikepdf) in place for page pno. data = original bytes (rendered with pdfium). -> stats dict."""
    import pypdfium2 as pdfium
    from . import ocr
    st = {'page': pno}
    doc = pdfium.PdfDocument(data)
    try:
        pg = doc[pno]
        img = render(pg, dpi)
        Hh, Ww = img.shape[:2]
        words = ocr.read_words(img, rotate_pass=True)
        st['ocr_words'] = len(words)
        kws = keywords(words); st['keywords'] = sorted(kws)
        if len(kws) < ocr.GATE_MIN:
            st['gate'] = 'not_drawing'; return st
        st['gate'] = 'drawing'
        hits = ocr.find_hits(words, mapping, source, extra_values, rules=rules or RULES)
        st['why'] = [h['why'] for h in hits]
        st['hits'] = [(h['old'], h['new'], [[round(float(x), 1), round(float(y), 1)] for x, y in h['quad']]) for h in hits]
        if not hits:
            return st
        boxes = []
        for h in hits:
            pts = [_dev2page(pg, Ww, Hh, x, y) for x, y in h['quad']]
            xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
            boxes.append(((min(xs), min(ys), max(xs), max(ys)), h))
    finally:
        doc.close()
    page = pdf.pages[pno]
    res = pdftext.inherited(page.obj, '/Resources')
    undo = _snapshot(page, res)
    _edit(pdf, page, res, boxes, st)
    if verify and (st.get('drawn') or st.get('painted')):
        try:
            bio = io.BytesIO(); pdf.save(bio)
            after = render_bytes(bio.getvalue(), pno, dpi)
            left = ocr.verify_gone(after, hits) if after.shape == img.shape else ['<render size changed>']
            if not left:
                out_n = _changed_outside(img, after, hits)
                st['px_out'] = out_n
                if out_n > 0.001 * img.shape[0] * img.shape[1]:
                    left = ['<%d px changed outside the boxes>' % out_n]
        except Exception as e:
            left = ['<verify error %s>' % type(e).__name__]
        st['verify'] = 'failed' if left else 'ok'
        if left:
            st['verify_left'] = left; _restore(page, res, undo); st['drawn'] = 0; st['painted'] = 0
    return st


def _changed_outside(a, b, hits, grow=6, level=48):
    """Pixels that differ by more than `level` (JPEG re-encode noise stays below) outside the hit boxes grown by
    `grow` px: a wrong coordinate frame or a broken image shows up here even when the old string is gone."""
    d = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2) > level
    H, W = d.shape
    for h in hits:
        xs = [p[0] for p in h['quad']]; ys = [p[1] for p in h['quad']]
        d[max(0, int(min(ys)) - grow):min(H, int(max(ys)) + grow + 1), max(0, int(min(xs)) - grow):min(W, int(max(xs)) + grow + 1)] = False
    return int(d.sum())


def _snapshot(page, res):
    u = {'contents': page.obj.get('/Contents'), 'had_font': False, 'had_fontdict': False, 'images': {}}
    try:
        u['had_fontdict'] = res is not None and '/Font' in res
        u['had_font'] = res is not None and '/Font' in res and '/HvDeid' in res.Font
    except Exception:
        pass
    try:
        for k, xo in (res.XObject.items() if res is not None and '/XObject' in res else []):
            if xo.get('/Subtype') == '/Image':
                u['images'][k] = (xo, xo.read_raw_bytes(), {kk: xo.get(kk) for kk in
                                  ('/Filter', '/DecodeParms', '/ColorSpace', '/BitsPerComponent', '/Decode', '/ImageMask')})
    except Exception:
        pass
    return u


def _restore(page, res, u):
    if u['contents'] is not None:
        page.obj['/Contents'] = u['contents']
    try:
        if not u['had_font'] and res is not None and '/Font' in res and '/HvDeid' in res.Font:
            del res.Font['/HvDeid']
        if not u['had_fontdict'] and res is not None and '/Font' in res:
            del res['/Font']
    except Exception:
        pass
    for k, (xo, raw, d) in u['images'].items():
        try:
            if xo.read_raw_bytes() != raw:
                xo.write(raw, filter=d['/Filter'], decode_parms=d['/DecodeParms'])
                for kk, v in d.items():
                    if v is None:
                        if kk in xo:
                            del xo[kk]
                    elif kk not in ('/Filter', '/DecodeParms'):
                        xo[kk] = v
        except Exception:
            pass


def _in_image(b, m):
    """Box b (page space) lies inside the unit square drawn with image matrix m."""
    inv = _inv(m)
    if inv is None:
        return False
    for x, y in ((b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])):
        u, v = x * inv[0] + y * inv[2] + inv[4], x * inv[1] + y * inv[3] + inv[5]
        if not (-0.001 <= u <= 1.001 and -0.001 <= v <= 1.001):
            return False
    return True


def _edit(pdf, page, res, boxes, st):
    """Per hit box: vector lettering (glyph-like paths in the box) -> remove paths and draw the stand-in; no such paths
    and the box inside a page-size image -> paint inside the image (scan); else draw over (the second read decides).
    (Earlier the whole page was treated as a scan when any image covered half the page: a vector drawing with a large
    render on it then got its image painted while the lettering, outside the image, stayed. Observed on one corpus file, page 2.)"""
    instrs = list(pikepdf.parse_content_stream(page))
    mb = [float(x) for x in page.mediabox]; parea = abs((mb[2] - mb[0]) * (mb[3] - mb[1])) or 1.0
    imgs = _images_on_page(instrs, res) if res is not None else []
    big = [(n, m) for n, m in imgs if abs(m[0] * m[3] - m[1] * m[2]) > 0.5 * parea]
    paths = page_paths(instrs); dele = []; vec = []; scan = []
    for (b, h) in boxes:
        d = glyph_shapes(paths, b)
        if not d and big and _in_image(b, big[0][1]):
            scan.append((b, h))
        else:
            dele += d; vec.append((b, h))
    st['kind'] = 'scan' if scan and not vec else ('mixed' if scan else 'vector')
    if scan:
        st['painted'] = _paint_scan(pdf, res, big[0], scan, st)
    if not vec:
        return
    boxes = vec
    st['shapes_removed'] = len(dele)
    new = remove_paths(instrs, dele) if dele else instrs
    tail = []
    for (x0, y0, x1, y1), h in boxes:
        vertical = h.get('angle', 0) == 90 or (y1 - y0) > 1.8 * (x1 - x0)
        txt = h['new']; hw = pdftext.helv_w(txt) / 1000.0 or 1.0
        if vertical:
            size = (x1 - x0) * 0.8; length = y1 - y0
            tz = max(30.0, min(100.0, 100.0 * length / (hw * size)))
            tm = [0, size, -size, 0, x1 - 0.15 * (x1 - x0), y0]
        else:
            size = (y1 - y0) * 0.8; length = x1 - x0
            tz = max(30.0, min(100.0, 100.0 * length / (hw * size)))
            tm = [size, 0, 0, size, x0, y0 + 0.15 * (y1 - y0)]
        tail += [([], Operator('q')), ([0], Operator('g')), ([], Operator('BT')), ([Name('/HvDeid'), 1], Operator('Tf')),
                 ([round(v, 4) for v in tm], Operator('Tm')), ([round(tz, 2)], Operator('Tz')),
                 ([String(txt.encode('cp1252', 'replace'))], Operator('Tj')), ([], Operator('ET')), ([], Operator('Q'))]
    page.obj['/Contents'] = pdf.make_stream(pikepdf.unparse_content_stream([([], Operator('q'))] + new + [([], Operator('Q'))] + tail))
    pdftext.add_font(pdf, res if res is not None else page.obj.setdefault('/Resources', pikepdf.Dictionary()))
    st['drawn'] = len(boxes)


def _paint_scan(pdf, res, big, boxes, st):
    from . import ocr
    name, m = big
    xo = res.XObject[name]
    try:
        pim = pikepdf.PdfImage(xo); im = pim.as_pil_image()
    except Exception as e:
        st['scan_err'] = 'decode:' + type(e).__name__; return 0
    was_jpeg = xo.get('/Filter') == '/DCTDecode'
    arr = np.array(im.convert('RGB'))          # a writable copy (np.asarray of a PIL image is read-only: ValueError)
    IH, IW = arr.shape[:2]; inv = _inv(m); n = 0
    if inv is None:
        return 0
    for (x0, y0, x1, y1), h in boxes:
        q = []
        for (x, y) in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            u, v = x * inv[0] + y * inv[2] + inv[4], x * inv[1] + y * inv[3] + inv[5]
            q.append((u * IW, (1 - v) * IH))
        try:
            ocr.paint(arr, q, h['old'], h['new']); n += 1
        except Exception as e:
            st['paint_err'] = type(e).__name__
    if not n:
        return 0
    from PIL import Image
    gray = im.mode in ('1', 'L')
    out = Image.fromarray(arr).convert('L' if gray else 'RGB')
    if im.mode == '1' and not was_jpeg:        # bilevel scans stay 1 bit per pixel
        bw = out.point(lambda v: 255 if v >= 128 else 0).convert('1', dither=Image.Dither.NONE)
        xo.write(zlib.compress(bw.tobytes(), 6), filter=Name.FlateDecode)
        xo.ColorSpace = Name.DeviceGray; xo.BitsPerComponent = 1
        for k in ('/Decode', '/DecodeParms', '/ImageMask'):
            if k in xo:
                del xo[k]
        return n
    if was_jpeg:
        bio = io.BytesIO(); out.save(bio, format='JPEG', quality=92)
        xo.write(bio.getvalue(), filter=Name.DCTDecode)
    else:
        xo.write(zlib.compress(out.tobytes(), 6), filter=Name.FlateDecode)
    xo.ColorSpace = Name.DeviceGray if gray else Name.DeviceRGB
    xo.BitsPerComponent = 8
    for k in ('/Decode', '/DecodeParms', '/ImageMask'):
        if k in xo:
            del xo[k]
    return n
