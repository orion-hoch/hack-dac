"""Per-format parsing shared by truth building and scoring.

analyze(path, rel) returns a Facts dict:
  kind, ext_full, size, sha1, opens, open_err, struct (dict of counts), geom (str|None),
  chunks (list of [key, hash]), slots (identity-bearing fields), eng (engineering tokens),
  refs (references to other files), bundle (list of [category, chunk_key, text]).
Bundle categories match the report categories in docs/scorer-model.md.
"""
import gzip, hashlib, io, os, re, struct as st, tarfile, uuid, zipfile, zlib
import xml.etree.ElementTree as ET

import olefile
try:  # V2: PyMuPDF is AGPL; optional, used only by the local scorer and the legacy PDF path
    import pymupdf
except ImportError:
    pymupdf = None
from PIL import Image

pymupdf.TOOLS.mupdf_display_errors(False) if pymupdf else None

MAX_PARSE = 300 * 1024 * 1024      # don't deep-parse files bigger than this
MAX_TEXT = 1024 * 1024              # cap text kept per bundle entry
SW_EXT = {'.sldprt', '.sldasm', '.slddrw', '.slddrt', '.sldlfp', '.prtdot', '.asmdot', '.drwdot', '.sldblk'}
NATIVE_ZIP_EXT = {'.fcstd', '.3mf', '.f3d', '.f3z', '.odg', '.docx', '.xlsx', '.pptx', '.ods', '.odt', '.kmz'}
OLE_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
REF_SW = re.compile(r'([^\\/:*?"<>|@\x00]{1,160}\.(?:sldprt|sldasm|slddrw))\b', re.I)
REF_OLE = re.compile(r'([^\\/:*?"<>|\x00]{1,160}\.(?:ipt|iam|idw|ipn|dwg|sldprt|sldasm|slddrw|par|psm|asm|dft|prt))\b', re.I)
SW_PROP = re.compile(rb'<property[^>]{0,300}?name="([^"]{1,200})"[^>]{0,300}>\s{0,40}<vt:\w{1,20}>([^<]{0,4000})<')
SW_CORE = re.compile(rb'<(cp:lastModifiedBy|dc:creator|Company|Manager)>([^<]{0,400})<')
NUM = re.compile(rb'[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eEdD][-+]?\d+)?')


def h(b):
    if isinstance(b, str):
        b = b.encode('utf-8', 'surrogatepass')
    return hashlib.sha1(b).hexdigest()[:16]


def ext_full(name):
    """Extension including Creo version suffix: part.prt.3 -> .prt.3"""
    base, e = os.path.splitext(name)
    if e[1:].isdigit():
        e2 = os.path.splitext(base)[1]
        return (e2 + e) if e2 else e
    return e


def ext_key(name):
    e = ext_full(name).lower()
    return re.sub(r'\.\d+$', '.N', e) if re.search(r'\.\d+$', e) and e.count('.') == 2 else e


def utf16_strings(b, n=4):
    return [m.group().decode('utf-16le', 'ignore') for m in re.finditer(rb'(?:[\x20-\x7e]\x00){%d,}' % n, b)]


def ascii_strings(b, n=4):
    return [m.group().decode('latin1') for m in re.finditer(rb'[\x20-\x7e\xa0-\xff]{%d,}' % n, b)]


def text_of(parts):
    t = '\n'.join(dict.fromkeys(p for p in parts if p))
    return t[:MAX_TEXT]


def classify(b, ext):
    if not b:
        return 'empty'
    if b[:5] == b'%PDF-' or (ext == '.pdf' and b'%PDF-' in b[:1024]):
        return 'pdf'
    if b[:8] == OLE_MAGIC:
        return 'ole'
    if b[:4] in (b'PK\x03\x04', b'PK\x05\x06'):
        return 'zip'
    if b[:2] == b'\x1f\x8b':
        return 'gzip'
    if len(b) > 262 and b[257:262] == b'ustar':
        return 'tar'
    if b[:6] == b'7z\xbc\xaf\x27\x1c':
        return '7z'
    if b[:4] == b'Rar!':
        return 'rar'
    if ext in SW_EXT:
        return 'sw'
    if b[:8] == b'\x89PNG\r\n\x1a\n' or b[:3] == b'\xff\xd8\xff' or b[:4] in (b'II*\x00', b'MM\x00*') or b[:4] == b'GIF8' or b[:2] == b'BM':
        return 'image'
    if b[:4] == b'%!PS' or b[:4] == b'\xc5\xd0\xd3\xc6':
        return 'ps'
    head = b[:4096].decode('latin1')
    if 'ISO-10303-21' in head[:400]:
        return 'step'
    lines = head.splitlines()
    if lines and len(lines[0]) >= 73 and lines[0][72] == 'S':
        return 'iges'
    if head.startswith('#UGC') or '#UGC:' in head[:64]:
        return 'creo'
    if head.startswith('**ABCDEFGHIJKLMNOPQRSTUVWXYZ') or ext in ('.x_t', '.xmt_txt'):
        return 'parasolid'
    if ext in ('.x_b', '.xmt_bin'):
        return 'parasolid_bin'
    if ext in ('.sat',) or (ext in ('.sab',)):
        return 'sat'
    if re.match(r'\s*0\r?\nSECTION', head):
        return 'dxf'
    if b[:4] == b'AC10' or b[:4] == b'AC10'[:4]:
        return 'dwg'
    if ext == '.stl':
        return 'stl'
    if ext == '.obj':
        return 'obj'
    if ext in ('.wrl', '.vrml') or head.startswith('#VRML'):
        return 'wrl'
    if ext == '.svg' or '<svg' in head[:2000]:
        return 'svg'
    if head.lstrip().startswith('<?xml'):
        return 'xml'
    if ext in ('.unv', '.neu', '.ply', '.off', '.brep', '.mtl', '.igs_txt'):
        return 'text'
    sample = b[:8192]
    printable = sum(32 <= c < 127 or c in (9, 10, 13) for c in sample)
    if printable / max(1, len(sample)) > 0.95:
        return 'text'
    return 'bin'


# ---------------------------------------------------------------- helpers per format
def step_items(s):
    """Split a STEP parameter list at top level; returns raw items."""
    items, depth, cur, i, n = [], 0, [], 0, len(s)
    while i < n:
        c = s[i]
        if c == "'":
            j = i + 1
            while j < n:
                if s[j] == "'" and (j + 1 >= n or s[j + 1] != "'"):
                    break
                j += 2 if s[j] == "'" else 1
            cur.append(s[i:j + 1]); i = j + 1; continue
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
        if c == ',' and depth == 0:
            items.append(''.join(cur).strip()); cur = []
        else:
            cur.append(c)
        i += 1
    if cur:
        items.append(''.join(cur).strip())
    return items


def step_strings(item):
    return [m.group(1).replace("''", "'") for m in re.finditer(r"'((?:[^']|'')*)'", item)]


def iges_params(s, pd=',', rd=';'):
    out, i, n = [], 0, len(s)
    while i < n:
        m = re.match(r'\s*(\d+)H', s[i:])
        if m:
            L = int(m.group(1)); a = i + m.end(); out.append(s[a:a + L]); i = a + L
        else:
            j = i
            while j < n and s[j] not in (pd, rd):
                j += 1
            out.append(s[i:j].strip()); i = j
        if i < n and s[i] == rd:
            break
        if i < n and s[i] == pd:
            i += 1
    return out


def strip_hollerith(s):
    out, i, n = [], 0, len(s)
    while i < n:
        m = re.compile(r'(\d+)H').match(s, i)
        if m and (i == 0 or not s[i - 1].isdigit() and s[i - 1] not in '.Ee+-'):
            L = int(m.group(1)); i = m.end() + L; out.append(' '); continue
        out.append(s[i]); i += 1
    return ''.join(out)


def numhash(b):
    hh = hashlib.sha1()
    cnt = 0
    for m in NUM.finditer(b):
        hh.update(m.group()); hh.update(b','); cnt += 1
    return f'{hh.hexdigest()[:16]}:{cnt}'


def sw_parse(b):
    """SolidWorks 2015+ sections: marker 14 00 06 00, u32 CRC32(uncompressed)@+10, comp@+14, uncomp@+18,
    namelen@+22, obfuscated name@+26, raw-deflate payload. Returns (marker_pos, data_pos, comp, uncomp, data)."""
    secs, pos, n = [], 0, len(b)
    while len(secs) < 5000:
        i = b.find(b'\x14\x00\x06\x00', pos)
        if i < 0 or i + 26 > n:
            break
        crc, comp, unc, nl = st.unpack_from('<IIII', b, i + 10)
        a = i + 26 + nl
        if 0 < nl < 1024 and 0 < comp <= n - a and unc < (1 << 31):
            try:
                d = zlib.decompressobj(-15)
                data = d.decompress(b[a:a + comp])
                if len(data) == unc and (zlib.crc32(data) & 0xffffffff) == crc:
                    secs.append((i, a, comp, unc, data)); pos = a + comp
                    continue
            except Exception:
                pass
        pos = i + 4
    return secs


def sw_sections(b):
    return [x[4] for x in sw_parse(b)]


def ole_inflate(raw):
    """Inventor-style OLE stream holding a zlib stream near its start: (offset, data, consumed) or None."""
    for m in re.finditer(rb'\x78[\x01\x5e\x9c\xda]', raw[:64]):
        i = m.start()
        try:
            d = zlib.decompressobj()
            data = d.decompress(raw[i:])
            if len(data) > 16 and d.eof:
                return i, data, len(raw) - i - len(d.unused_data)
        except Exception:
            continue
    return None


def ole_fmtid(raw):
    try:
        return str(uuid.UUID(bytes_le=raw[28:44])).upper()
    except Exception:
        return ''


def clean(v):
    if isinstance(v, bytes):
        v = v.decode('utf-8', 'ignore') if not v.startswith(b'\xff\xfe') else v.decode('utf-16', 'ignore')
    v = str(v).split('\x1f')[0]
    v = re.sub(r'[\x00-\x08\x0b-\x1f]', ' ', v)
    return v.strip()


# Known identity-bearing property ids per FMTID
FMT_SUMMARY = 'F29F85E0-4FF9-1068-AB91-08002B27B3D9'
FMT_DOCSUM = 'D5CDD502-2E9C-101B-9397-08002B2CF9AE'
FMT_DESIGN = '32853F0F-3444-11D1-9E93-0060B03C1CA6'
OLE_SLOTS = {
    (FMT_SUMMARY, 4): ('author', 'person'), (FMT_SUMMARY, 8): ('last_saved_by', 'person'),
    (FMT_DOCSUM, 14): ('manager', 'person'), (FMT_DOCSUM, 15): ('company', 'company'),
    (FMT_DESIGN, 10): ('checked_by', 'person'), (FMT_DESIGN, 12): ('engr_approved_by', 'person'),
    (FMT_DESIGN, 34): ('mfg_approved_by', 'person'), (FMT_DESIGN, 41): ('designer', 'person'),
    (FMT_DESIGN, 42): ('engineer', 'person'), (FMT_DESIGN, 43): ('authority', 'company'),
}
OLE_ENG = {(FMT_DESIGN, 20): 'material', (FMT_DESIGN, 58): 'mass', (FMT_DESIGN, 61): 'density',
           (FMT_DESIGN, 52): 'size_designation', (FMT_DESIGN, 49): 'standards_org', (FMT_DESIGN, 37): 'standard'}

# SolidWorks custom-property keys that carry identities (lowercased; localized variants included)
SW_PERSON_KEYS = re.compile(r'(?i)^(sw-author|sw-last saved by|sw-dernier enregistrement par|sw-zuletzt gespeichert von|sw-autor|sw-auteur'
                            r'|sw-上次保存者|sw-作者|sw-последнее сохранение|sw-автор|author|autor|auteur|drawnby|drawn by|drawn|drawn_by|designer|designed by|designedby'
                            r'|engineer|checkedby|checked by|checked|approvedby|approved by|approved|wdc_drn|wdc_appr\d?|wdc_chk|wdc_eng'
                            r'|ersteller|bearbeiter|gezeichnet|geprüft|konstrukteur|freigegeben|dessinateur|vérifié par|approuvé par'
                            r'|设计|制图|审核|校对|批准|工艺|标准化|разработал|разраб\.?|проверил|пров\.?|утвердил|утв\.?|т\.контр\.?|н\.контр\.?)$')
SW_COMPANY_KEYS = re.compile(r'(?i)^(company|companyname|company name|firma|société|societe|organisation|organization|organisation name|公司|单位|организация|customer|client|owner)$')
SW_CONTACT_KEYS = re.compile(r'(?i)^(sw-comments|comments|email|e-mail|phone|tel|telephone|contact|kommentar|commentaires|备注|комментарий)$')
SW_ENG_KEYS = re.compile(r'(?i)^(sw-massprop-config-\d+|sw_units.*|sw_unitsystem|material|材料|материал|weight|mass|质量|масса|单重|finish|surface finish|'
                         r'.*tol.*|sw-configuration name|sw-sheet scale|sw-sheet format size|sw-template size|swformatsize|revision|rev|wdc_matl.*|wdc_finish.*|'
                         r'description|title_line_\d|wdc_title_\d)$')


# ---------------------------------------------------------------- main analyzer
def analyze(path, rel, depth=0, data=None):
    name = os.path.basename(rel)
    ef = ext_full(name)
    ext = ef.lower() if not re.search(r'\.\d+$', ef) else os.path.splitext(name[:-len(ef.split('.')[-1]) - 1])[1].lower()
    F = {'rel': rel, 'ext_full': ef, 'size': 0, 'kind': 'empty', 'sha1': '', 'opens': True, 'open_err': '',
         'struct': {}, 'geom': None, 'chunks': [], 'slots': [], 'eng': [], 'refs': [], 'bundle': []}
    try:
        if data is None:
            F['size'] = os.path.getsize(path)
            if F['size'] > MAX_PARSE:
                with open(path, 'rb') as fh:
                    head = fh.read(1 << 20)
                F['kind'] = classify(head, ext)
                F['sha1'] = 'big:' + str(F['size'])
                F['struct'] = {'big': 1}
                F['bundle'].append(['raw', 'head', text_of(ascii_strings(head, 6) + utf16_strings(head, 6))])
                return F
            with open(path, 'rb') as fh:
                b = fh.read()
        else:
            b = data
            F['size'] = len(b)
        F['sha1'] = h(b)
        k = classify(b, ext)
        F['kind'] = k
        fn = PARSERS.get(k, parse_generic)
        fn(F, b, ext, depth)
    except Exception as e:  # parsing failure == does not open
        F['opens'] = False
        F['open_err'] = repr(e)[:200]
    return F


def B(F, cat, key, text):
    if text:
        F['bundle'].append([cat, key, text[:MAX_TEXT]])


def slot(F, cat, field, value, kind, idx=0):
    v = clean(value)
    if v:
        idx = sum(1 for x in F['slots'] if x[0] == cat and x[1] == field)   # positional index per field
        F['slots'].append([cat, field, idx, v, kind])


def eng(F, cat, field, value):
    v = clean(value)
    if v:
        F['eng'].append([cat, field, v])


def parse_empty(F, b, ext, depth):
    F['struct'] = {'empty': 1}


def parse_pdf(F, b, ext, depth):
    doc = pymupdf.open(stream=b, filetype='pdf')
    F['struct'] = {'pages': doc.page_count}
    meta = doc.metadata or {}
    parts = []
    for k2, v in meta.items():
        if v and k2 not in ('format', 'encryption', 'trapped'):
            parts.append(f'{k2}: {v}')
            if k2 == 'author':
                slot(F, 'pdf_metadata', 'author', v, 'person')
            if k2 in ('creator', 'producer'):
                eng(F, 'pdf_metadata', k2, v)
    xmp = doc.get_xml_metadata() or ''
    parts.append(xmp)
    for m in re.finditer(r'<dc:creator>(.*?)</dc:creator>', xmp, re.S):
        for i, li in enumerate(re.findall(r'<rdf:li[^>]*>([^<]*)</rdf:li>', m.group(1))):
            slot(F, 'pdf_metadata', 'xmp:dc:creator', li, 'person', i)
    for n in doc.embfile_names():
        parts.append('attachment: ' + n)
        try:
            inner = doc.embfile_get(n)
            sub = analyze(None, n, depth + 1, data=inner) if depth < 2 else None
            if sub:
                for cat, key, t in sub['bundle']:
                    B(F, 'pdf_metadata', 'attach:' + n + ':' + key, t)
        except Exception:
            pass
    B(F, 'pdf_metadata', 'meta', text_of(parts))
    geo, texts = [], []
    for pno in range(doc.page_count):
        pg = doc[pno]
        if pno < 40:
            t = pg.get_text()
            texts.append(t)
            for a in pg.annots() or []:
                texts.append((a.info.get('title', '') + ' ' + a.info.get('content', '')).strip())
            for l in pg.get_links():
                if l.get('uri'):
                    texts.append(l['uri'])
        if pno < 3:
            r = pg.rect
            try:
                nd = len(pg.get_cdrawings())
            except Exception:
                nd = -1
            geo.append(f'{round(r.width)}x{round(r.height)}:{nd}:{len(pg.get_image_info())}')   # drawn images, not resource lists
            if pno == 0:
                toks = re.findall(r'(?:Ø|R|M)?\d+(?:[.,]\d+)?(?:\s?[±]\s?\d+(?:[.,]\d+)?)?(?:\s?(?:[hHgGfFkKmMnNpPjJsS]\d{1,2}))?|M\d+(?:[x×]\d+(?:[.,]\d+)?)?(?:\s?-\s?\d[gGhH])?|Ra\s?\d+(?:[.,]\d+)?', t)
                toks = [x for x in dict.fromkeys(toks) if len(x) >= 3 and re.search(r'[.,±xX×]|[A-Za-z]', x)]
                for x in toks[:12]:
                    eng(F, 'visible_text', 'page1_token', x)
    B(F, 'visible_text', 'pages', text_of(texts))
    F['geom'] = 'pdf:' + h('|'.join(geo) + f'|{doc.page_count}')


def parse_ole(F, b, ext, depth):
    o = olefile.OleFileIO(io.BytesIO(b))
    entries = o.listdir(streams=True, storages=False)
    F['struct'] = {'streams': len(entries)}
    props_txt, other_txt = [], []
    for s in entries:
        key = '/'.join(s)
        raw = o.openstream(s).read()
        F['chunks'].append([key, h(raw)])
        if s[-1].startswith('\x05'):
            fmt = ole_fmtid(raw)
            try:
                props = o.getproperties(s, convert_time=False)
            except Exception:
                props = {}
            for pid, v in props.items():
                if isinstance(v, (bytes, str)):
                    cv = clean(v)
                    if not cv:
                        continue
                    props_txt.append(f'{pid}: {cv}')
                    if (fmt, pid) in OLE_SLOTS:
                        fld, kind = OLE_SLOTS[(fmt, pid)]
                        slot(F, 'native_properties', f'ole:{fld}', cv, kind)
                    elif (fmt, pid) in OLE_ENG:
                        eng(F, 'native_properties', 'ole:' + OLE_ENG[(fmt, pid)], cv)
            props_txt += utf16_strings(raw, 3) + ascii_strings(raw, 4)
            B(F, 'native_properties', key, text_of(props_txt))
            props_txt = []
        else:
            if len(raw) < 32_000_000:
                ss = utf16_strings(raw, 5) + ascii_strings(raw, 6)
                z = ole_inflate(raw)
                if z is not None:
                    ss += utf16_strings(z[1], 5) + ascii_strings(z[1], 6)
                other_txt += ss
                for x in ss:
                    if '.' not in x:
                        continue
                    for m in REF_OLE.finditer(x):
                        F['refs'].append(['native_properties', m.group(1).strip()])
            B(F, 'native_properties', key, text_of(other_txt[-2000:]))
            other_txt = []
    F['geom'] = None  # chunk-based


def parse_sw(F, b, ext, depth):
    secs = sw_sections(b)
    if not secs:
        raise ValueError('no SolidWorks sections found')
    F['struct'] = {'sections': len(secs)}
    core = {}
    for i, d in enumerate(secs):
        F['chunks'].append([f'sec{i}', h(d)])
        ss = utf16_strings(d, 4) + ascii_strings(d, 4)
        B(F, 'native_properties', f'sec{i}', text_of(ss))
        for m in SW_CORE.finditer(d):
            fld = m.group(1).decode(); v = m.group(2).decode('utf-8', 'ignore')
            core[fld] = v
            slot(F, 'native_properties', 'sw:' + fld, v, 'company' if fld == 'Company' else 'person')
        for m in SW_PROP.finditer(d):
            k2 = m.group(1).decode('utf-8', 'ignore'); v = m.group(2).decode('utf-8', 'ignore')
            if SW_PERSON_KEYS.match(k2):
                slot(F, 'native_properties', 'swprop:' + k2, v, 'person')
            elif SW_COMPANY_KEYS.match(k2):
                slot(F, 'native_properties', 'swprop:' + k2, v, 'company')
            elif SW_CONTACT_KEYS.match(k2):
                slot(F, 'native_properties', 'swprop:' + k2, v, 'contact')
            elif SW_ENG_KEYS.match(k2):
                eng(F, 'native_properties', 'swprop:' + k2, v)
        for x in ss:
            if '.sld' not in x.lower():
                continue
            for m in REF_SW.finditer(x):
                F['refs'].append(['native_properties', m.group(1).strip()])
    F['geom'] = None


def parse_zip(F, b, ext, depth):
    z = zipfile.ZipFile(io.BytesIO(b))
    bad = z.testzip()
    if bad is not None:
        raise ValueError('zip crc error in ' + bad)
    infos = z.infolist()
    F['struct'] = {'members': len(infos)}
    cat = 'native_properties' if ext in NATIVE_ZIP_EXT else 'archives'
    B(F, cat, 'names', text_of([i.filename for i in infos] + [z.comment.decode('latin1', 'ignore')]))
    geo = []
    for mi, i in enumerate(infos):
        F['chunks'].append([i.filename, f'{i.CRC:08x}:{i.file_size}'])
        if i.is_dir():
            continue
        low = i.filename.lower()
        if low.endswith(('.brp', '.brep')):
            geo.append(f'{i.CRC:08x}')
        if i.file_size > 64_000_000 or depth >= 3:
            continue
        data = z.read(i)
        if low.endswith(('document.xml', 'guidocument.xml')):
            t = data.decode('utf-8', 'ignore')
            for m in re.finditer(r'<Property name="(CreatedBy|LastModifiedBy|Company)"[^>]{0,200}>\s{0,40}<String value="([^"]{0,400})"', t):
                slot(F, cat, f'm{mi}:fcstd:' + m.group(1), m.group(2), 'company' if m.group(1) == 'Company' else 'person')
            for m in re.finditer(r'<Property name="(FileName)"[^>]*>\s*<String value="([^"]*)"', t):
                F['refs'].append([cat, m.group(2)])
        if low.endswith('3dmodel.model'):
            t = data.decode('utf-8', 'ignore')
            for m in re.finditer(r'<metadata name="(Designer|Copyright|CreatedBy)"[^>]{0,200}>([^<]{0,400})<', t):
                slot(F, cat, f'm{mi}:3mf:' + m.group(1), m.group(2), 'person')
            mu = re.search(r'<model[^>]*unit="([^"]+)"', t)
            if mu:
                eng(F, cat, '3mf:unit', mu.group(1))
            geo.append(numhash(re.sub(rb'<metadata.*?</metadata>', b'', data, flags=re.S)))
        sub = analyze(None, i.filename, depth + 1, data=data)
        for c2, key, t in sub['bundle']:
            B(F, cat, f'{i.filename}:{key}', t)
        for s in sub['slots']:
            F['slots'].append([cat, f'm{mi}:{s[1]}', s[2], s[3], s[4]])
    F['geom'] = ('zipgeo:' + h('|'.join(geo))) if geo else None


def parse_gzip(F, b, ext, depth):
    inner = gzip.decompress(b)
    F['struct'] = {'inner_size': len(inner)}
    if depth < 3:
        sub = analyze(None, os.path.splitext(F['rel'])[0], depth + 1, data=inner)
        for c2, key, t in sub['bundle']:
            B(F, 'archives', 'gz:' + key, t)
        F['geom'] = sub['geom']


def parse_tar(F, b, ext, depth):
    tf = tarfile.open(fileobj=io.BytesIO(b))
    mem = tf.getmembers()
    F['struct'] = {'members': len(mem)}
    B(F, 'archives', 'names', text_of([m.name for m in mem]))
    for m in mem:
        if not m.isfile():
            continue
        data = tf.extractfile(m).read() if m.size < 64_000_000 else b''
        F['chunks'].append([m.name, h(data)])
        if data and depth < 3:
            sub = analyze(None, m.name, depth + 1, data=data)
            for c2, key, t in sub['bundle']:
                B(F, 'archives', f'{m.name}:{key}', t)


def parse_opaque_archive(F, b, ext, depth):
    F['struct'] = {'size': len(b)}
    B(F, 'archives', 'raw', text_of(ascii_strings(b[:4_000_000], 6) + utf16_strings(b[:4_000_000], 6)))


def parse_step(F, b, ext, depth):
    t = b.decode('latin1')
    if 'END-ISO-10303-21' not in t[-4096:] and 'END-ISO-10303-21' not in t:
        raise ValueError('STEP terminator missing')
    hi = t.find('DATA;')
    header = t[:hi if hi > 0 else 20000]
    data = t[hi:] if hi > 0 else ''
    F['struct'] = {'entities': len(re.findall(r'(?m)^\s*#\d+\s*=', data))}
    B(F, 'cad_headers', 'header', header)
    m = re.search(r'FILE_NAME\s*\((.*?)\)\s*;', header, re.S)
    if m:
        it = step_items(m.group(1))
        if len(it) >= 4:
            for i, s in enumerate(step_strings(it[2])):
                slot(F, 'cad_headers', 'step:author', s, 'person', i)
            for i, s in enumerate(step_strings(it[3])):
                slot(F, 'cad_headers', 'step:organization', s, 'company', i)
        if len(it) >= 7:
            for i, s in enumerate(step_strings(it[6])):
                slot(F, 'cad_headers', 'step:authorization', s, 'person', i)
    ms = re.search(r'FILE_SCHEMA\s*\((.*?)\)\s*;', header, re.S)
    if ms:
        eng(F, 'cad_headers', 'step:FILE_SCHEMA', ' '.join(ms.group(1).split()))
    qs = re.findall(r"'((?:[^']|'')*)'", data[:MAX_TEXT * 4])
    B(F, 'cad_headers', 'strings', text_of(dict.fromkeys(q for q in qs if q.strip())))
    for mu in list(re.finditer(r'(SI_UNIT\s*\([^;]*\)|CONVERSION_BASED_UNIT\s*\([^;]*\))', data))[:4]:
        eng(F, 'cad_headers', 'step:unit', ' '.join(mu.group(1).split()))
    nb = re.sub(rb"'(?:[^']|'')*'", b'', b[b.find(b'DATA;'):]) if hi > 0 else b''
    F['geom'] = 'step:' + numhash(re.sub(rb'#\d+', b'', nb))


def parse_iges(F, b, ext, depth):
    t = b.decode('latin1')
    lines = t.splitlines()
    sec = {}
    for l in lines:
        if len(l) >= 73:
            sec.setdefault(l[72], []).append(l[:72])
    if 'T' not in sec:
        raise ValueError('IGES terminate section missing')
    F['struct'] = {'D': len(sec.get('D', [])), 'P': len(sec.get('P', []))}
    s_txt = ''.join(sec.get('S', [])); g_txt = ''.join(sec.get('G', []))
    B(F, 'cad_headers', 'S', s_txt)
    pd, rd = ',', ';'
    if g_txt[:2] == '1H':
        pd = g_txt[2]
        if g_txt[4:6] == '1H':
            rd = g_txt[6]
    gp = iges_params(g_txt, pd, rd)
    B(F, 'cad_headers', 'G', '\n'.join(gp))
    if len(gp) >= 22:
        slot(F, 'cad_headers', 'iges:author', gp[20], 'person')
        slot(F, 'cad_headers', 'iges:organization', gp[21], 'company')
    if len(gp) >= 15:
        eng(F, 'cad_headers', 'iges:unit_flag', gp[13]); eng(F, 'cad_headers', 'iges:unit_name', gp[14])
    p = '\n'.join(sec.get('P', []))
    B(F, 'cad_headers', 'P_strings', text_of([x for x in re.findall(r'\d+H([^,;]+)', p)][:5000]))
    F['geom'] = 'iges:' + numhash(strip_hollerith(p).encode('latin1'))


def parse_sat(F, b, ext, depth):
    t = b.decode('latin1')
    lines = t.splitlines()
    if len(lines) < 4:
        raise ValueError('SAT too short')
    B(F, 'cad_headers', 'header', '\n'.join(lines[:3]))
    body = '\n'.join(lines[3:])
    out, i = [], 0
    for m in re.finditer(r'@(\d+) ', body):
        if m.start() < i:
            continue
        out.append(body[i:m.start()]); i = m.end() + int(m.group(1))
    out.append(body[i:])
    body2 = ' '.join(out)
    B(F, 'cad_headers', 'strings', text_of(re.findall(r'@\d+ ([^\s]+)', body)[:5000]))
    F['struct'] = {'lines': len(lines) // 100}
    F['geom'] = 'sat:' + numhash(body2.encode('latin1'))


def parse_parasolid(F, b, ext, depth):
    t = b.decode('latin1')
    hdr = t.split('**END_OF_HEADER')[0][:20000] if '**END_OF_HEADER' in t else t[:4000]
    B(F, 'cad_headers', 'header', hdr)
    for m in re.finditer(r'(?m)^(?:\*\*)?(USER|SITE)=(.*?);?\s*$', hdr):
        slot(F, 'cad_headers', 'parasolid:' + m.group(1), m.group(2), 'person' if m.group(1) == 'USER' else 'company')
    F['struct'] = {'size_k': len(b) // 4096}
    F['geom'] = 'ps:' + numhash(b[len(hdr):])


def parse_creo(F, b, ext, depth):
    if b'#-END_OF_UGC_HEADER' not in b[:200000]:
        raise ValueError('Creo header terminator missing')
    ss = ascii_strings(b, 6)
    B(F, 'cad_headers', 'strings', text_of(ss))
    for m in re.finditer(rb'#- CMNM ([^\s\\]+)', b[:200000]):
        F['refs'].append(['cad_headers', m.group(1).decode('latin1')])
    F['struct'] = {'ugc': 1}
    F['geom'] = None  # line-sample based


def parse_dxf(F, b, ext, depth):
    t = b.decode('latin1')
    lines = t.splitlines()
    if not any(l.strip() == 'EOF' for l in lines[-6:]):
        raise ValueError('DXF EOF missing')
    pairs = [(lines[i].strip(), lines[i + 1] if i + 1 < len(lines) else '') for i in range(0, len(lines) - 1, 2)]
    F['struct'] = {'entities': sum(1 for c, v in pairs if c == '0')}
    hdr = []
    for i, (c, v) in enumerate(pairs[:20000]):
        if c == '9' and i + 1 < len(pairs):
            var = v.strip(); val = pairs[i + 1][1].strip()
            hdr.append(f'{var} {val}')
            if var == '$LASTSAVEDBY':
                slot(F, 'cad_headers', 'dxf:$LASTSAVEDBY', val, 'person')
            if var in ('$INSUNITS', '$MEASUREMENT', '$ACADVER', '$LUNITS', '$AUNITS'):
                eng(F, 'cad_headers', 'dxf:' + var, val)
        if c == '0' and v.strip() == 'ENDSEC' and hdr:
            break
    B(F, 'cad_headers', 'header', '\n'.join(hdr))
    txt = [v for c, v in pairs if c in ('1', '3', '2', '999', '1000', '7', '8')]
    B(F, 'visible_text', 'strings', text_of(dict.fromkeys(x.strip() for x in txt if x.strip())))
    coords = [v.strip() for c, v in pairs if c.isdigit() and (10 <= int(c) <= 59 or 210 <= int(c) <= 239)]
    F['geom'] = 'dxf:' + h(','.join(coords))


def parse_dwg(F, b, ext, depth):
    F['struct'] = {'ver': b[:6].decode('latin1')}
    B(F, 'cad_headers', 'strings', text_of(ascii_strings(b, 6) + utf16_strings(b, 5)))


def parse_stl(F, b, ext, depth):
    if b[:5].lower() == b'solid' and b'facet' in b[:2000]:
        t = b.decode('latin1')
        first = t.splitlines()[0]
        B(F, 'cad_headers', 'solid', first + '\n' + '\n'.join(re.findall(r'(?m)^\s*endsolid.*$', t)[:5]))
        F['struct'] = {'facets': t.count('facet normal')}
        F['geom'] = 'stl:' + numhash(b[len(first):])
    else:
        if len(b) < 84:
            raise ValueError('STL too short')
        n = st.unpack_from('<I', b, 80)[0]
        if 84 + 50 * n != len(b):
            raise ValueError('binary STL size mismatch')
        B(F, 'cad_headers', 'header80', b[:80].decode('latin1').rstrip('\x00 '))
        F['struct'] = {'facets': n}
        F['geom'] = 'stl:' + h(b[84:])


def parse_lines_mesh(F, b, ext, depth):
    t = b.decode('latin1')
    names, geo = [], []
    for l in t.splitlines():
        s = l.strip()
        if not s:
            continue
        if s[0] == '#' or s.startswith(('o ', 'g ', 'usemtl', 'mtllib', 'DEF ', 'USE ', 'newmtl', 'map_')):
            names.append(s[:300])
            if s.startswith('mtllib'):
                F['refs'].append(['cad_headers', s[7:].strip()])
        elif s[:2] in ('v ', 'vt', 'vn', 'f '):
            geo.append(s)
    B(F, 'cad_headers', 'names', text_of(names[:5000]))
    if ext == '.obj':
        F['struct'] = {'v': sum(1 for g in geo if g.startswith('v ')), 'f': sum(1 for g in geo if g.startswith('f '))}
        F['geom'] = 'obj:' + h('\n'.join(geo))
    else:
        F['struct'] = {'lines': t.count('\n') // 100}
        nb = re.sub(rb'#[^\n]*|"[^"\n]*"', b'', b)
        F['geom'] = 'num:' + numhash(nb)


def parse_svg(F, b, ext, depth):
    t = b.decode('utf-8', 'ignore')
    ET.fromstring(b)  # must parse
    F['struct'] = {'elements': t.count('<')}
    meta = re.findall(r'(?:sodipodi:docname|inkscape:export-filename|xlink:href)="([^"]*)"', t)
    meta += [' '.join(re.sub(r'<[^>]+>', ' ', m).split()) for m in re.findall(r'<metadata.*?</metadata>', t, re.S)]
    meta += re.findall(r'<!--(.*?)-->', t, re.S)[:20]
    B(F, 'cad_headers', 'meta', text_of(meta))
    vis = [' '.join(re.sub(r'<[^>]+>', ' ', m).split()) for m in re.findall(r'<(?:text|title|desc)[^>]*>(.*?)</(?:text|title|desc)>', t, re.S)]
    B(F, 'visible_text', 'text', text_of(vis))
    F['geom'] = 'svg:' + h(' '.join(re.findall(r'\sd="([^"]*)"', t)))


def parse_xml(F, b, ext, depth):
    ET.fromstring(b)
    F['struct'] = {'elements': b.count(b'<')}
    B(F, 'cad_headers', 'xml', b.decode('utf-8', 'ignore')[:MAX_TEXT])


def parse_ps(F, b, ext, depth):
    t = b[:2_000_000].decode('latin1')
    dsc = [l for l in t.splitlines() if l.startswith('%%')][:200]
    B(F, 'cad_headers', 'dsc', '\n'.join(dsc))
    for l in dsc:
        if l.startswith('%%For:'):
            slot(F, 'cad_headers', 'ps:%%For', l[6:], 'person')
        if l.startswith('%%Creator:') and re.search(r'\w+:\w+|\(', l):
            slot(F, 'cad_headers', 'ps:%%Creator', l[10:], 'person')
    B(F, 'visible_text', 'show', text_of(re.findall(r'\(([^()\\]{3,200})\)\s*(?:show|S|Tj|ashow|widthshow)', t)[:5000]))
    F['struct'] = {'pages': t.count('%%Page:')}


def parse_image(F, b, ext, depth):
    im = Image.open(io.BytesIO(b))
    im.load()
    F['struct'] = {'w': im.size[0], 'h': im.size[1]}
    parts = []
    for k2, v in (im.info or {}).items():
        if isinstance(v, (str, bytes)) and k2 not in ('icc_profile', 'exif', 'transparency') and len(v) < 4000:
            parts.append(f'{k2}: {clean(v)}')
            if k2.lower() in ('author', 'artist', 'copyright'):
                slot(F, 'images', 'png:' + k2, v, 'person')
    try:
        ex = im.getexif()
        for tag, v in ex.items():
            nm = {315: 'Artist', 33432: 'Copyright', 40093: 'XPAuthor', 270: 'ImageDescription', 305: 'Software', 42016: 'ImageUniqueID'}.get(tag)
            if nm:
                cv = clean(v.decode('utf-16le', 'ignore') if nm.startswith('XP') and isinstance(v, bytes) else v)
                parts.append(f'{nm}: {cv}')
                if nm in ('Artist', 'Copyright', 'XPAuthor'):
                    slot(F, 'images', 'exif:' + nm, cv, 'person')
    except Exception:
        pass
    xmp = re.findall(rb'<x:xmpmeta.*?</x:xmpmeta>', b[:2_000_000], re.S)
    if xmp:
        parts.append(xmp[0].decode('utf-8', 'ignore')[:20000])
    B(F, 'images', 'meta', text_of(parts))
    th = im.convert('L').resize((32, 32))
    F['geom'] = 'img:' + th.tobytes().hex()


def parse_text(F, b, ext, depth):
    t = b[:MAX_TEXT * 2].decode('utf-8', 'ignore')
    F['struct'] = {'lines': t.count('\n') // 100}
    lines = t.splitlines()
    names = [l for l in lines if re.search(r'[A-Za-z]{3}', l) and not re.fullmatch(r'[\s\dEeDd.+\-,]+', l)]
    B(F, 'cad_headers', 'text', text_of(names[:20000]))
    F['geom'] = 'num:' + numhash(b'\n'.join(l.encode('latin1', 'ignore') for l in lines if not re.search(r'[A-CF-Za-cf-z]', l)))


def parse_generic(F, b, ext, depth):
    F['struct'] = {'size_k': len(b) // 4096}
    B(F, 'cad_headers', 'strings', text_of(ascii_strings(b[:64_000_000], 6) + utf16_strings(b[:64_000_000], 5)))


PARSERS = {
    'empty': parse_empty, 'pdf': parse_pdf, 'ole': parse_ole, 'sw': parse_sw, 'zip': parse_zip, 'gzip': parse_gzip,
    'tar': parse_tar, '7z': parse_opaque_archive, 'rar': parse_opaque_archive, 'step': parse_step, 'iges': parse_iges,
    'sat': parse_sat, 'parasolid': parse_parasolid, 'creo': parse_creo, 'dxf': parse_dxf, 'dwg': parse_dwg,
    'stl': parse_stl, 'obj': parse_lines_mesh, 'wrl': parse_lines_mesh, 'svg': parse_svg, 'xml': parse_xml,
    'ps': parse_ps, 'image': parse_image, 'text': parse_text, 'bin': parse_generic, 'parasolid_bin': parse_generic,
}


def line_sample(b, free_pred, k=200):
    """For opaque formats: hashes of up to k lines that contain no identity (free_pred(line_text)->bool)."""
    out = []
    for ln in b.split(b'\n'):
        if 8 <= len(ln) <= 4000:
            s = ln.decode('latin1')
            if free_pred(s):
                out.append(h(ln))
    if len(out) > k:
        step = len(out) / k
        out = [out[int(i * step)] for i in range(k)]
    return out


def line_hashes(b):
    return {h(ln) for ln in b.split(b'\n') if 8 <= len(ln) <= 4000}
