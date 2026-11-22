"""Vector glyph shapes inside a box (rule glyph3 from work/pdf/paths.py): owned by the PDF builder.
Copied from deid/vision/notext.py so pdf.py does not depend on a file another builder edits."""
from . import pdftext

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



def kill_set(dele):
    """Instruction indexes to drop for a set of deleted subpaths (paint operator too when its whole group goes)."""
    kill = set(); bypaint = {}; seen = set()
    for d in dele:
        if id(d) in seen:
            continue
        seen.add(id(d)); kill.update(d['i']); bypaint[d['paint']] = bypaint.get(d['paint'], 0) + 1
    for d in dele:
        if bypaint[d['paint']] == d['npaint']:
            kill.add(d['paint'])
    return kill
