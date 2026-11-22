"""pipeline_v3 routing (file kept as v2.py so imports stay stable): one file in, one gated file out. Integrator's code (MIT), built on the teammate's engine/rename/fmt.

process(S, rel, data) -> (out_bytes, rec)   rec: kind, route, n, status, notes
  status: ok (changed and gated) | unchanged | kept (family not edited, listed) | gate-failed (original emitted,
  listed) | error (original emitted, listed). The output is ALWAYS either the gated edit or the original bytes.
"""
import io
import json
import os
import re
import struct
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
if CODE not in sys.path:
    sys.path.insert(0, CODE)

from deid_eval import fmt                       # noqa: E402  the teammate's classifier
from .engine import Engine                      # noqa: E402
from .pseudo import norm, pseudo                # noqa: E402
from . import gate as G                         # noqa: E402

TEXT_KINDS = {'step', 'dxf', 'svg', 'xml', 'obj', 'wrl', 'text', 'ps'}
SAME_LEN_TEXT = {'iges', 'sat', 'parasolid'}
CAD_ZIP = ('.fcstd', '.3mf', '.f3d', '.f3z')

# Tier A: routes whose output is emitted. Any other route is computed only when DEID_TIER=all, else the file keeps
# its content (path renamed). Set after the development-set evaluation (findings/integration.md).
TIER_A = {'text', 'text_same', 'pdf', 'zip', 'tar', 'gzip', 'Z', 'rar', 'png', 'stl_bin', 'stl_ascii', 'lock',
          'sw', 'inventor', 'ole_generic', 'creo', 'jt', 'nx', 'catia', '7z', 'parasolid_bin', 'dwg', 'iges', 'step'}
KEEP_ROUTES = {'step_big': 'KEPT STEP of 200 MB or more (string-slot streaming rewrite not wired in v3)',
               'bin': 'KEPT unknown binary', 'image_other': 'KEPT image',
               'ole_other': 'KEPT other OLE'}
PLACEHOLDERS = {'author', 'unknown', 'none', 'user', 'users', 'administrator', 'admin', 'owner', 'open cascade',
                'anonymous', '(anonymous)', 'default', 'name', 'company', 'organization', 'organisation', 'n/a', 'na',
                'test', 'student', 'windows user', 'your name', 'designer', 'engineer', 'creator', 'title'}


_FILE_EXT = re.compile(r'\.(zip|rar|7z|tar|gz|tgz|step|stp|igs|iges|sldprt|sldasm|slddrw|ipt|iam|idw|prt|asm|drw|'
                       r'fcstd|dxf|dwg|stl|sat|x_t|jt|pdf|png|jpe?g|svg|f3d|f3z|3mf|obj|wrl)$', re.I)


def _strip_ext(ids):
    """An identity form that ends in a file extension ('name.zip') would rewrite the extension inside content while
    rename() keeps it on disk: match the stem only, so disk and content agree (test c)."""
    out = {}
    for k, r in ids.items():
        k2 = _FILE_EXT.sub('', k)
        forms = [f for f in (_FILE_EXT.sub('', f) for f in r.get('forms', [])) if len(f) >= 4]
        if len(k2) < 4:
            continue
        if k2 in out:
            out[k2] = dict(out[k2], forms=sorted(set(out[k2].get('forms', [])) | set(forms)))
        else:
            out[k2] = dict(r, forms=forms)
    return out


class Setup:
    """Everything a worker needs; built once in the parent, shared by fork."""

    def __init__(self, ids_path, scoped_path=None, stop_path=None, tier=None):
        ids = _strip_ext(json.load(open(ids_path, encoding='utf-8')))
        self.E = Engine(ids)
        po = {}
        if scoped_path and os.path.exists(scoped_path):
            for k, r in json.load(open(scoped_path, encoding='utf-8')).items():
                al = re.sub(r'[\W_]', '', k)
                if r.get('mode') in ('path', 'file') and len(al) >= 3 and not al.isdigit() and len(set(al.lower())) > 1:
                    po[k] = dict(r, mode='word')
        self.P = Engine(po)                     # path-position identities: source folder name only (D11)
        stop = set(PLACEHOLDERS)
        if stop_path and os.path.exists(stop_path):
            for line in open(stop_path, encoding='utf-8'):
                v = line.split('\t')[0].strip()
                if v and not v.startswith('#'):
                    stop.add(norm(v))
        self.stop = stop
        self.tier = set(TIER_A) if tier is None else set(tier)
        self.M = Mapping(self)


class Mapping:
    """deid.mapping.Mapping API on top of the teammate's Engine and the team stand-in function, so our handlers (pdf, sw,
    creo, archives, text_step, misc) and his engine produce the same stand-in for the same name."""
    version = 'v2'

    def __init__(self, S):
        self.S = S

    def standin(self, value):
        return pseudo(value)

    def is_identity(self, value, source=None, in_field=False):
        return norm(value) in self.S.E.ids

    def sub_field(self, value, source=None):
        from .fieldrules import _KEEP_FIELD, TOOL
        v = value.strip(' \x00') if value else value
        if not v or not v.strip() or not re.search(r'\w', v) or norm(v) in self.S.stop or _KEEP_FIELD.match(v) \
                or TOOL.match(v):
            return value
        return pseudo(value)

    def sub_text(self, text, source=None):
        return self.S.E.sub_text(text)

    def sub_bytes(self, blob, source=None, encodings=None):
        return self.S.E.sub_bytes(blob, same_len=True)

    def file_matcher(self, values):
        """Per-file identities (D9): the global table plus identity-field values found in THIS file (initials too),
        matched as whole tokens by the team's filematch rules; same stand-in as everywhere else."""
        return FileMatcher(self.S, values)


class FileMatcher:
    def __init__(self, S, values):
        from .filematch import FileValues
        self.E = S.E
        self.fv = FileValues(list(values or ()), stop=S.stop)
        self.values = self.fv.values

    def _hits(self, data):
        hits = self.E.find(data) + [(a, b, None, enc) for a, b, enc in self.fv.find(data)]
        hits.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        out, last = [], -1
        for h in hits:
            if h[0] >= last:
                out.append(h); last = h[1]
        return out

    def sub_bytes(self, blob):
        return self.E._apply(blob, self._hits(blob), True)

    def sub_text(self, text):
        b = text.encode('utf-8', 'surrogatepass')
        out, n = self.E._apply(b, self._hits(b), False)
        return out.decode('utf-8', 'surrogatepass'), n


def rename(S, rel):
    """the teammate's rename (identity spans in folders and stem, extension never touched) + path-position identities on
    the source folder (sources/<kind>/<this>) only. sources/existing/<third> is never renamed by the second step."""
    d, base = os.path.split(rel)
    ef = fmt.ext_full(base)
    stem = base[:len(base) - len(ef)] if ef else base
    nd = S.E.sub_text(d)[0] if d else d
    ns = S.E.sub_text(stem)[0]
    parts = (os.path.join(nd, ns + ef) if nd else ns + ef).split('/')
    first = 3 if parts[:2] == ['sources', 'existing'] else 2      # sources/existing/<x> is never renamed
    for i in range(first if parts[0] == 'sources' else 0, len(parts) - 1):
        parts[i] = S.P.sub_text(parts[i])[0]
    return '/'.join(parts)


def _ext(rel):
    ext = fmt.ext_full(os.path.basename(rel)).lower()
    return re.sub(r'\.\d+$', '', ext) if ext.count('.') > 1 else ext


def route_of(rel, data):
    base = os.path.basename(rel)
    ext = _ext(rel)
    kind = fmt.classify(data, ext)
    if base.startswith('~$') and len(data) <= 4096:
        return kind, 'lock'
    if data[:2] == b'\x1f\x9d':
        return 'Z', 'Z'
    if ext == '.jt' and (data[:8] == b'Version ' or data[:1] in (b'#', b'!')):
        return 'jt', 'jt'
    if ext == '.prt' and data[:8] == b'SPLMSSTR':
        return 'nx', 'nx'
    if data[:7] == b'V5_CFV2' and ext.startswith('.cat'):
        return 'catia', 'catia'
    if ext in ('.x_b', '.xmt_bin'):
        return 'parasolid_bin', 'parasolid_bin'
    if kind == 'empty':
        return kind, 'empty'
    if kind in ('pdf', 'zip', 'tar', 'gzip', 'rar', '7z', 'dwg', 'creo', 'bin', 'parasolid_bin'):
        return kind, kind
    if kind == 'image':
        return kind, 'png' if data[:8] == b'\x89PNG\r\n\x1a\n' else 'image_other'
    if kind == 'sw':
        return kind, 'sw'
    if kind == 'ole':
        if ext in ('.sldprt', '.sldasm', '.slddrw', '.prtdot', '.asmdot', '.drwdot', '.slddrt', '.sldlfp'):
            return kind, 'sw'
        if ext in ('.ipt', '.iam', '.idw', '.ipn', '.ide'):
            return kind, 'inventor'
        if ext in ('.par', '.psm', '.asm', '.dft', '.pwd', '.prt'):
            return kind, 'ole_generic'
        return kind, 'ole_other'
    if kind == 'stl':
        if len(data) >= 84 and 84 + 50 * struct.unpack_from('<I', data, 80)[0] == len(data):
            return kind, 'stl_bin'
        return kind, 'stl_ascii' if data[:5].lower() == b'solid' else 'bin'
    if kind == 'step':
        return kind, 'step'
    if kind in TEXT_KINDS:
        return kind, 'text'
    if kind == 'iges':
        return kind, 'iges'
    if kind == 'step':
        return kind, 'step'
    if kind in SAME_LEN_TEXT:
        return kind, 'text_same'
    return kind, 'bin'


def _ctx(S, rel, depth):
    from deid.api import Ctx, Result

    def rec(b, member):
        out, r = process(S, member.split('!/')[-1], b, depth + 1)
        return Result(None if out is b or out == b else out, r['n'], 'ok' if out != b else 'unchanged', r['notes'])
    return Ctx(old_path=rel, new_path=rel, source='/'.join(rel.split('/')[:3]), mapping=S.M,
               rename=lambda s: rename(S, s), recurse=rec)


def _native(S, modname, rel, data, depth):
    import importlib
    mod = importlib.import_module('deid.handlers.' + modname)
    r = mod.handle(data, _ctx(S, rel, depth))
    notes = list(r.notes or [])
    if r.status == 'failed' or r.data is None:
        return data, 0, notes
    return r.data, r.changed, notes


def _do_sw_fast(E, data):
    """SolidWorks 2015+: the teammate's do_sw method (sections inflated, edited at the same length, re-deflated and NUL-padded
    to their original compressed size, CRC updated; no offset moves), with matching restricted to readable text
    (engine.sub_readable, council D9) in sections and in the bytes between them. A section that does not fit stays as
    it is and the file is marked PARTIAL (D5). Selected by DEID_SW=fast; the default is the native handler (fewer names left on 120 files, findings/integration.md)."""
    import zlib
    from deid.native import swnew
    secs = fmt.sw_parse(data)
    if not secs:
        return data, 0, ['sw: no sections parsed']
    locs = swnew.sw_locals(data)
    by_at = {l['crc_at']: l for l in locs}
    cen = {}
    for c in swnew.sw_central(data, locs):                 # central-directory copies of each record's CRC
        cen.setdefault(c['name'], []).append(c['crc_at'])
    out, n, prev, unfit = bytearray(data), 0, 0, 0
    for i, a, comp, unc, raw in secs:
        gap, k = E.sub_readable(bytes(out[prev:i]))
        if k:
            out[prev:i] = gap; n += k
        new, k = E.sub_readable(raw)
        loc = by_at.get(i + 10)
        if k and (loc is None or not cen.get(loc['name'])):
            unfit += 1; k = 0                               # no central record found: leave this section as it is
        if k:
            best = None
            for lvl in (6, 9, 1):
                c = zlib.compressobj(lvl, zlib.DEFLATED, -15, 9)
                c = c.compress(new) + c.flush()
                if len(c) <= comp:
                    best = c; break
            if best is None:
                unfit += 1
            else:
                out[a:a + comp] = best + b'\x00' * (comp - len(best))
                crc = struct.pack('<I', zlib.crc32(new) & 0xffffffff)
                out[i + 10:i + 14] = crc
                for at in cen[loc['name']]:
                    out[at:at + 4] = crc
                n += k
        prev = a + comp
    tail, k = E.sub_readable(bytes(out[prev:]))
    if k:
        out[prev:] = tail; n += k
    return bytes(out), n, (['PARTIAL sw: %d sections did not fit' % unfit] if unfit else [])


STEP_CHUNKED = 32_000_000


def sub_chunked(E, data, chunk=32 << 20):
    """Same output as E.sub_bytes(data) for text: cut only right after a newline (identities never span one, and
    the boundary test sees a non-alphanumeric neighbour either way), so memory stays near 2 x chunk + file size."""
    if len(data) <= 2 * chunk:
        return E.sub_bytes(data, same_len=True)
    parts, n, pos = [], 0, 0
    while pos < len(data):
        end = min(len(data), pos + chunk)
        if end < len(data):
            nl = data.find(b'\n', end)
            end = len(data) if nl < 0 else nl + 1
        o, k = E.sub_bytes(data[pos:end], same_len=True)
        parts.append(o); n += k; pos = end
    return b''.join(parts), n


def _edit(S, route, rel, data, depth):
    """Returns (out, n, notes). Never called for routes outside the tier."""
    E, M = S.E, S.M
    ren = lambda s: rename(S, s)
    sub = lambda t: E.sub_text(t)
    field = M.sub_field
    if route == 'text':
        if data[:4] == b'\xc5\xd0\xd3\xc6':                  # EPS with a binary preview: readable runs only
            out, n = E.sub_readable(data)
        else:
            out, n = E.sub_cad(data)
        return out, n, []
    if route == 'text_same':
        out, n = E.sub_cad(data)
        return out, n, []
    if route in ('iges', 'step'):                         # core builder's rewriters: string slots only, never numbers
        from deid.handlers import text_step
        cs = lambda t: E.sub_cad_text(t)
        old = os.path.basename(rel); new = os.path.basename(rename(S, rel))
        names = [(old, new)] if old != new and len(old) == len(new) else []
        if names and len(old.rsplit('.', 1)[0]) >= 4:
            names.append((old.rsplit('.', 1)[0], new.rsplit('.', 1)[0]))
        if route == 'step' and len(data) >= STEP_CHUNKED:
            import tempfile
            td = os.environ.get('DEID_TMP') or None
            with tempfile.NamedTemporaryFile(dir=td, suffix='.stp') as fi, tempfile.NamedTemporaryFile(dir=td, suffix='.stp') as fo:
                fi.write(data); fi.flush()
                n = text_step.rewrite_step_file(fi.name, fo.name, cs, field, names)
                n = n[1] if isinstance(n, tuple) else n
                out = open(fo.name, 'rb').read()
            return out, n or 0, ['step chunked']
        f = text_step.rewrite_iges if route == 'iges' else text_step.rewrite_step
        out, n = f(data, cs, field, names)
        return (data if out is None else out), n or 0, []
    if route == 'stl_bin':
        h, n = E.sub_bytes(data[:80], same_len=True)
        return (h + data[80:], n, []) if n else (data, 0, [])
    if route == 'stl_ascii':
        nl = data.find(b'\n')
        nl = len(data) if nl < 0 else nl
        h, n = E.sub_cad(data[:nl])                          # the solid name line; facets never touched
        e = data.rfind(b'endsolid')
        if e > nl:
            t, k = E.sub_cad(data[e:])
            return h + data[nl:e] + t, n + k, []
        return h + data[nl:], n, []
    if route == 'lock':
        from deid.handlers import misc
        out, n = misc.lockfile(data, field)
        return out, n, []
    if route == 'png':
        from .andre_image import do_image
        out, n = do_image(E, data)
        return out, n, []
    if route == 'pdf':
        from deid.handlers import pdf as P
        res, _st = P.process(data, _ctx(S, rel, depth))
        if res.data is None or res.status == 'failed':
            return data, 0, list(res.notes or [])
        return res.data, res.changed or 1, list(res.notes or [])
    if route in ('zip', 'tar', 'gzip', 'Z', 'rar'):
        from deid.handlers import archive as A

        def recb(b, member):
            out, _r = process(S, member, b, depth + 1)
            return None if out == b else out
        own = os.path.basename(rel)
        if route == 'zip':
            r = A.rewrite_zip(data, recb, ren, sub, field, member_names=not rel.lower().endswith(CAD_ZIP))
        elif route == 'tar':
            r = A.rewrite_tar(data, recb, ren, sub, field)
        elif route == 'gzip':
            r = A.rewrite_gzip(data, recb, ren, sub, own)
        elif route == 'Z':
            r = A.rewrite_z(data, recb, own)
        else:
            r = A.rewrite_rar(data, recb, ren)
        out, n, notes = r
        return (data if out is None else out), n, list(notes or [])
    if route == '7z':
        from deid.handlers import archive as A

        def recb7(b, member):
            out, _r = process(S, member, b, depth + 1)
            return None if out == b else out
        out, n, notes = A.rewrite_7z(data, recb7, ren)
        return (data if out is None else out), n, list(notes or [])
    if route == 'parasolid_bin':
        from deid.handlers import misc
        r = misc.rewrite_xb(data, lambda t: E.sub_text(t), field)
        out, n = r[0], r[1]
        return (data if out is None else out), n, []
    if route in ('jt', 'nx', 'catia', 'dwg'):           # dwg: handler sends the original unless LibreDWG agrees
        return _native(S, route, rel, data, depth)
    if route == 'creo':
        sys.path.insert(0, HERE)
        from . import creo_route as dropins
        out, n = dropins.do_creo(E, data)
        return out, n, []
    if route == 'sw' and data[4:8] == b'\x00\x00\x00\x04' and os.environ.get('DEID_SW') == 'fast':
        return _do_sw_fast(E, data)
    if route in ('sw', 'inventor', 'ole_generic'):
        return _native(S, route, rel, data, depth)
    return data, 0, ['no route']


def process(S, rel, data, depth=0):
    kind, route = route_of(rel, data)
    rec = {'kind': kind, 'route': route, 'n': 0, 'status': 'unchanged', 'notes': []}
    if route == 'empty':
        return data, rec
    if route in KEEP_ROUTES or (route not in S.tier and os.environ.get('DEID_TIER') != 'all'):
        rec['status'] = 'kept'
        rec['notes'].append(KEEP_ROUTES.get(route, 'KEPT %s (not in tier A)' % route))
        return data, rec
    try:
        out, n, notes = _edit(S, route, rel, data, depth)
    except Exception as ex:                                   # a handler error costs only this file
        rec.update(status='error', notes=['ERROR %s: %s' % (type(ex).__name__, str(ex)[:120])])
        if os.environ.get('DEID_DEBUG'):
            traceback.print_exc()
        return data, rec
    rec['notes'] += [x for x in notes if isinstance(x, str)][:5]
    if out is None or out == data:
        return data, rec
    ok, why = G.gate(route, rel, data, out)
    if not ok:
        rec.update(status='gate-failed', n=0)
        rec['notes'].append('GATE %s' % why)
        return data, rec
    rec.update(status='ok', n=n or 1)
    return out, rec
