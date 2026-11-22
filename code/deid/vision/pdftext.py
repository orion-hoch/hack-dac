"""PDF page text at glyph level, permissive code only (pikepdf MPL-2.0 for objects and tokenising).

Adapted from work/pdf/textswap.py (exp 2, method (c)): a glyph is removed by deleting its code and putting its advance
back as a TJ displacement, so nothing else on the line moves; the stand-in is drawn afterwards in base-14 Helvetica with
the text-rendering matrix of the first removed glyph, squeezed with Tz to the removed width.
Difference from the prototype: edits are given as glyph indices with new characters (found by diffing a same-length
substitution of the unit's reconstructed text), not by searching target strings again, so targets cannot shadow each
other and every edit is exactly the substitution's.
"""
import re
import pikepdf
from pikepdf import Name, Operator, Array, String, Dictionary

HELV = [278,278,355,556,556,889,667,191,333,333,389,584,278,333,278,278,556,556,556,556,556,556,556,556,556,556,278,278,584,584,584,556,1015,667,667,722,722,667,611,778,722,278,500,667,556,833,722,778,667,778,722,667,611,722,667,944,667,667,611,278,278,278,469,556,333,556,556,500,556,556,278,556,556,222,222,500,222,833,556,556,556,556,333,500,278,556,500,722,500,500,500,334,260,334,584]
I6 = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
AGL = {'space': ' ', 'period': '.', 'comma': ',', 'hyphen': '-', 'minus': '-', 'slash': '/', 'colon': ':', 'semicolon': ';',
       'underscore': '_', 'parenleft': '(', 'parenright': ')', 'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4',
       'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9', 'at': '@', 'ampersand': '&', 'numbersign': '#',
       'plus': '+', 'equal': '=', 'quotesingle': "'", 'quotedbl': '"', 'degree': '\xb0', 'plusminus': '\xb1',
       'endash': '–', 'emdash': '—', 'bullet': '•', 'adieresis': '\xe4', 'odieresis': '\xf6',
       'udieresis': '\xfc', 'Adieresis': '\xc4', 'Odieresis': '\xd6', 'Udieresis': '\xdc', 'germandbls': '\xdf',
       'eacute': '\xe9', 'egrave': '\xe8', 'agrave': '\xe0', 'ccedilla': '\xe7', 'percent': '%', 'asterisk': '*',
       'question': '?', 'exclam': '!', 'bracketleft': '[', 'bracketright': ']', 'less': '<', 'greater': '>', 'dollar': '$',
       'quoteright': '’', 'quoteleft': '‘', 'copyright': '\xa9', 'registered': '\xae', 'backslash': '\\'}


def helv_w(s):
    return sum(HELV[ord(c) - 32] if 32 <= ord(c) <= 126 else 556 for c in s)


def mul(a, b):
    return [a[0]*b[0]+a[1]*b[2], a[0]*b[1]+a[1]*b[3], a[2]*b[0]+a[3]*b[2], a[2]*b[1]+a[3]*b[3],
            a[4]*b[0]+a[5]*b[2]+b[4], a[4]*b[1]+a[5]*b[3]+b[5]]


def apply(m, x, y):
    return (x * m[0] + y * m[2] + m[4], x * m[1] + y * m[3] + m[5])


def gname(n):
    if len(n) == 1:
        return n
    if n in AGL:
        return AGL[n]
    m = re.match(r'uni([0-9A-Fa-f]{4})$', n) or re.match(r'u([0-9A-Fa-f]{4,6})$', n)
    return chr(int(m.group(1), 16)) if m else None


def hx(b):
    b = re.sub(rb'\s', b'', b)
    if len(b) % 2:
        b += b'0'
    return bytes.fromhex(b.decode('ascii'))


def parse_tounicode(data):
    m = {}
    for blk in re.findall(rb'beginbfchar(.*?)endbfchar', data, re.S):
        for a, b in re.findall(rb'<([0-9A-Fa-f\s]+)>\s*<([0-9A-Fa-f\s]*)>', blk):
            m[hx(a)] = hx(b).decode('utf-16-be', 'replace')
    for blk in re.findall(rb'beginbfrange(.*?)endbfrange', data, re.S):
        for a, b, c in re.findall(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f\s]*>|\[[^\]]*\])', blk):
            lo = int(a, 16); hi = int(b, 16); n = (len(a) + 1) // 2
            if hi - lo > 70000:
                continue
            if c.startswith(b'['):
                for i, it in enumerate(re.findall(rb'<([0-9A-Fa-f\s]*)>', c)):
                    if lo + i > hi:
                        break
                    m[(lo + i).to_bytes(n, 'big')] = hx(it).decode('utf-16-be', 'replace')
            else:
                base = hx(c[1:-1])
                if len(base) < 2:
                    base = b'\x00' * (2 - len(base)) + base
                v0 = int.from_bytes(base[-2:], 'big')
                for i in range(hi - lo + 1):
                    m[(lo + i).to_bytes(n, 'big')] = (base[:-2] + ((v0 + i) & 0xFFFF).to_bytes(2, 'big')).decode('utf-16-be', 'replace')
    return m


class Font:
    def __init__(self, f):
        self.sub = str(f.get('/Subtype', '')); self.type0 = self.sub == '/Type0'; self.type3 = self.sub == '/Type3'
        self.n = 2 if self.type0 else 1; self.tou = None; self.w = {}; self.dw = 1000.0 if self.type0 else 556.0
        self.enc = {}; self.err = None
        try:
            if '/ToUnicode' in f:
                self.tou = parse_tounicode(f.ToUnicode.read_bytes())
        except Exception as e:
            self.err = 'tou:' + type(e).__name__
        if self.type0 and not self.tou:
            self._cid_tables(f)
        try:
            if self.type0:
                d = f.DescendantFonts[0]; self.dw = float(d.get('/DW', 1000)); W = d.get('/W')
                if W is not None:
                    W = list(W); i = 0
                    while i + 1 < len(W):
                        c = int(W[i])
                        if isinstance(W[i + 1], Array):
                            for k, x in enumerate(W[i + 1]):
                                self.w[c + k] = float(x)
                            i += 2
                        else:
                            c2 = int(W[i + 1]); x = float(W[i + 2])
                            for k in range(c, min(c2, c + 70000) + 1):
                                self.w[k] = x
                            i += 3
            else:
                fc = int(f.get('/FirstChar', 0)); Ws = f.get('/Widths'); sc = 1.0
                if self.type3 and f.get('/FontMatrix') is not None:
                    sc = float(f.FontMatrix[0]) * 1000
                if Ws is not None:
                    for k, x in enumerate(Ws):
                        self.w[fc + k] = float(x) * sc
                enc = f.get('/Encoding'); base = 'latin-1'; diffs = None
                tbl = {'/WinAnsiEncoding': 'cp1252', '/MacRomanEncoding': 'mac_roman'}
                if isinstance(enc, Name):
                    base = tbl.get(str(enc), 'latin-1')
                elif isinstance(enc, Dictionary):
                    base = tbl.get(str(enc.get('/BaseEncoding', '')), 'latin-1'); diffs = enc.get('/Differences')
                for c in range(256):
                    try:
                        self.enc[c] = bytes([c]).decode(base)
                    except Exception:
                        pass
                if diffs is not None:
                    code = 0
                    for it in diffs:
                        if isinstance(it, Name):
                            u = gname(str(it)[1:])
                            if u:
                                self.enc[code] = u
                            code += 1
                        else:
                            code = int(it)
                if Ws is None:
                    if not self._embedded_widths(f) and not self._base14_widths(f):
                        for c, u in self.enc.items():
                            if len(u) == 1 and 32 <= ord(u) <= 126:
                                self.w[c] = HELV[ord(u) - 32]
        except Exception as e:
            self.err = 'w:' + type(e).__name__

    def _embedded_widths(self, f):
        """Simple font without /Widths: take advances from the embedded TrueType/OpenType program (fontTools, MIT),
        as the renderer does. Without this the restored TJ displacement is wrong and the rest of the line moves."""
        try:
            fd = f.get('/FontDescriptor')
            ff = fd.get('/FontFile2') if fd is not None else None
            if ff is None and fd is not None:
                ff = fd.get('/FontFile3')
            if ff is None:
                return False
            import io
            from fontTools.ttLib import TTFont
            tt = TTFont(io.BytesIO(ff.read_bytes()), lazy=True)
            upm = tt['head'].unitsPerEm or 1000
            hm = tt['hmtx'].metrics; order = tt.getGlyphOrder()
            cm = {}
            for t in tt['cmap'].tables:
                cm[(t.platformID, t.platEncID)] = t.cmap
            n = 0
            for c in range(256):
                g = None
                if (3, 0) in cm: g = cm[(3, 0)].get(0xF000 + c) or cm[(3, 0)].get(c)
                if g is None and (1, 0) in cm: g = cm[(1, 0)].get(c)
                if g is None and (3, 1) in cm and c in self.enc: g = cm[(3, 1)].get(ord(self.enc[c][0]))
                if g is None and c < len(order) and not cm: g = order[c]
                if g is not None and g in hm:
                    self.w[c] = hm[g][0] * 1000.0 / upm; n += 1
            return n > 0
        except Exception:
            return False

    def _base14_widths(self, f):
        """Standard 14 font without /Widths (Times, Courier, Symbol...): its real AFM advances (reportlab metrics, BSD)."""
        try:
            name = str(f.get('/BaseFont', ''))[1:].split('+')[-1]
            alias = {'TimesNewRoman': 'Times-Roman', 'TimesNewRoman,Bold': 'Times-Bold', 'TimesNewRoman,Italic': 'Times-Italic',
                     'TimesNewRoman,BoldItalic': 'Times-BoldItalic', 'Arial': 'Helvetica', 'Arial,Bold': 'Helvetica-Bold',
                     'CourierNew': 'Courier', 'Times': 'Times-Roman'}
            name = alias.get(name, name)
            from reportlab.pdfbase import pdfmetrics
            if name not in pdfmetrics.standardFonts:
                return False
            font = pdfmetrics.getFont(name)
            for c, u in self.enc.items():
                if len(u) == 1:
                    try:
                        self.w[c] = font.stringWidth(u, 1000)
                    except Exception:
                        pass
            return True
        except Exception:
            return False

    def _cid_tables(self, f):
        """Type0 font without a usable ToUnicode: code -> CID (Identity encodings only) -> GID (CIDToGIDMap) ->
        Unicode from the embedded font's own cmap, else from its glyph names (post table / CFF charset)."""
        self.cid_rev = None; self.cid2gid = None
        try:
            if str(f.get('/Encoding', '')) not in ('/Identity-H', '/Identity-V'):
                return
            d = f.DescendantFonts[0]; fd = d.get('/FontDescriptor')
            if fd is None:
                return
            import io
            ff = fd.get('/FontFile2'); rev = {}
            if ff is not None:
                from fontTools.ttLib import TTFont
                tt = TTFont(io.BytesIO(ff.read_bytes()), lazy=True)
                order = tt.getGlyphOrder(); gid = {n: i for i, n in enumerate(order)}
                if 'cmap' in tt:
                    for t in tt['cmap'].tables:
                        if t.isUnicode() or (t.platformID, t.platEncID) in ((3, 1), (3, 10), (0, 3), (0, 4)):
                            for u, n in t.cmap.items():
                                if n in gid and gid[n] not in rev and u >= 32:
                                    rev[gid[n]] = chr(u)
                for i, n in enumerate(order):
                    if i not in rev:
                        u = gname(n)
                        if u and u != '.notdef' and len(u) == 1:
                            rev[i] = u
                m = d.get('/CIDToGIDMap')
                if isinstance(m, pikepdf.Stream):
                    self.cid2gid = m.read_bytes()
            else:
                ff = fd.get('/FontFile3')
                if ff is None:
                    return
                from fontTools.cffLib import CFFFontSet
                cff = CFFFontSet(); cff.decompile(io.BytesIO(ff.read_bytes()), None)
                top = cff[cff.fontNames[0]]
                for i, n in enumerate(top.charset):
                    u = gname(n)
                    if u and len(u) == 1:
                        rev[i] = u
            self.cid_rev = rev or None
        except Exception:
            self.cid_rev = None

    def decode(self, code):
        if self.tou is not None:
            u = self.tou.get(code)
            if u is not None or (self.type0 and not getattr(self, 'cid_rev', None)):
                return u
        if self.type0:
            rev = getattr(self, 'cid_rev', None)
            if rev:
                cid = int.from_bytes(code, 'big'); g = cid
                if self.cid2gid is not None and 2 * cid + 1 < len(self.cid2gid):
                    g = int.from_bytes(self.cid2gid[2 * cid:2 * cid + 2], 'big')
                return rev.get(g)
            return None
        return self.enc.get(code[0])

    def width(self, code):
        return self.w.get(int.from_bytes(code, 'big'), self.dw)


def num(x):
    try:
        return float(x)
    except Exception:
        return 0.0


def interpret(instrs, fonts, on_do=None):
    """Glyph list for one content unit. trm = text-rendering matrix in the unit's space (font size 1)."""
    gs = {'ctm': I6, 'Tc': 0.0, 'Tw': 0.0, 'Th': 1.0, 'TL': 0.0, 'font': None, 'size': 0.0, 'Tr': 0, 'Ts': 0.0, 'col': None}
    stack = []; Tm = I6; Tlm = I6; G = []

    def show(ii, k, s):
        nonlocal Tm
        f = gs['font']; n = f.n if f else 1; size = gs['size']; Th = gs['Th']
        for b0 in range(0, len(s) - n + 1, n):
            code = s[b0:b0 + n]; w0 = f.width(code) if f else 500.0
            tx = (w0 / 1000.0 * size + gs['Tc'] + (gs['Tw'] if code == b' ' else 0.0)) * Th
            trm = mul(mul([size * Th, 0, 0, size, 0, gs['Ts']], Tm), gs['ctm'])
            Tm = mul([1, 0, 0, 1, tx, 0], Tm)
            nx = mul(mul([1, 0, 0, 1, 0, gs['Ts']], Tm), gs['ctm'])
            G.append({'ii': ii, 'k': k, 'b0': b0, 'n': n, 'u': f.decode(code) if f else None, 'trm': trm,
                      'nxt': (nx[4], nx[5]),
                      'units': w0 + ((gs['Tc'] + (gs['Tw'] if code == b' ' else 0.0)) * 1000.0 / size if size else 0.0),
                      'tr': gs['Tr'], 'col': gs['col']})
    for ii, (ops, op) in enumerate(instrs):
        o = str(op)
        try:
            if o == 'q': stack.append(dict(gs))
            elif o == 'Q':
                if stack: gs = stack.pop()
            elif o == 'cm': gs['ctm'] = mul([num(x) for x in ops], gs['ctm'])
            elif o == 'BT': Tm = Tlm = I6
            elif o == 'Tf': gs['font'] = fonts.get(str(ops[0])); gs['size'] = num(ops[1])
            elif o == 'Td': Tlm = mul([1, 0, 0, 1, num(ops[0]), num(ops[1])], Tlm); Tm = Tlm
            elif o == 'TD': gs['TL'] = -num(ops[1]); Tlm = mul([1, 0, 0, 1, num(ops[0]), num(ops[1])], Tlm); Tm = Tlm
            elif o == 'Tm': Tm = Tlm = [num(x) for x in ops]
            elif o == 'T*': Tlm = mul([1, 0, 0, 1, 0, -gs['TL']], Tlm); Tm = Tlm
            elif o == 'Tc': gs['Tc'] = num(ops[0])
            elif o == 'Tw': gs['Tw'] = num(ops[0])
            elif o == 'Tz': gs['Th'] = num(ops[0]) / 100.0
            elif o == 'TL': gs['TL'] = num(ops[0])
            elif o == 'Tr': gs['Tr'] = int(num(ops[0]))
            elif o == 'Ts': gs['Ts'] = num(ops[0])
            elif o in ('g', 'rg', 'k'): gs['col'] = (list(ops), o)
            elif o in ('sc', 'scn', 'cs'): gs['col'] = None
            elif o == 'Tj': show(ii, None, bytes(ops[0]))
            elif o == "'":
                Tlm = mul([1, 0, 0, 1, 0, -gs['TL']], Tlm); Tm = Tlm; show(ii, None, bytes(ops[0]))
            elif o == '"':
                gs['Tw'] = num(ops[0]); gs['Tc'] = num(ops[1]); Tlm = mul([1, 0, 0, 1, 0, -gs['TL']], Tlm); Tm = Tlm
                show(ii, None, bytes(ops[2]))
            elif o == 'TJ':
                for k, el in enumerate(ops[0]):
                    if isinstance(el, String): show(ii, k, bytes(el))
                    else: Tm = mul([1, 0, 0, 1, -num(el) / 1000.0 * gs['size'] * gs['Th'], 0], Tm)
            elif o == 'Do' and on_do: on_do(str(ops[0]), gs['ctm'])
        except Exception:
            continue
    return G


def build_text(G):
    """Reconstruct the unit's text in stream order. Returns (text, cmap) where cmap[i] is the glyph index of text[i]
    or -1 for a separator this function inserted (space for a gap on the same baseline, newline otherwise)."""
    out = []; cmap = []; prev = None
    for gi, g in enumerate(G):
        u = g['u'] if g['u'] is not None else '�'
        if prev is not None:
            p = G[prev]; t = p['trm']; h = (t[2] ** 2 + t[3] ** 2) ** 0.5 or 1.0
            rr = (t[0] ** 2 + t[1] ** 2) or 1.0
            dx = g['trm'][4] - p['nxt'][0]; dy = g['trm'][5] - p['nxt'][1]
            along = (dx * t[0] + dy * t[1]) / rr ** 0.5; across = abs(dx * t[2] + dy * t[3]) / h
            if across > 0.4 * h or along < -0.5 * h or along > 3.0 * h:
                out.append('\n'); cmap.append(-1)
            elif along > 0.2 * h and not out[-1].isspace() and not u.isspace():
                out.append(' '); cmap.append(-1)
        for ch in u:
            out.append(ch); cmap.append(gi)
        prev = gi
    return ''.join(out), cmap


def runs_from_diff(T, T2, cmap):
    """Maximal runs of changed characters (never across an inserted separator). -> list of (glyph indices, new text)."""
    runs = []; cur = []; new = []
    for i, (a, b) in enumerate(zip(T, T2)):
        gi = cmap[i]
        if a != b and gi >= 0:
            if cur and gi != cur[-1] and gi != cur[-1] + 1:
                runs.append((cur, ''.join(new))); cur = []; new = []
            if not cur or cur[-1] != gi:
                cur.append(gi)
            new.append(b)
        else:
            if cur and gi >= 0 and gi == cur[-1]:   # same multi-character glyph, partly unchanged
                new.append(b); continue
            if cur:
                runs.append((cur, ''.join(new))); cur = []; new = []
    if cur:
        runs.append((cur, ''.join(new)))
    return runs


def edit_unit(instrs, G, runs, font_name='/HvDeid', visible=(), drop=()):
    """runs: list of (glyph indices, new text). visible: indexes of runs whose stand-in is drawn visibly although the
    original glyphs were invisible (their drawn lettering is removed through drop). drop: instruction indexes to omit.
    Returns (new instructions, boxes in unit space, n_invisible)."""
    R = set(); draws = []; boxes = []; inv = 0; drop = set(drop)
    for ri, (gis, pseud) in enumerate(runs):
        b = G[gis[0]]; trm = b['trm']; rt = (trm[0], trm[1]); up = (trm[2], trm[3]); rr = rt[0] ** 2 + rt[1] ** 2
        if rr == 0:
            continue
        R.update(gis)
        e = G[gis[-1]]['nxt']; emw = ((e[0] - trm[4]) * rt[0] + (e[1] - trm[5]) * rt[1]) / rr
        hw = helv_w(pseud) / 1000.0
        tz = 100.0 * max(0.3, min(1.0, emw / hw)) if hw > 0 and emw > 0 else 100.0
        draws.append((trm, tz, 0 if ri in visible else b['tr'], None if ri in visible else b['col'], pseud))
        if b['tr'] in (3, 7):
            inv += 1
        boxes.append([(trm[4] + rt[0] * a + up[0] * c, trm[5] + rt[1] * a + up[1] * c)
                      for a in (0, max(emw, hw * tz / 100.0)) for c in (-0.35, 1.1)])
    if not R and not drop:
        return None, [], 0
    byel = {}
    for gi in R:
        byel.setdefault((G[gi]['ii'], G[gi]['k']), set()).add(gi)
    elg = {}
    for gi, g in enumerate(G):
        if (g['ii'], g['k']) in byel:
            elg.setdefault((g['ii'], g['k']), []).append(gi)

    def rebuild(s, key):
        out = []; cur = bytearray(); pend = 0.0
        for gi in elg[key]:
            g = G[gi]; code = s[g['b0']:g['b0'] + g['n']]
            if gi in R:
                if cur:
                    out.append(String(bytes(cur))); cur = bytearray()
                pend += g['units']
            else:
                if pend:
                    out.append(round(-pend, 3)); pend = 0.0
                cur += code
        if cur:
            out.append(String(bytes(cur)))
        if pend:
            out.append(round(-pend, 3))
        return out
    new = []
    for ii, (ops, op) in enumerate(instrs):
        if ii in drop:
            continue
        o = str(op); keys = [k for k in byel if k[0] == ii]
        if not keys:
            new.append((ops, op)); continue
        if o == 'TJ':
            arr = []
            for k, el in enumerate(ops[0]):
                if (ii, k) in byel: arr += rebuild(bytes(el), (ii, k))
                else: arr.append(el)
        else:
            if o == "'":
                new.append(([], Operator('T*')))
            if o == '"':
                new.append(([ops[0]], Operator('Tw'))); new.append(([ops[1]], Operator('Tc'))); new.append(([], Operator('T*')))
            arr = rebuild(bytes(ops[-1]), (ii, None))
        new.append(([Array(arr)], Operator('TJ')))
    tail = []
    for trm, tz, tr, col, pseud in draws:
        tail.append(([], Operator('q')))
        tail.append(col if col else ([0], Operator('g')))
        tail += [([], Operator('BT')), ([Name(font_name), 1], Operator('Tf')),
                 ([round(x, 5) for x in trm], Operator('Tm')), ([round(tz, 2)], Operator('Tz')),
                 ([3 if tr in (3, 7) else 0], Operator('Tr')),
                 ([String(pseud.encode('cp1252', 'replace'))], Operator('Tj')), ([], Operator('ET')), ([], Operator('Q'))]
    return [([], Operator('q'))] + new + [([], Operator('Q'))] + tail, boxes, inv


def inherited(o, key):
    d = 0
    while o is not None and d < 50:
        if key in o:
            return o[key]
        o = o.get('/Parent'); d += 1
    return None


def add_font(pdf, res, font_name='/HvDeid'):
    if '/Font' not in res:
        res['/Font'] = Dictionary()
    if font_name not in res['/Font']:
        res['/Font'][font_name] = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica,
                                                               Encoding=Name.WinAnsiEncoding))


class Unit:
    __slots__ = ('kind', 'holder', 'res', 'base', 'instrs', 'G', 'T', 'cmap')


def page_units(pdf, page, fonts_of, seen, max_depth=6):
    """Every content unit drawn by a page: the page, its form XObjects (once per document), annotation appearances.
    base maps unit space to page space."""
    units = []
    res = inherited(page.obj, '/Resources')
    if res is None:
        res = Dictionary(); page.obj['/Resources'] = res

    def do(kind, holder, res, base, depth):
        try:
            instrs = list(pikepdf.parse_content_stream(holder))
        except Exception:
            return False
        kids = []

        def on_do(name, ctm):
            try:
                xo = res.XObject[name]
                if xo.get('/Subtype') == '/Form' and xo.objgen not in seen:
                    seen.add(xo.objgen); m = [num(x) for x in xo.get('/Matrix', I6)]
                    kids.append((xo, mul(mul(m, ctm), base)))
            except Exception:
                pass
        u = Unit(); u.kind = kind; u.holder = holder; u.res = res; u.base = base; u.instrs = instrs
        u.G = interpret(instrs, fonts_of(res), on_do)
        u.T, u.cmap = build_text(u.G)
        units.append(u)
        if depth < max_depth:
            for xo, b in kids:
                r2 = xo.get('/Resources')
                do('form', xo, r2 if r2 is not None else res, b, depth + 1)
        return True
    ok = do('page', page, res, I6, 0)
    for a in (page.obj.get('/Annots') or []):
        try:
            ap = a.get('/AP'); n = ap.get('/N') if ap is not None else None
            cands = [n] if isinstance(n, pikepdf.Stream) else ([n[k] for k in n.keys()] if isinstance(n, Dictionary) else [])
            for xo in cands:
                if isinstance(xo, pikepdf.Stream) and xo.objgen not in seen:
                    seen.add(xo.objgen); r2 = xo.get('/Resources')
                    rect = [num(x) for x in a.get('/Rect', [0, 0, 0, 0])]
                    do('annot', xo, r2 if r2 is not None else res, [1, 0, 0, 1, rect[0], rect[1]], 0)
        except Exception:
            pass
    return units, ok


def write_unit(pdf, u, new_instrs):
    data = pikepdf.unparse_content_stream(new_instrs)
    if u.kind == 'page':
        u.holder.obj['/Contents'] = pdf.make_stream(data)
    else:
        u.holder.write(data)
    add_font(pdf, u.res)
