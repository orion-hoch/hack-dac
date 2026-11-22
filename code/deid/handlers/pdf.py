"""PDF handler (PIXELS builder). No PyMuPDF: pikepdf (MPL-2.0) only.

What it does, in one pikepdf session per file, then a full rewrite (drops stale revisions and unreferenced objects):
  1. Identity fields: every Info-like dictionary (keys matching IDENT_KEY), XMP creator-like elements and attributes,
     markup-annotation authors (/T), user folders in paths, UNC hosts, e-mail addresses -> mapping.sub_field.
     Their tokens of 4+ characters become per-file needles, replaced wherever else they occur in the file.
  2. Every string object, every XMP packet (typed or not), Name destinations: per-file needles + mapping.sub_text;
     file names inside strings follow the path plan (ctx.rename).
  3. Page text in pages, form XObjects and annotation appearances (deid/vision/pdftext.py): glyphs deleted with their
     advance kept, stand-in drawn in Helvetica. Sources of edits: mapping.sub_text, per-file needles, and the value next
     to a title-block label word (DRAWN BY: NJW) on pages that look like drawings.
  4. Deleted: /PieceInfo (private application data), /Thumb (page thumbnails). Embedded JPEGs: metadata segments
     stripped without re-encoding (deid/vision/imgmeta.py, if present). Logos by hash (deid/vision/logos.py, if present).
  5. Embedded files -> ctx.recurse.
Plain-function entry for other pipelines: deid_pdf(data, mapping, source, old_path) -> bytes.
"""
import io, os, re, time, math
import pikepdf
from pikepdf import Name, String, Dictionary, Array, Stream

from ..api import register, Result, Ctx
from ..vision import pdftext, pathrules

IDENT_KEY = re.compile(r'(?i)^/(author|authors|company|manager|owner|lastmodifiedby|lastsavedby|savedby|creatorname|'
                       r'drawnby|checkedby|approvedby|designedby|designer|engineer|checker|approver|operator|'
                       r'username|user|contact|e-?mail|phone|organi[sz]ation|department|customer|client)$')
INFO_HINT = ('/Author', '/Creator', '/Producer', '/Title', '/CreationDate', '/ModDate')
XMP_IDENT = re.compile(r'(?i)(creator|contributor|author|lastmodifiedby|owner|captionwriter|manager|company)$')
GENERIC = {'user', 'users', 'admin', 'administrator', 'owner', 'default', 'guest', 'unknown', 'none', 'solidworks',
           'autodesk', 'microsoft', 'windows', 'office', 'adobe', 'acrobat', 'word', 'excel', 'printer', 'print',
           'document', 'documents', 'drawing', 'drawings', 'engineering', 'design', 'corp', 'corporation', 'company',
           'gmbh', 'desktop', 'public', 'local', 'home', 'name', 'author', 'drawn', 'checked', 'approved', 'title'}
RX_USERDIR = re.compile(r'(?i)(?:\b[a-z]:[\\/]+(?:users|documents and settings)[\\/]+|(?<![\w.])/home/|(?<![\w.])/Users/)'
                        r'([^\\/:*?"<>|\r\n]{2,64})')
RX_UNC = re.compile(r'(?<![\\\w])\\\\([A-Za-z0-9_.$-]{2,63})\\')
RX_EMAIL = re.compile(r'[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}')
RX_URLHOST = re.compile(r'(?i)(?:\bhttps?://|(?<![\w.@-])(?=www\.))([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)')
RX_BAREDOMAIN = re.compile(r'(?i)(?<![\w@./:-])((?:[A-Za-z0-9-]{2,63}\.)+(?:com|org|net|edu|gov|mil|int|de|ch|uk|fr|it|es|nl|be|at|se|dk|no|fi|pl|cz|pt|ie|eu|io|ai|jp|cn|kr|tw|in|au|nz|ca|us|br|mx|ru|info|biz|tech|xyz))(?![\w-])')
RX_PHONE = re.compile(r'(?i)\b(?:tel(?:ephone|efon)?|phone|fax|telefax|mobile|mob|cell|t[ée]l[ée]phone|ph|fon)\.?[ \t]*(?:\([a-z]+\))?[ \t]*[:.]?[ \t]*(\+?\(?\d[\d \t()./-]{5,}\d)')
RX_ADDR_LABEL = re.compile(r'(?i)\b(?:address|adresse|anschrift)\b[ \t]*[:.]?[ \t]*([^\n]{4,80})')
RX_STREETS = [re.compile(r'\b\d{1,5}[ \t]+(?!(?:Stainless|Steel|Aluminum|Aluminium|Brass|Bronze|Copper|Carbon|Alloy|Plastic|Nylon|Delrin|Hex|Socket|Cap|Flat|Pan|Button)\b)(?:[A-Z][A-Za-z]+[ \t]+){0,3}(?:Street|St\.|Avenue|Ave\.?|Road|Rd\.?|Drive|Boulevard|Blvd\.?|Lane|Parkway|Pkwy\.?|Suite[ \t]+\d+)(?![A-Za-z])'),
              re.compile(r'\b[A-ZÄÖÜ][a-zäöüß]+(?:straße|strasse|str\.|weg|platz|allee|gasse)[ \t]+\d{1,4}[a-z]?\b'),
              re.compile(r'\b(?:D-|DE-|CH-|A-|F-)?\d{5}[ \t]+[A-ZÄÖÜ][a-zäöüß]{2,}(?:[ \t][A-ZÄÖÜ][a-zäöüß]+)?\b'),
              re.compile(r'\b[A-Z][a-z]+(?:[ \t][A-Z][a-z]+)?,[ \t]*[A-Z]{2}[ \t]+\d{5}(?:-\d{4})?\b')]
RX_AFFIL = re.compile(r'(?i)\b(universit\w*|institut\w*|laborator\w*|lab|college|school|department|dept|faculty|'
                      r'cent(?:er|re)|gmbh|inc|ltd|llc|corporation|hospital|academy|akademie|hochschule|fachhochschule|'
                      r'politecnico|polytechnique|ecole|école)\b')
AFFIL_KEEP = {'university', 'universität', 'universitat', 'université', 'institute', 'institut', 'laboratory', 'lab',
              'college', 'school', 'department', 'dept', 'faculty', 'center', 'centre', 'of', 'for', 'and', 'the', 'de',
              'engineering', 'mechanical', 'electrical', 'science', 'sciences', 'technology', 'research', 'physics',
              'inc', 'ltd', 'llc', 'gmbh', 'corporation', 'hospital', 'academy', 'national', 'applied', 'computer',
              'biomedical', 'robotics', 'state', 'medical', 'medicine', 'informatics', 'chemistry', 'biology'}
TITLE_WORDS = {'test', 'model', 'case', 'part', 'parts', 'drawing', 'figure', 'table', 'page', 'product', 'standard',
               'definitions', 'overview', 'specification', 'appendix', 'section', 'chapter', 'document', 'report',
               'project', 'release', 'notes', 'general', 'technical', 'engineering', 'introduction', 'kit', 'set',
               'series', 'edition', 'volume', 'measurement', 'quality', 'control', 'process', 'testing', 'tolerance',
               'tolerancing', 'dimensioning', 'geometric', 'mechanical', 'electrical', 'hardware', 'software', 'board',
               'a', 'an', 'the', 'for', 'of', 'with', 'in', 'on', 'to', 'using', 'design', 'system', 'systems', 'open',
               'source', 'low', 'cost', 'review', 'study', 'analysis', 'new', 'novel', 'based', 'via', 'from', 'manual',
               'guide', 'user', 'assembly', 'instructions', 'datasheet', 'data', 'sheet', 'version', 'rev', 'revision'}
NAME_JOIN = {'and', '&', 'und', 'et', 'y', 'e'}
NAME_PART = {'von', 'van', 'de', 'der', 'da', 'del', 'di', 'la', 'le', 'du', 'dos', 'den', 'ten', 'zu'}
INVISIBLE_PATHS = True     # remove drawn lettering under invisible text runs (item 4)
TB_SINGLETONS = True       # D8: one-off images inside the title block are replaced
GENERIC_HOSTS = {'github.com', 'gitlab.com', 'bitbucket.org', 'google.com', 'youtube.com', 'youtu.be', 'wikipedia.org',
                 'creativecommons.org', 'gnu.org', 'w3.org', 'iso.org', 'din.de', 'asme.org', 'adobe.com', 'microsoft.com',
                 'autodesk.com', 'solidworks.com', 'ptc.com', 'freecad.org', 'opensource.org', 'apache.org', 'ns.adobe.com',
                 'purl.org', 'xml.org', 'thingiverse.com', 'grabcad.com', 'onshape.com', 'archive.org'}
RX_FILE = re.compile(r'(?i)[\w\- .]{1,120}\.(?:slddrw|sldprt|sldasm|dwg|dxf|idw|ipt|iam|drw|prt|asm|catpart|catproduct|'
                     r'catdrawing|step|stp|igs|iges|pdf)\b')
# Title-block label words (English, German, French, Italian, Spanish) whose neighbour value is a person or company.
RX_LABEL = re.compile(r'(?i)^(drawn|drawnby|drn|dr|checked|checkedby|chk|chkd|approved|approvedby|appr|appd|designed|'
                      r'designedby|designer|engineer|eng|author|createdby|created|name|company|gez|gezeichnet|gepr|'
                      r'geprüft|geprueft|bearb|bearbeitet|ersteller|erstellt|genehmigt|freigabe|freigegeben|zeichner|konstrukteur|norm|dessiné|dessine|vérifié|verifie|disegnato|dibujado|'
                      r'revisado|controllato|by)[.:]*$')
TB_WORDS = re.compile(r'(?i)^(drawn|checked|approved|scale|sheet|title|dwg|rev|revision|material|size|weight|date|'
                      r'finish|tolerances?|massstab|maßstab|blatt|werkstoff|datum|échelle|echelle|feuille|scala|foglio|'
                      r'escala|hoja|unless|designed|projection|qty|part|benennung|bezeichnung|zeichnungsnummer|dept|department|abteilung|'
                      r'description|item|number|no|quantity)[.:]*$')
NOT_VALUE = re.compile(r'(?i)^([\d\W_]+|\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*|'
                       r'date|datum|signature|sign|name|title|sheet|scale|size|rev|n/?a|tbd|by|of|the|and|qty|mm|inch|'
                       r'\d+:\d+|[a-z]\d?)$')
MAX_PAGES_TEXT = 400        # page text beyond this many pages is left (deterministic budget)
MAX_GLYPHS_TEXT = 4_000_000
OCR_NO_TEXT = os.environ.get('DEID_PDF_OCR', '0') == '1'   # OCR chain for pages without text
OCR_PAGES = 2


def _looks_like_pdf(head):
    return head[:1024].find(b'%PDF-') >= 0


def _kind(head):
    h = head[:16].lstrip()
    for sig, k in ((b'PK\x03\x04', 'zip'), (b'<!DOCTYPE', 'html'), (b'<html', 'html'), (b'<HTML', 'html'),
                   (b'\x89PNG', 'png'), (b'\xff\xd8', 'jpeg'), (b'%!PS', 'postscript'), (b'{\\rtf', 'rtf'),
                   (b'\xd0\xcf\x11\xe0', 'ole'), (b'<?xml', 'xml')):
        if h.startswith(sig):
            return k
    return 'unknown'


class Subst:
    """Same-length substitutions for one file. Identity values -> mapping.sub_field; everything else via needles and
    mapping.sub_text. All transforms are same length so page-text edits can be diffed character by character."""

    def __init__(self, mapping, source, ctx=None):
        self.m = mapping; self.source = source; self.ctx = ctx
        self.needles = set(); self._rx = None; self.n = 0

    def field(self, v):
        if not v or not v.strip():
            return v
        self.add_needles(v)
        out = self.m.sub_field(v, self.source)
        if out != v:
            self.n += 1
        return out

    def add_needles(self, v):
        for tok in re.split(r'[^0-9A-Za-zÀ-ɏ]+', v):
            if len(tok) >= 4 and not tok.isdigit() and tok.lower() not in GENERIC:
                if tok.lower() not in self.needles:
                    self.needles.add(tok.lower()); self._rx = None
        v = v.strip()
        if len(v) >= 4 and v.lower() not in GENERIC and v.lower() not in self.needles:
            self.needles.add(v.lower()); self._rx = None

    def drawing_text(self, s):
        """Drawing pages only: street lines and postcode + city (title-block addresses)."""
        for rx in RX_STREETS:
            s = rx.sub(lambda m: self.m.sub_field(m.group(0), self.source), s)
        return s

    def _host(self, m):
        """D10 default: the organisation part of a web address is an identity. Keep scheme, 'www.' and the top-level
        domain; generic hosts (code hosting, standards, licences, big vendors) stay."""
        host = m.group(1); core = host[4:] if host.lower().startswith('www.') else host
        if core.lower() in GENERIC_HOSTS or '.'.join(core.lower().split('.')[-2:]) in GENERIC_HOSTS:
            return m.group(0)
        labels = core.split('.')
        new = '.'.join([self.m.sub_field(x, self.source) if len(x) >= 3 else x for x in labels[:-1]] + labels[-1:])
        return m.group(0)[:m.start(1) - m.start(0)] + host[:len(host) - len(core)] + new

    def _needle_rx(self):
        if self._rx is None:
            keys = sorted(self.needles, key=len, reverse=True)
            self._rx = re.compile(r'(?<![0-9A-Za-z])(' + '|'.join(map(re.escape, keys)) + r')(?![0-9A-Za-z])', re.I) \
                if keys else False
        return self._rx

    def text(self, s):
        """Free text, same length. Returns new text."""
        if not s:
            return s
        s0 = s
        f = lambda m: m.group(0)[:m.start(1) - m.start(0)] + self.m.sub_field(m.group(1), self.source) + m.group(0)[m.end(1) - m.start(0):]
        s = RX_USERDIR.sub(f, s)
        s = RX_UNC.sub(f, s)
        def email(m):
            local, dom = m.group(0).split('@', 1); labels = dom.split('.')
            return (self.m.sub_field(local, self.source) + '@' +
                    '.'.join([self.m.sub_field(x, self.source) for x in labels[:-1]] + labels[-1:]))
        s = RX_EMAIL.sub(email, s)
        s = RX_URLHOST.sub(self._host, s)
        s = RX_PHONE.sub(lambda m: m.group(0)[:m.start(1) - m.start(0)] + self.m.sub_field(m.group(1), self.source), s)
        s = RX_ADDR_LABEL.sub(lambda m: m.group(0)[:m.start(1) - m.start(0)] + self.m.sub_field(m.group(1), self.source), s)
        s = RX_BAREDOMAIN.sub(self._host, s)
        rx = self._needle_rx()
        if rx:
            s = rx.sub(lambda m: self.m.sub_field(m.group(0), self.source), s)
        s, k = self.m.sub_text(s, self.source)
        if s != s0:
            self.n += 1
        return s

    def meta(self, s):
        """Metadata string: free text plus file names through the path plan (length may change)."""
        s2 = self.text(s)
        if self.ctx is not None and RX_FILE.search(s2):
            def ren(m):
                name = m.group(0).strip()
                try:
                    new = self.ctx.rename(name)
                except Exception:
                    new = name
                return m.group(0).replace(name, new) if new and new != name else m.group(0)
            s2 = RX_FILE.sub(ren, s2)
        return s2


def _xml_text_sub(xml, sub):
    """Apply sub.text to XML character data and attribute values only, never inside tags or entities;
    identity elements and attributes go through sub.field."""
    def chardata(t, ident):
        parts = re.split(r'(&[#\w]+;)', t)
        return ''.join(p if p.startswith('&') and p.endswith(';') else (sub.field(p) if ident and p.strip() else sub.text(p))
                       for p in parts)
    # attributes:  prefix:Name="value"
    def attr(m):
        name, val = m.group(1), m.group(2)
        ident = bool(XMP_IDENT.search(name.split(':')[-1]))
        return '%s="%s"' % (name, chardata(val, ident))
    out = []; pos = 0; stack = []
    for m in re.finditer(r'<[^>]*>', xml):
        txt = xml[pos:m.start()]
        ident = any(XMP_IDENT.search(t.split(':')[-1]) for t in stack)
        out.append(chardata(txt, ident) if txt else txt)
        tag = m.group(0)
        if tag.startswith('</'):
            if stack: stack.pop()
        elif not tag.startswith(('<?', '<!')) and not tag.endswith('/>'):
            stack.append(re.match(r'<\s*([^\s>/]+)', tag).group(1) if re.match(r'<\s*([^\s>/]+)', tag) else '')
        out.append(re.sub(r'([\w.-]+:[\w.-]+)="([^"]*)"', attr, tag) if '="' in tag else tag)
        pos = m.end()
    out.append(chardata(xml[pos:], False))
    return ''.join(out)


def _edit_xmp(stream, sub, st):
    try:
        raw = stream.read_bytes()
    except Exception:
        st['xmp_unreadable'] += 1; return False
    enc = 'utf-8'
    if raw[:2] in (b'\xfe\xff', b'\xff\xfe'):
        enc = 'utf-16'
    try:
        xml = raw.decode(enc)
    except Exception:
        xml = raw.decode('latin-1'); enc = 'latin-1'
    new = _xml_text_sub(xml, sub)
    if new == xml:
        return False
    stream.write(new.encode(enc if enc != 'utf-16' else 'utf-16'))
    st['xmp_edited'] += 1
    return True


def _pdfstr(o):
    """pikepdf String -> python text and a writer back."""
    return str(o)


def _walk_strings(pdf, sub, st):
    markup = {'/Text', '/FreeText', '/Line', '/Square', '/Circle', '/Polygon', '/PolyLine', '/Highlight', '/Underline',
              '/Squiggly', '/StrikeOut', '/Stamp', '/Caret', '/Ink', '/FileAttachment', '/Sound', '/Redact'}

    def fix(v, ident):
        try:
            s = str(v)
        except Exception:
            return None
        s2 = sub.field(s) if ident else sub.meta(s)
        return None if s2 == s else String(s2)

    def walk(o, depth, info_like, keep_strings=False):
        if depth > 40:
            return
        if isinstance(o, (Dictionary, Stream)):
            keys = list(o.keys())
            is_info = info_like or any(k in keys for k in INFO_HINT) and '/Type' not in keys
            is_markup = str(o.get('/Subtype', '')) in markup if '/Subtype' in keys else False
            for k in keys:
                if k == '/PieceInfo':
                    del o[k]; st['pieceinfo'] += 1; continue
                if k == '/Thumb':
                    del o[k]; st['thumb'] += 1; continue
                v = o[k]
                if isinstance(v, String) and k in ('/D', '/Dest', '/S'):
                    continue                      # string destinations must keep matching the sorted name tree
                if isinstance(v, String):
                    ident = (is_info and bool(IDENT_KEY.match(k))) or (is_markup and k == '/T')
                    nv = fix(v, ident)
                    if nv is not None:
                        o[k] = nv; st['strings'] += 1
                elif isinstance(v, Name) and k in ('/D', '/Dest'):
                    nn = _fix_name(v, sub)
                    if nn is not None:
                        o[k] = nn; st['names'] += 1
                elif isinstance(v, (Dictionary, Array)) and not v.is_indirect:
                    walk(v, depth + 1, False, k in ('/Names', '/Limits', '/D', '/Dest'))
                elif isinstance(v, Array) and k in ('/Names', '/Limits'):
                    pass
        elif isinstance(o, Array):
            for i in range(len(o)):
                v = o[i]
                if isinstance(v, String) and not keep_strings:
                    nv = fix(v, False)
                    if nv is not None:
                        o[i] = nv; st['strings'] += 1
                elif isinstance(v, (Dictionary, Array)) and not v.is_indirect:
                    walk(v, depth + 1, False)
    # Info first so its values become needles before the rest of the walk
    done = set()
    try:
        info = pdf.trailer.get('/Info')
        if info is not None:
            walk(info, 0, True)
            if info.is_indirect:
                done.add(info.objgen)
    except Exception:
        st['err_info'] += 1
    for o in pdf.objects:
        try:
            if isinstance(o, (Dictionary, Stream, Array)) and o.objgen not in done:
                done.add(o.objgen)
                walk(o, 0, False)
        except Exception:
            st['err_walk'] += 1
    # Name-keyed destinations in the catalog
    try:
        dests = pdf.Root.get('/Dests')
        if isinstance(dests, Dictionary):
            for k in list(dests.keys()):
                nn = _fix_name(Name(k), sub)
                if nn is not None:
                    dests[str(nn)] = dests[k]; del dests[k]; st['names'] += 1
    except Exception:
        st['err_dests'] += 1


def _fix_name(n, sub):
    s = str(n)[1:]
    s2 = sub.text(s)
    return Name('/' + s2) if s2 != s and s2 else None


def _xmp_streams(pdf):
    seen = set(); out = []
    for o in pdf.objects:
        try:
            if isinstance(o, (Dictionary, Stream)):
                md = o.get('/Metadata')
                if isinstance(md, Stream) and md.objgen not in seen:
                    seen.add(md.objgen); out.append(md)
            if isinstance(o, Stream) and o.objgen not in seen:
                if o.get('/Type') == '/Metadata' or o.get('/Subtype') == '/XML':
                    seen.add(o.objgen); out.append(o); continue
                if o.get('/Subtype') in ('/Image', '/Form') or '/Length1' in o or '/Length2' in o:
                    continue
                if int(o.get('/Length', 0)) > 2_000_000:
                    continue
                head = o.read_bytes()[:512]
                if b'<?xpacket' in head or b'xmpmeta' in head:
                    seen.add(o.objgen); out.append(o)
        except Exception:
            continue
    return out


def _embedded(pdf, ctx, st):
    for o in list(pdf.objects):
        try:
            if not isinstance(o, Dictionary) or '/EF' not in o:
                continue
            name = str(o.get('/UF', o.get('/F', 'attachment')))
            ef = o.EF
            for key in ('/F', '/UF'):
                s = ef.get(key)
                if not isinstance(s, Stream):
                    continue
                if ctx is None or ctx.recurse is None:
                    st['embedded_not_recursed'] += 1; continue
                r = ctx.recurse(s.read_bytes(), ctx.old_path + '!/' + name)
                st['embedded'] += 1
                if r is not None and r.data is not None:
                    s.write(r.data); st['embedded_changed'] += 1
                    for p in ('/Params',):
                        if p in s and '/CheckSum' in s[p]:
                            del s[p]['/CheckSum']
            for key in ('/F', '/UF'):
                if key in o and ctx is not None:
                    try:
                        nn = ctx.rename(str(o[key]))
                        if nn and nn != str(o[key]):
                            o[key] = String(nn)
                    except Exception:
                        pass
        except Exception:
            st['err_embedded'] += 1


def _jpeg_strip(pdf, st):
    try:
        from ..vision import imgmeta
    except Exception:
        st['jpeg_strip_unavailable'] += 1; return
    for o in pdf.objects:
        try:
            if isinstance(o, Stream) and o.get('/Subtype') == '/Image':
                f = o.get('/Filter')
                if f == '/DCTDecode' or (isinstance(f, Array) and len(f) == 1 and f[0] == '/DCTDecode'):
                    raw = o.read_raw_bytes()
                    new, removed = imgmeta.strip_jpeg(raw)
                    if removed and new != raw:
                        o.write(new, filter=Name.DCTDecode, decode_parms=o.get('/DecodeParms'))
                        st['jpeg_stripped'] += 1
        except Exception:
            st['err_jpeg'] += 1


# ---------------------------------------------------------------- page text
def _words(units):
    """Words with page-space boxes: list of dict(text, x0, y0, x1, y1, h, unit, gis)."""
    W = []
    for ui, u in enumerate(units):
        cur = []; txt = []
        def flush():
            if cur:
                xs = []; ys = []; hs = []
                for gi in cur:
                    g = u.G[gi]; t = pdftext.mul(g['trm'], u.base)
                    h = (t[2] ** 2 + t[3] ** 2) ** 0.5
                    p0 = (t[4], t[5]); p1 = pdftext.apply(u.base, *g['nxt'])
                    xs += [p0[0], p1[0]]; ys += [p0[1], p0[1] + h]; hs.append(h)
                t0m = pdftext.mul(u.G[cur[0]]['trm'], u.base)
                ang = int(round(math.degrees(math.atan2(t0m[1], t0m[0])) / 90.0)) % 4
                W.append({'text': ''.join(txt), 'pts': list(zip(xs, ys)), 'h': max(hs) or 1.0, 'unit': ui,
                          'gis': list(cur), 'ang': ang, 'exact': abs(t0m[1] * t0m[0]) < 1e-6})
        for ch, gi in zip(u.T, u.cmap):
            if gi < 0 or ch.isspace():
                flush(); cur = []; txt = []
            else:
                if not cur or cur[-1] != gi:
                    cur.append(gi)
                txt.append(ch)
        flush()
    # turn the page so that the most common text direction reads left to right, then box every word in that frame
    from collections import Counter
    dom = Counter(w['ang'] for w in W).most_common(1)[0][0] if W else 0
    rot = {0: lambda x, y: (x, y), 1: lambda x, y: (y, -x), 2: lambda x, y: (-x, -y), 3: lambda x, y: (-y, x)}[dom]
    for w in W:
        pts = w.pop('pts'); w['ub'] = (min(x for x, y in pts), min(y for x, y in pts), max(x for x, y in pts), max(y for x, y in pts))
        q = [rot(x, y) for x, y in pts]
        w['x0'] = min(a for a, b in q); w['x1'] = max(a for a, b in q)
        w['y0'] = min(b for a, b in q); w['y1'] = max(b for a, b in q)
        w['upright'] = w['ang'] == dom and w['exact']
    return W


def _label_values(W):
    """Values next to title-block label words: right of the label on the same line, else the line below.
    Returns list of word indices. Only called on pages that look like drawings."""
    out = []
    for i, w in enumerate(W):
        t = w['text']
        if not RX_LABEL.match(t) or not w['upright']:
            continue
        h = w['h']; cy = (w['y0'] + w['y1']) / 2
        right = [j for j, v in enumerate(W) if j != i and v['upright'] and abs((v['y0'] + v['y1']) / 2 - cy) < 0.45 * h
                 and 0 <= v['x0'] - w['x1'] < 12 * h]
        right.sort(key=lambda j: W[j]['x0'])
        vals = []; last = w['x1']
        for j in right:
            v = W[j]
            gap = v['x0'] - last
            if (vals and gap > 2.0 * h) or (not vals and gap > 12 * h):
                break
            if RX_LABEL.match(v['text']) and not vals:
                last = v['x1']; continue          # "DRAWN BY"
            if TB_WORDS.match(v['text']) or RX_LABEL.match(v['text']):
                break
            vals.append(j); last = v['x1']
            if len(vals) >= 4:
                break
        if not vals:
            below = [j for j, v in enumerate(W) if j != i and v['upright'] and 0 < w['y0'] - v['y1'] < 2.2 * h
                     and v['x0'] < w['x1'] + 2 * h and v['x1'] > w['x0'] - 2 * h]
            below.sort(key=lambda j: (-W[j]['y1'], W[j]['x0']))
            if below:
                y = W[below[0]]['y1']
                vals = [j for j in below if abs(W[j]['y1'] - y) < 0.3 * h and not TB_WORDS.match(W[j]['text'])
                        and not RX_LABEL.match(W[j]['text'])][:3]
        for j in vals:
            if not NOT_VALUE.match(W[j]['text']) and re.search(r'[A-Za-z]', W[j]['text']):
                out.append(j)
    return out


def _lines(W, idx):
    idx = sorted(idx, key=lambda i: (-W[i]['y0'], W[i]['x0'])); lines = []
    for i in idx:
        if lines and abs(W[lines[-1][0]]['y0'] - W[i]['y0']) < 0.5 * W[i]['h']:
            lines[-1].append(i)
        else:
            lines.append([i])
    return [sorted(l, key=lambda i: W[i]['x0']) for l in lines]


def _is_name_line(toks):
    names = 0; run = 0; total = 0
    for t in toks:
        tl = t.lower()
        if tl in NAME_JOIN or not re.search(r'[A-Za-z]', t):
            run = 0; continue
        if tl in TITLE_WORDS:
            return False
        if tl in NAME_PART:
            continue
        if not re.match(r"^[A-ZÀ-Ý][a-zà-ÿ'’-]*\.?$|^[A-Z]\.(?:[A-Z]\.)*$", t):
            return False
        run += 1; total += 1
        if run == 2:
            names += 1
    return names >= 1 and total <= 30


def _author_block(W):
    """Documents, page 1 (D10 upgrade): word indexes of person names and affiliation proper nouns in the lines between
    the title (largest text in the top 45%) and the first heading or paragraph. Conservative by construction."""
    up = [i for i, w in enumerate(W) if w['upright']]
    if len(up) < 20:
        return [], []
    top = max(W[i]['y1'] for i in up); bot = min(W[i]['y0'] for i in up); H = (top - bot) or 1.0
    head = [i for i in up if W[i]['y0'] > top - 0.45 * H]
    if not head:
        return [], []
    hmax = max(W[i]['h'] for i in head)
    title = {i for i in head if W[i]['h'] >= 0.85 * hmax}
    ty = min(W[i]['y0'] for i in title)
    below = [i for i in head if i not in title and W[i]['y1'] <= ty + 0.2 * hmax]
    names, affs = [], []; cand = []; support = False
    for line in _lines(W, below)[:10]:
        toks = [W[i]['text'] for i in line]
        txt = ' '.join(toks)
        if re.match(r'(?i)^\W*(abstract|introduction|keywords?|summary|contents|table of contents|1\.?\s)', txt) or len(line) > 25:
            break
        clean = [re.sub(r"^[^A-Za-zÀ-ÿ]+|[^A-Za-zÀ-ÿ.'’-]+$", '', t) for t in toks]
        if RX_AFFIL.search(txt):
            for i, t in zip(line, clean):
                if t and t[0].isupper() and t.lower().strip('.') not in AFFIL_KEEP and len(t) >= 3:
                    affs.append(i)
        if RX_AFFIL.search(txt) or RX_EMAIL.search(txt):
            support = True
        if not RX_AFFIL.search(txt) and _is_name_line([t for t in clean if t]):
            multi = bool(re.search(r',|\band\b|&|\bund\b', txt))
            idx = [i for i, t in zip(line, clean) if t and t[0].isupper() and t.lower() not in NAME_PART | NAME_JOIN
                   and len(re.sub(r'[^A-Za-zÀ-ÿ]', '', t)) >= 2]
            cand.append((idx, multi))
    for idx, multi in cand:
        if multi or support:
            names += idx
    return names, affs


def _titleblock_box(W, page):
    """Page-space box (unrotated) around the densest cluster of title-block keywords, or None."""
    kw = [w for w in W if TB_WORDS.match(w['text']) or RX_LABEL.match(w['text'])]
    if len(kw) < 3:
        return None
    try:
        mb = [float(x) for x in page.mediabox]
        R = 0.22 * ((mb[2] - mb[0]) ** 2 + (mb[3] - mb[1]) ** 2) ** 0.5
    except Exception:
        return None
    c = [((w['ub'][0] + w['ub'][2]) / 2, (w['ub'][1] + w['ub'][3]) / 2) for w in kw]
    best = max(range(len(kw)), key=lambda i: sum(1 for j in range(len(kw)) if (c[i][0] - c[j][0]) ** 2 + (c[i][1] - c[j][1]) ** 2 < R * R))
    cl = [kw[j] for j in range(len(kw)) if (c[best][0] - c[j][0]) ** 2 + (c[best][1] - c[j][1]) ** 2 < R * R]
    if len(cl) < 3:
        return None
    hm = sorted(w['h'] for w in cl)[len(cl) // 2]; pad = 2.5 * hm
    return (min(w['ub'][0] for w in cl) - pad, min(w['ub'][1] for w in cl) - pad,
            max(w['ub'][2] for w in cl) + pad, max(w['ub'][3] for w in cl) + pad)


def _run_box(u, gis, pad=0.15):
    xs = []; ys = []; hh = 0.0
    for gi in gis:
        g = u.G[gi]; t = pdftext.mul(g['trm'], u.base); h = (t[2] ** 2 + t[3] ** 2) ** 0.5; hh = max(hh, h)
        nx = pdftext.apply(u.base, *g['nxt'])
        for (x, y) in ((t[4], t[5]), (t[4] + t[2], t[5] + t[3]), nx, (nx[0] + t[2], nx[1] + t[3])):
            xs.append(x); ys.append(y)
    p = pad * hh
    return (min(xs) - p, min(ys) - p, max(xs) + p, max(ys) + p)


def _is_drawing_page(W):
    kws = {w['text'].lower().strip('.:') for w in W if TB_WORDS.match(w['text']) or RX_LABEL.match(w['text'])}
    return len(kws) >= 3 and len(W) < 4000


def _page_text(pdf, sub, st):
    fcache = {}; seen = set()

    def fonts_of(res):
        out = {}
        fd = res.get('/Font') if res is not None else None
        if fd is None:
            return out
        for name in fd.keys():
            try:
                f = fd[name]; key = f.objgen if f.is_indirect else id(f)
                if key not in fcache:
                    fcache[key] = pdftext.Font(f)
                    if fcache[key].type0 and not fcache[key].tou:
                        st['font_type0_no_tounicode'] += 1
                out[str(name)] = fcache[key]
            except Exception:
                st['err_font'] += 1
        return out
    words_p1 = 0; drawing = False
    for pno, page in enumerate(pdf.pages):
        if pno >= MAX_PAGES_TEXT or st['glyphs'] > MAX_GLYPHS_TEXT:
            st['pages_skipped_time'] += len(pdf.pages) - pno; break
        try:
            units, ok = pdftext.page_units(pdf, page, fonts_of, seen)
        except Exception:
            st['err_page'] += 1; continue
        if not ok:
            st['parse_fail'] += 1
        st['pages'] += 1
        st['glyphs'] += sum(len(u.G) for u in units)
        st['glyphs_undecoded'] += sum(1 for u in units for g in u.G if g['u'] is None)
        W = _words(units)
        if pno == 0:
            words_p1 = len(W)
        st['_wpage'][pno] = len(W)
        forced = {}                                  # (unit, glyph) -> new char(s): label values, author block

        def force(j, letters_only=False):
            w = W[j]; new = sub.m.sub_field(w['text'], sub.source)
            if new == w['text']:
                return False
            u = units[w['unit']]; k = 0
            for gi in w['gis']:
                old = u.G[gi]['u'] or '\ufffd'; nch = len(old); nw = new[k:k + nch]
                if letters_only:
                    nw = ''.join(b if a.isalpha() else a for a, b in zip(old, nw))
                forced[(w['unit'], gi)] = nw; k += nch
            return True
        is_dwg = _is_drawing_page(W)
        if is_dwg:
            drawing = drawing or pno == 0
            st['drawing_pages'] += 1
            for j in _label_values(W):
                if force(j):
                    st['label_values'] += 1
                    if len(W[j]['text']) >= 4:
                        sub.add_needles(W[j]['text'])
            if TB_SINGLETONS and pno < 3:
                box = _titleblock_box(W, page)
                if box is not None:
                    try:
                        from ..vision import logos as _logos
                        n, _ = _logos.replace_titleblock_singletons(pdf, pno, box)
                        st['tb_singletons'] += n
                    except Exception:
                        st['err_tb_singletons'] += 1
        elif pno == 0:
            names, affs = _author_block(W)
            for j in names:
                if force(j, letters_only=True):
                    st['author_names'] += 1
                    t = re.sub(r'[^A-Za-zÀ-ÿ]', '', W[j]['text'])
                    if len(t) >= 4:
                        sub.add_needles(t)
            for j in affs:
                st['author_affil'] += bool(force(j, letters_only=True))
        # plan: runs per unit; invisible runs over drawn lettering get their letter shapes removed (page unit only)
        plan = []; all_dele = []; ppaths = None
        for ui, u in enumerate(units):
            if not u.G:
                continue
            T2 = sub.text(u.T)
            if is_dwg:
                T2 = sub.drawing_text(T2)
            if len(T2) != len(u.T):
                st['err_length'] += 1; continue
            if any(k[0] == ui for k in forced):
                L = list(T2); first = {}
                for i, gi in enumerate(u.cmap):
                    if gi >= 0 and gi not in first:
                        first[gi] = i
                for i, gi in enumerate(u.cmap):
                    if gi >= 0 and (ui, gi) in forced:
                        f = forced[(ui, gi)]; off = i - first[gi]
                        if off < len(f):
                            L[i] = f[off]
                T2 = ''.join(L)
            if T2 == u.T:
                continue
            runs = pdftext.runs_from_diff(u.T, T2, u.cmap)
            if not runs:
                continue
            vis = set()
            if INVISIBLE_PATHS and units[0].kind == 'page':
                for ri, (gis, pseud) in enumerate(runs):
                    if u.G[gis[0]]['tr'] not in (3, 7):
                        continue
                    if ppaths is None:
                        ppaths = pathrules.page_paths(units[0].instrs)
                    box = _run_box(u, gis)
                    dele = pathrules.glyph_shapes(ppaths, box) if ppaths else []
                    if dele and len(dele) <= 8 * max(1, len(pseud)):
                        vis.add(ri); all_dele += dele; st['invisible_shapes_removed'] += len(dele)
                        st['_boxes'].setdefault(pno, []).append([(box[0], box[1]), (box[2], box[1]), (box[0], box[3]), (box[2], box[3])])
                    else:
                        st['invisible_no_shapes'] += 1
            plan.append((ui, runs, vis))
        drop = pathrules.kill_set(all_dele) if all_dele else set()
        if drop and not any(ui == 0 for ui, _, _ in plan):
            plan.insert(0, (0, [], set()))
        for ui, runs, vis in plan:
            u = units[ui]
            try:
                new, boxes, inv = pdftext.edit_unit(u.instrs, u.G, runs, visible=vis, drop=drop if ui == 0 else ())
                if new is None:
                    continue
                pdftext.write_unit(pdf, u, new)
                st['text_runs'] += len(runs); st['edited_' + u.kind] += 1; st['invisible_runs'] += inv
                st['invisible_made_visible'] += len(vis)
                st['_boxes'].setdefault(pno, []).extend(
                    [[pdftext.apply(u.base, x, y) for x, y in b] for b in boxes])
            except Exception:
                st['err_edit'] += 1
    st['words_p1'] = words_p1; st['drawing'] = int(drawing)


def _no_text_pages(pdf, data, sub, st):
    """Pages with under 10 text words among the first OCR_PAGES: OCR chain (deid/vision/notext.py)."""
    try:
        from ..vision import notext
    except Exception:
        st['ocr_unavailable'] = 1; return
    for pno in range(min(OCR_PAGES, len(pdf.pages))):
        if st['_wpage'].get(pno, 0) >= 10:
            continue
        try:
            r = notext.process_page(pdf, data, pno, sub.m, sub.source, sorted(sub.needles))
            st['ocr_pages'] += 1
            st['ocr_drawings'] += r.get('gate') == 'drawing'
            st['ocr_hits'] += len(r.get('hits', []))
            st['ocr_edits'] += (r.get('drawn') or 0) + (r.get('painted') or 0)
            st['_ocr'].append({k: v for k, v in r.items() if k != 'keywords'})
            for old, new, _q in r.get('hits', []):
                if len(old) >= 4:
                    sub.add_needles(old)
        except Exception as e:
            st['err_ocr'] += 1


# ---------------------------------------------------------------- entry points
class _Counter(dict):
    def __missing__(self, k):
        if k in ('_boxes', '_wpage', '_ocr'):
            self[k] = {} if k != '_ocr' else []; return self[k]
        return 0


def process(data, ctx, page_text=True, logos=True):
    """-> (Result, stats). stats['_boxes'] = {page: [quad in page space]} of text edits (for tests)."""
    st = _Counter()
    if not _looks_like_pdf(data[:1024]):
        return Result(None, status='unchanged', notes=['not a PDF (%s); copied unchanged' % _kind(data[:16])]), st
    t0 = time.monotonic()
    try:
        pdf = pikepdf.open(io.BytesIO(data))
    except pikepdf.PasswordError:
        return Result(None, status='failed', notes=['encrypted PDF, password required; copied unchanged']), st
    except Exception as e:
        return Result(None, status='failed', notes=['pikepdf cannot open: %s' % str(e)[:80]]), st
    try:
        sub = Subst(ctx.mapping, ctx.source, ctx)
        n_pages = len(pdf.pages)
        if pdf.is_encrypted:
            st['was_encrypted'] = 1
        # Info first (harvests needles), then page text (harvests label values), then everything else.
        info = pdf.trailer.get('/Info')
        if isinstance(info, Dictionary):
            for k in list(info.keys()):
                v = info[k]
                if isinstance(v, String) and IDENT_KEY.match(k):
                    sub.add_needles(str(v))
        for md in _xmp_streams(pdf):
            try:
                xml = md.read_bytes().decode('utf-8', 'replace')
                for m in re.finditer(r'<([\w.-]+:[\w.-]+)[^>]*>(.*?)</\1>', xml, re.S):
                    if XMP_IDENT.search(m.group(1).split(':')[-1]):
                        for t in re.findall(r'>([^<>]+)<', '>' + m.group(2) + '<'):
                            if t.strip():
                                sub.add_needles(t)
            except Exception:
                pass
        if page_text:
            _page_text(pdf, sub, st)
        if page_text and OCR_NO_TEXT:
            _no_text_pages(pdf, data, sub, st)
        _walk_strings(pdf, sub, st)
        for md in _xmp_streams(pdf):
            _edit_xmp(md, sub, st)
        _embedded(pdf, ctx, st)
        _jpeg_strip(pdf, st)
        if logos:
            try:
                from ..vision import vlogo as _vlogo
                st['vector_logos'] += _vlogo.remove_vector_logos(pdf)
            except Exception:
                st['err_vector_logos'] += 1
        if logos:
            try:
                from ..vision import logos as _logos
                n, _ = _logos.replace_logos(pdf)
                st['logos'] += n
            except ImportError:
                st['logos_unavailable'] = 1
            except Exception:
                st['err_logos'] += 1
        changed = (st['strings'] + st['names'] + st['xmp_edited'] + st['pieceinfo'] + st['thumb'] + st['text_runs'] + st['ocr_edits'] + st['tb_singletons'] + st['vector_logos']
                   + st['embedded_changed'] + st['jpeg_stripped'] + st['logos'])
        stale = data.count(b'%%EOF') > 1 or st.get('was_encrypted')
        if not changed and not stale:
            return Result(None, status='unchanged', changed=0), st
        out = io.BytesIO()
        pdf.save(out, deterministic_id=True, fix_metadata_version=False)
        outb = out.getvalue()
    except Exception as e:
        return Result(None, status='failed', notes=['pdf handler error: %s: %s' % (type(e).__name__, str(e)[:80])]), st
    finally:
        try:
            pdf.close()
        except Exception:
            pass
    try:
        with pikepdf.open(io.BytesIO(outb)) as chk:
            if len(chk.pages) != n_pages:
                return Result(None, status='failed', notes=['page count changed on rewrite']), st
    except Exception as e:
        return Result(None, status='failed', notes=['output does not re-open: %s' % str(e)[:60]]), st
    notes = []
    if st['pages_skipped_time']:
        notes.append('page text not processed on %d pages (size budget)' % st['pages_skipped_time'])
    if st['font_type0_no_tounicode']:
        notes.append('%d fonts without ToUnicode: their text cannot be matched' % st['font_type0_no_tounicode'])
    if st['invisible_runs']:
        notes.append('%d replaced runs were invisible text over drawn lettering' % st['invisible_runs'])
    status = 'partial' if st['pages_skipped_time'] else 'ok'
    return Result(outb, changed=changed, status=status, notes=notes), st


@register("pdf", lambda p, h: p.endswith('.pdf') or p.endswith('.ai') and _looks_like_pdf(h) or h[:5] == b'%PDF-', priority=55)
def handle(data: bytes, ctx: Ctx) -> Result:
    try:
        return process(data, ctx)[0]
    except Exception as e:      # never raise
        return Result(None, status='failed', notes=['pdf handler crashed: %s' % type(e).__name__])


def deid_pdf(data: bytes, mapping=None, source: str = '', old_path: str = 'file.pdf', rename=None, recurse=None) -> bytes:
    """Plain function for other pipelines: bytes in, bytes out (the input itself when nothing changed or on failure)."""
    if mapping is None:
        from ..mapping import Mapping
        mapping = Mapping()
    ctx = Ctx(old_path=old_path, new_path=old_path, source=source, mapping=mapping,
              rename=rename or (lambda s: s), recurse=recurse or (lambda b, p: Result(None, status='unchanged')))
    r = handle(data, ctx)
    return r.data if r.data is not None else data
