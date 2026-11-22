"""Rules that turn slot values, paths and free text into identity strings.

Everything here is heuristic and tuned for precision: an identity we add becomes a leak check,
so false positives make the local score pessimistic in a misleading way.
"""
import re
import unicodedata

GENERIC = {
    'administrator', 'admin', 'user', 'users', 'operator', 'owner', 'unknown', 'author', 'none', 'null', 'default', 'temp', 'tmp',
    'guest', 'test', 'public', 'all users', 'default user', 'localservice', 'system', 'root', 'pc', 'home', 'desktop', 'documents',
    'name', 'designer', 'engineer', 'unspecified', 'n/a', 'na', 'tbd', 'anonymous', 'student', 'autor', 'auteur', 'benutzer',
    'administrateur', 'utilisateur', 'windows user', 'win', 'adminforworker', 'any', 'company', 'my company', 'mycompany', 'organization',
    'organisation', 'unknown author', 'not specified', 'sw', 'solidworks', 'customer', 'appdata', 'local', 'roaming', 'programdata',
    'program files', 'onedrive', 'dropbox', 'google drive', 'icloud drive', 'shared', 'work', 'projects', 'project', 'cad', 'git', 'github',
    'documents and settings', 'my documents', 'downloads', 'workspace', 'workspaces', 'source', 'src', 'build', 'scratch', 'proj', 'mnt',
    'volumes', 'media', 'data', 'var', 'opt', 'usr', 'etc', 'private', 'folders', 'library', 'applications', 'conda', 'envs', 'lib',
    '用户', '管理员', 'пользователь', 'администратор', 'microsoft', 'dell', 'hp', 'lenovo', 'buildbot', 'jenkins', 'runner', 'travis',
    'vagrant', 'ubuntu', 'debian', 'pi', 'nobody', 'www-data', 'docker', 'sandbox', 'tester', 'demo', 'example', 'sample', 'xxx', 'abc',
}
SOFTWARE = re.compile(r'(?i)\boc{1,2}t\b|steptools|hsmworks|camworks|mastercam|solidcam|esprit|hypermill|powermill|featurecam|solid\s?works|dassault|autodesk|inventor|\bptc\b|parametric technology|creo|pro/?engineer|siemens|solid ?edge|\bnx\b|unigraphics|catia|'
                      r'freecad|open ?cascade|spatial corp|\bacis\b|parasolid|adobe|acrobat|microsoft|mac os|quartz|ghostscript|cairo|inkscape|ezdxf|netfabb|'
                      r'fusion ?360|kicad|zwcad|zw3d|designcenter|i-deas|\bsdrc\b|bentley|microstation|rhino|sketchup|blender|meshlab|prusa|slic3r|cura|'
                      r'trimesh|greenshot|lead technologies|libreoffice|openoffice|pdfcreator|cutepdf|pdf-xchange|foxit|primopdf|pdfium|skia|itext|reportlab|'
                      r'matplotlib|tcpdf|wkhtmltopdf|latex|pdftex|xetex|dvips|onshape|ansys|comsol|abaqus|hypermesh|gmsh|salome|vectorworks|draftsight|'
                      r'librecad|qcad|bricscad|progecad|eplan|altium|eagle|easyeda|xplus|translator|interop|datakit|cadexchanger|tetra4d|3d pdf|'
                      r'aspose|pymupdf|mupdf|poppler|pdfsharp|apache|pdfbox|chrome|chromium|safari|firefox|word|excel|powerpoint|visio')
PLACEHOLDER = re.compile(r'(?i)^(unknown|unspecified|none|null|n/?a|-+|\?+|x+|\*+|redacted|anon(ymous)?|author|organi[sz]ation|company|name|default|'
                         r'\[.*\]|<.*>|tbd|todo|not set|not specified|\s*)$')
SAFE_DOMAINS = re.compile(r'(?i)(^|\.)(creativecommons\.org|wikipedia\.org|wikimedia\.org|freecadweb\.org|freecad\.org|opencascade\.(org|com)|netfabb\.com|'
                          r'solidworks\.com|autodesk\.com|w3\.org|adobe\.com|microsoft\.com|openxmlformats\.org|purl\.org|inkscape\.org|sodipodi\.sourceforge\.net|'
                          r'sourceforge\.net|cairographics\.org|iptc\.org|xml\.org|apple\.com|google\.com|gnu\.org|python\.org|slic3r\.org|prusa3d\.com|'
                          r'mozilla\.org|example\.(com|org|net)|schema\.org|schemas\.\w+|ns\.adobe\.com|iso\.org|ieee\.org|ptc\.com|plm\.automation\.siemens\.com|'
                          r'spatial\.com|3ds\.com|ezdxf\.mozman\.de|opengl\.org|khronos\.org|github\.com|gitlab\.com|githubusercontent\.com|bitbucket\.org|'
                          r'steptools\.com|hsmworks\.com|metalprices\.com|gdtanalysis\.net|amesweb\.info|engineeringtoolbox\.com|matweb\.com|'
                          r'stackoverflow\.com|youtube\.com|amazon\.com|ebay\.com|aliexpress\.com|digikey\.com|mouser\.com|mcmaster\.com|grabcad\.com|'
                          r'thingiverse\.com|printables\.com|cad\.onshape\.com|readthedocs\.io|pypi\.org|npmjs\.com|doi\.org|arxiv\.org)$')

USERPATH = re.compile(r'(?i)(?:[a-z]:[\\/]+(?:users|documents and settings|dokumente und einstellungen)[\\/]+|/(?:home|users)/|\\\\[\w.-]+\\users\\)([^\\/\s"\'<>|:*?]{2,40})')
UNC_HOST = re.compile(r'\\\\([a-z][\w-]*(?:\.[\w-]+)+)\\', re.I)
TLDS = ('com|org|net|edu|gov|mil|int|io|ai|co|uk|de|fr|it|es|nl|be|ch|at|se|no|dk|fi|pl|cz|sk|hu|ro|pt|ie|gr|ru|ua|cn|jp|kr|in|'
        'au|nz|ca|us|br|mx|ar|cl|za|il|tr|tw|hk|sg|my|th|vn|id|ph|eu|info|biz|dev|app|tech|xyz|me|tv|cc|ws|lt|lv|ee|si|hr|rs|bg')
EMAIL = re.compile(r'\b[A-Za-z0-9][A-Za-z0-9._%+-]{0,39}@[A-Za-z0-9][A-Za-z0-9-]{1,40}(?:\.[A-Za-z0-9-]{2,30})*\.(?:' + TLDS + r')\b')
URL = re.compile(r'(?i)\b(?:https?://|www\.)([\w-]+(?:\.[\w-]+)+)')
DOMAIN_USER = re.compile(r'^([A-Za-z][\w.-]{1,30})\\([\w.$-]{2,40})(?:\s*\(([\w.-]{2,40})\))?$')
CONTACT_NUM = re.compile(r'(?i)(?:qq|微信|wechat|tel\.?|phone|telefon|téléphone|mobile|handy|电话|手机)\s*[:：]?\s*(\+?\d[\d\s-]{6,16}\d)')
BY_LABEL = re.compile(r'(?i)(?:©|\(c\)|copyright)\s*(?:by)?\s*:?\s*([A-ZÀ-Ž][\w.\'’-]+(?:[ \t]+[A-ZÀ-Ž][\w.\'’-]+){0,3})|'
                      r'(?:designed|drawn|checked|approved|created|modelled|modeled)\s+by\s*:?\s*([A-ZÀ-Ž][\w.\'’-]+(?:[ \t]+[A-ZÀ-Ž][\w.\'’-]+){0,3})')
BARE_DOMAIN = re.compile(r'(?<![\w@./-])((?:[a-z0-9][a-z0-9-]{1,40}\.)+(?:com|org|net|edu|gov|io|co|de|fr|it|es|nl|be|ch|at|se|no|dk|fi|pl|cz|uk|eu|ru|cn|jp|kr|in|au|ca|us|br|info|biz|dev|tech))(?![\w-]|\.[a-z])', re.I)
GITHUB_URL = re.compile(r'(?i)github\.com/([\w-]{2,39})')

# Hand-made path identities (org / person / project names in catalog and "existing" folder names).
# Kept short and conservative; extend as needed.
def _load_private(name):
    """Hand-made identity lists hold real names, so they are kept out of the repository.
    Read from $DEID_PRIVATE_DATA (default ~/deid/private); a missing file gives an empty list."""
    import os
    path = os.path.join(os.environ.get("DEID_PRIVATE_DATA", os.path.expanduser("~/deid/private")), name)
    rows = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            cols = line.rstrip("\n").split("\t")
            if cols and cols[0] and not cols[0].startswith("#"):
                rows.append(cols)
    return rows


MANUAL = [(r[0], r[1]) for r in _load_private("manual_identities.tsv") if len(r) > 1]
MANUAL_MIN_CTX = {r[0] for r in _load_private("manual_min_ctx.tsv")}


def norm(s):
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return ' '.join(s.lower().split())


def variants(s):
    n = norm(s)
    out = {n}
    if ' ' in n:
        for j in ('', '_', '-', '.'):
            out.add(n.replace(' ', j))
    return {v for v in out if v}


def distinctive_repo(name):
    """Repo/project names that are safe to replace inside file contents (not plain dictionary-like words)."""
    if len(name) < 5:
        return False
    return bool(re.search(r'\d|[a-z][A-Z]|[-_.]', name)) or len(name) >= 14


DATEISH = re.compile(r'^\d{1,4}[-./]\d{1,2}[-./]\d{1,4}$|^\d+$')
LABELS = re.compile(r'(?i)^(approved|approved by|checked|checked by|drawn|drawn by|designed|designed by|created|created by|date|datum|name|'
                    r'scale|sheet|title|rev|revision|material|weight|finish|tolerance|description|part|part number|qty|quantity|'
                    r'gezeichnet|geprüft|freigegeben|bearbeitet|norm|maßstab|blatt|benennung|werkstoff|dessiné|vérifié|approuvé)$')


def garbled(v):
    # binary junk decoded as text: control chars, private-use/unassigned, or Latin mixed with CJK/Hangul
    if any(unicodedata.category(c) in ('Cc', 'Co', 'Cn', 'Cs') for c in v if c not in '\t'):
        return True
    latin = any('a' <= c.lower() <= 'z' for c in v)
    cjk = any('\u3040' <= c <= '\u9fff' or '\uac00' <= c <= '\ud7af' for c in v)
    return latin and cjk


def ok_value(v, kind):
    v = v.strip().strip('"\'')
    if not v or len(v) > 80 or PLACEHOLDER.match(v) or DATEISH.match(v) or LABELS.match(v) or garbled(v):
        return False
    n = norm(v)
    if n in GENERIC or SOFTWARE.search(v):
        return False
    if not re.search(r'[^\W\d_]', v):          # needs a letter
        return False
    if re.fullmatch(r'[\d\W_]+', v):
        return False
    if re.search(r'\.(sld\w+|ipt|iam|idw|step|stp|pdf|dwg|dxf|prt|asm|drw)$', v, re.I):
        return False
    return True


def from_slot(value, kind):
    """Identity strings contained in one slot value -> list of (string, kind)."""
    out = []
    v = value.strip()
    m = DOMAIN_USER.match(v)
    if m:
        dom, usr, host = m.groups()
        if ok_value(dom, 'company') and norm(dom) not in GENERIC:
            out.append((dom, 'company'))
        if ok_value(usr, 'account'):
            out.append((usr, 'account'))
        if host and ok_value(host, 'account'):
            out.append((host, 'account'))
        return out
    for u in USERPATH.findall(v):
        if ok_value(u, 'account'):
            out.append((u, 'account'))
    if kind == 'contact':
        out += [(e, 'contact') for e in EMAIL.findall(v)]
        out += [(n.strip(), 'contact') for n in CONTACT_NUM.findall(v)]
        return out
    if out:
        return out
    v2 = re.sub(r'(?i)^(?:©|\(c\)|copyright)\s*(?:by)?\s*:?\s*|,?\s*\b(19|20)\d\d\b.*$', '', v).strip(' ,;:')
    for part in re.split(r'\s*[;|]\s*|\s+(?:and|und|et|y)\s+|\s*/\s*', v2):
        part = part.strip()
        if ok_value(part, kind):
            out.append((part, kind))
    return out


def from_text(text, cat):
    """High-precision patterns found anywhere in a bundle text."""
    out = []
    for u in USERPATH.findall(text):
        if ok_value(u, 'account') and not u.startswith(('%', '$', '~')):
            out.append((u, 'account'))
    for hst in UNC_HOST.findall(text):
        out.append((hst, 'domain'))
    for e in EMAIL.findall(text):
        dom = e.split('@')[1]
        if not SAFE_DOMAINS.search(dom) and not re.search(r'\.(png|jpe?g|gif|svg)$', e, re.I):
            out.append((e, 'contact'))
    for d in URL.findall(text):
        d = d.lower().rstrip('.')
        d = d[4:] if d.startswith('www.') else d
        if '.' in d and not SAFE_DOMAINS.search(d) and not re.fullmatch(r'[\d.]+', d) and len(d) >= 5:
            out.append((d, 'domain'))
    if cat in ('visible_text', 'pdf_metadata', 'cad_headers', 'images', 'native_properties'):
        for d in BARE_DOMAIN.findall(text):
            d = d.lower()
            if not SAFE_DOMAINS.search(d) and not re.search(r'\.(?:sld\w+|step|stp|pdf|dwg|dxf|png|jpe?g|xml|html?|exe|dll|txt)$', d):
                out.append((d, 'domain'))
    for a in GITHUB_URL.findall(text):
        if ok_value(a, 'account') and a.lower() not in ('orgs', 'settings', 'features', 'about', 'blog', 'topics', 'sponsors'):
            out.append((a, 'account'))
    for n in CONTACT_NUM.findall(text):
        out.append((n.strip(), 'contact'))
    if cat in ('visible_text',):
        for a, b2 in BY_LABEL.findall(text):
            nm = (a or b2).strip()
            if ok_value(nm, 'person') and len(nm) >= 3:
                out.append((nm, 'person'))
    return out


def path_structure(rel):
    """Structural identity spans in a corpus-relative path -> list of (start, end, string, kind)."""
    spans = []
    parts = rel.split('/')
    pos = [0]
    for p in parts[:-1]:
        pos.append(pos[-1] + len(p) + 1)

    def add(seg_i, sub, kind):
        seg = parts[seg_i]
        j = seg.find(sub)
        if j >= 0 and sub:
            spans.append((pos[seg_i] + j, pos[seg_i] + j + len(sub), sub, kind))

    if len(parts) >= 3 and parts[0] == 'sources' and parts[1] == 'github':
        bits = parts[2].split('__')
        if bits and bits[0]:
            add(2, bits[0], 'account')
        if len(bits) > 1 and len(bits[1]) >= 2:
            add(2, bits[1], 'project')
    elif len(parts) >= 3 and parts[0] == 'sources' and parts[1] == 'gitlab':
        bits = [b for b in parts[2].split('__') if b]
        if len(bits) >= 2:
            add(2, bits[1], 'account')
        if len(bits) >= 3 and len(bits[-1]) >= 2:
            add(2, bits[-1], 'project')
    elif len(parts) >= 3 and parts[0] == 'sources' and parts[1] == 'web':
        add(2, parts[2], 'domain')
    elif len(parts) >= 2 and parts[0] == 'archives':
        bits = parts[1].split('__')
        if bits and bits[0]:
            add(1, bits[0], 'account')
        if len(bits) > 1 and len(bits[1]) >= 2:
            add(1, bits[1], 'project')
    elif len(parts) >= 3 and parts[0] == 'sources' and parts[1] == 'catalog':
        seg = parts[2]
        if '.' in seg and ' ' not in seg:
            add(2, seg, 'domain')
    return spans


GENERIC_ACCOUNT = re.compile(r'(?i)^(user|users|usuario|usuari|utente|utilisateur|benutzer|admin|administrator|administrateur|admini|'
                             r'temp|tmp|tempds|test|guest|owner|default|local|local_|design|designer|student|pc|win|windows|'
                             r'operator|engineer|cad|work|home)[\W_]?\d*(~\d+(\.\w+)?)?$')
JUNK_PHRASE = re.compile(r'(?i)^(drawn date|base plate|cover|design|title block|part name|file name|sheet \d+)$')


def ok_identity(n, kind, src):
    """Final dictionary-level filter (applied after pass 1, so rules can change without re-parsing)."""
    if GENERIC_ACCOUNT.match(n) or JUNK_PHRASE.match(n) or n in GENERIC or PLACEHOLDER.match(n):
        return False
    if kind == 'domain' and SAFE_DOMAINS.search(n):
        return False
    if SOFTWARE.search(n) and not src.get('path'):
        return False          # software stamps from file contents are not identities
    return True


def path_only(n):
    """Identities that must only be replaced as whole path/field tokens (owner accounts named like software)."""
    return bool(SOFTWARE.search(n))
