"""Output gate (integrator's code, MIT). gate(route, rel, orig, out) -> (ok, why).

Every edited output is re-opened by a reader that can refuse it, and compared with the same reader's view of the
original. If the original itself does not open, the edit must keep the reader's verdict and the structure counts.
A refused edit is never emitted: the caller writes the original bytes under the new name and lists the file.
"""
import gzip
import io
import os
import re
import subprocess
import tarfile
import tempfile
import zipfile
import zlib

import numpy as np

_ALNUM = np.zeros(256, bool)
for _c in b'0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ':
    _ALNUM[_c] = True
_ALNUM[0x80:] = True          # UTF-8 continuation/lead bytes of accented letters (same-width stand-ins)
TMP = os.environ.get('DEID_TMP') or os.path.expanduser('~/hack-osu/private/tmp')
UNRAR = os.environ.get('DEID_UNRAR', os.path.expanduser('~/hack-osu/work/gaps/build/unrar/unrar'))


def same_shape(orig, out):
    """Same length and every changed byte is a letter/digit in both (a stand-in never touches syntax)."""
    if len(orig) != len(out):
        return False, 'length %d->%d' % (len(orig), len(out))
    a = np.frombuffer(orig, np.uint8)
    b = np.frombuffer(out, np.uint8)
    d = np.nonzero(a != b)[0]
    if len(d) and not (_ALNUM[a[d]].all() and _ALNUM[b[d]].all()):
        return False, 'non-alnum byte changed'
    return True, ''


def _try(f, *a):
    try:
        return True, f(*a)
    except Exception as ex:      # noqa: BLE001  any refusal counts
        return False, type(ex).__name__


def _pdf(b, strict=False):
    """Page count, page boxes, and per page: /Type, number of fonts and XObjects in its resources. strict: no
    recovery (qpdf refuses a damaged xref instead of rebuilding it)."""
    import pikepdf
    with pikepdf.open(io.BytesIO(b), attempt_recovery=not strict) as p:
        pages = []
        for pg in p.pages[:200]:
            o = pg.obj
            res = o.get('/Resources') or {}
            fonts = res.get('/Font') if hasattr(res, 'get') else None
            xo = res.get('/XObject') if hasattr(res, 'get') else None
            pages.append((str(o.get('/Type')), len(fonts.keys()) if fonts is not None else 0,
                          len(xo.keys()) if xo is not None else 0,
                          tuple(round(float(x), 1) for x in pg.mediabox)))
        root = p.Root.get('/Pages')
        return len(p.pages), pages, str(root.get('/Type')) if root is not None else ''


def _zip(b):
    z = zipfile.ZipFile(io.BytesIO(b))
    names = z.namelist()
    for i in z.infolist():
        if i.compress_type in (0, 8, 12, 14) and not i.flag_bits & 1:
            z.read(i)                                       # CRC checked by zipfile
        elif i.compress_type == 93 and not i.flag_bits & 1:
            import zstandard
            z.fp.seek(i.header_offset + 26)
            n, m = int.from_bytes(z.fp.read(2), 'little'), int.from_bytes(z.fp.read(2), 'little')
            z.fp.seek(i.header_offset + 30 + n + m)
            raw = zstandard.ZstdDecompressor().decompress(z.fp.read(i.compress_size), max_output_size=i.file_size)
            if zlib.crc32(raw) & 0xffffffff != i.CRC:
                raise ValueError('zstd crc')
    return len(names)


def _tar(b):
    t = tarfile.open(fileobj=io.BytesIO(b))
    n = 0
    for m in t.getmembers():
        n += 1
        if m.isfile():
            t.extractfile(m).read()
    return n


def _rar(b):
    os.makedirs(TMP, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix='.rar', dir=TMP) as f:
        f.write(b); f.flush()
        r = subprocess.run([UNRAR, 't', '-idq', '-p-', f.name], capture_output=True, timeout=600)
        if r.returncode != 0:
            raise ValueError('unrar rc %d' % r.returncode)
    return 1


def _z(b):
    import unlzw3
    return len(unlzw3.unlzw(b))


def _sw(b):
    from deid_eval import fmt
    secs = fmt.sw_parse(b)
    return [(i, a, comp, unc, zlib.crc32(raw) & 0xffffffff) for i, a, comp, unc, raw in secs]


def _ole(b):
    import olefile
    o = olefile.OleFileIO(b)
    s = sorted(('/'.join(p), o.get_size('/'.join(p))) for p in o.listdir(streams=True, storages=False))
    for p, _ in s:
        o.openstream(p).read()
    o.close()
    return s


def _creo(b):
    from . import creo as C
    secs, ok, nl, lok, bad = C.verify(b)
    return secs, ok


def _xml(b):
    import xml.parsers.expat
    p = xml.parsers.expat.ParserCreate()
    p.Parse(b, True)
    return 1


def _dxf(b):
    import ezdxf
    from ezdxf.lldxf.validator import is_dxf_stream  # noqa: F401
    txt = b.decode('utf-8', 'surrogateescape') if b[:1] != b'\x00' else b.decode('latin-1')
    doc = ezdxf.read(io.StringIO(txt))
    return len(doc.modelspace())


def _img(b):
    from PIL import Image
    im = Image.open(io.BytesIO(b))
    im.load()
    return im.size, im.mode


def _xz_streams(b):
    """(XZ stream headers found, streams that decode to their end with the standard library's lzma)."""
    import lzma
    n = ok = 0
    i = b.find(b'\xfd7zXZ\x00')
    while i >= 0:
        n += 1
        try:
            d = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
            d.decompress(b[i:])
            ok += d.eof
        except lzma.LZMAError:
            pass
        i = b.find(b'\xfd7zXZ\x00', i + 6)
    return n, ok


def _preview_only(orig, out):
    """CATIA drawing preview replaced by a flat tile: every non-alphanumeric change must sit inside one JPEG
    (FFD8 ... FFD9) of the original, and the same span of the output must decode as an image of the same size."""
    a = np.frombuffer(orig, np.uint8); b = np.frombuffer(out, np.uint8)
    d = np.nonzero(a != b)[0]
    bad = d[~(_ALNUM[a[d]] & _ALNUM[b[d]])]
    if not len(bad):
        return True, ''
    lo, hi = int(bad[0]), int(bad[-1])
    s = orig.rfind(b'\xff\xd8\xff', 0, lo + 1)
    e = orig.find(b'\xff\xd9', hi - 1)
    if s < 0 or e < 0:
        return False, 'change outside any preview'
    o1, v1 = _try(_img, orig[s:e + 2]); o2, v2 = _try(_img, out[s:e + 2])
    if not (o1 and o2 and v1[0] == v2[0]):
        return False, 'preview span does not decode the same size'
    return True, 'preview'


_NUM = re.compile(rb'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?')
_STEP_STR = re.compile(rb"'(?:[^']|'')*'", re.S)


_HOLL = re.compile(rb'(?<=[,;])\s*(\d+)H')
_SAT = re.compile(rb'@(\d+) ')


def _blank_counted(b, rx, skip_after=True):
    out, pos = [], 0
    for m in rx.finditer(b):
        if m.start() < pos:
            continue
        n = int(m.group(1)); s = m.end()
        out.append(b[pos:s]); out.append(b' ' * min(n, len(b) - s)); pos = s + n
    out.append(b[pos:])
    return b''.join(out)


def numbers_equal(a, b, step=False, iges=False, sat=False):
    """V3: the sequence of numeric tokens (STEP: outside quoted strings) must be identical."""
    import hashlib
    if step:
        a, b = _STEP_STR.sub(b"''", a), _STEP_STR.sub(b"''", b)
    if iges:
        a, b = _blank_counted(a, _HOLL), _blank_counted(b, _HOLL)
    if sat:
        a, b = _blank_counted(a, _SAT), _blank_counted(b, _SAT)
    ha, hb = hashlib.sha256(), hashlib.sha256()
    for m in _NUM.finditer(a):
        ha.update(m.group(0) + b',')
    for m in _NUM.finditer(b):
        hb.update(m.group(0) + b',')
    return ha.digest() == hb.digest()


_STEP_ENT = re.compile(rb'(?m)^\s*#\d+\s*=')


def gate(route, rel, orig, out):
    ext = os.path.splitext(rel.lower())[1]
    if out == orig:
        return True, 'same'
    if route == 'pdf':
        s1, v1 = _try(_pdf, orig, True)
        o1 = s1 or _try(_pdf, orig)[0]
        if not s1 and o1:
            v1 = _try(_pdf, orig)[1]
        o2, v2 = _try(_pdf, out, True)            # our writer always produces a clean xref: never accept recovery
        if not o2:
            return False, 'pikepdf (no recovery) refuses: %s' % v2
        if o1 and v1[0] != v2[0]:
            return False, 'pages %d->%d' % (v1[0], v2[0])
        if o1:
            if v1[2] != v2[2]:
                return False, 'page tree root /Type changed'
            for a, b in zip(v1[1], v2[1]):          # stand-ins may ADD a font (Helvetica); nothing else may change
                if a[0] != b[0] or a[2] != b[2] or a[3] != b[3] or b[1] < a[1]:
                    return False, 'page type, fonts, xobjects or size changed'
        return True, 'pikepdf'
    if route in ('zip', 'tar', 'gzip', 'Z', 'rar'):
        f = {'zip': _zip, 'tar': _tar, 'gzip': lambda b: len(gzip.decompress(b)), 'Z': _z, 'rar': _rar}[route]
        o1, v1 = _try(f, orig)
        o2, v2 = _try(f, out)
        if not o2:
            return False, '%s reader refuses: %s' % (route, v2)
        if o1 and route in ('zip', 'tar') and v1 != v2:
            return False, 'members %s->%s' % (v1, v2)
        if route == 'zip' and ext == '.fcstd':
            z = zipfile.ZipFile(io.BytesIO(out))
            if 'Document.xml' in z.namelist():
                ok, why = _try(_xml, z.read('Document.xml'))
                if not ok:
                    return False, 'Document.xml does not parse'
        return True, route
    if route == 'sw':
        if len(orig) != len(out):
            return False, 'size changed'
        o1, v1 = _try(_sw, orig)
        o2, v2 = _try(_sw, out)
        if o1 and v1:
            if not o2:
                return False, 'sw container no longer parses: %s' % v2
            if [x[:4] for x in v1] != [x[:4] for x in v2]:
                return False, 'sw section layout changed'
            for (i, _a, _c, _u, c1), (_i, _a2, _c2, _u2, c2) in zip(v1, v2):
                if int.from_bytes(orig[i + 10:i + 14], 'little') == c1 and int.from_bytes(out[i + 10:i + 14], 'little') != c2:
                    return False, 'sw section crc stale'
            from deid.native import swnew
            l1, l2 = swnew.sw_locals(orig), swnew.sw_locals(out)
            c1, c2 = swnew.sw_central(orig, l1), swnew.sw_central(out, l2)
            if len(l1) != len(l2) or len(c1) != len(c2):
                return False, 'sw local/central records %d/%d -> %d/%d' % (len(l1), len(c1), len(l2), len(c2))
            return True, 'sw sections + central CRC'
        o1, v1 = _try(_ole, orig)
        o2, v2 = _try(_ole, out)
        if o1 and (not o2 or v1 != v2):
            return False, 'olefile %s' % (v2 if not o2 else 'streams changed')
        return True, 'sw ole'
    if route in ('inventor', 'ole_generic'):
        if len(orig) != len(out):
            return False, 'size changed'
        o1, v1 = _try(_ole, orig)
        o2, v2 = _try(_ole, out)
        if o1 and not o2:
            return False, 'olefile refuses: %s' % v2
        if o1 and v1 != v2:
            return False, 'stream list/sizes changed'
        return True, 'olefile'
    if route == 'creo':
        o1, v1 = _try(_creo, orig)
        o2, v2 = _try(_creo, out)
        if not o2 or (o1 and (v1[0] != v2[0] or v2[1] < v1[1])):
            return False, 'creo verify %s -> %s' % (v1, v2)
        return True, 'creo.verify'
    if route == '7z':
        from deid.handlers import archive as A
        o1, v1 = _try(A.read_7z, orig)
        o2, v2 = _try(A.read_7z, out)
        if not o2:
            return False, 'libarchive refuses 7z: %s' % v2
        if o1 and len(v1) != len(v2):
            return False, '7z members %d -> %d' % (len(v1), len(v2))
        return True, 'libarchive'
    if route == 'dwg':
        if len(orig) != len(out):
            return False, 'dwg size changed'
        from . import dwg_inplace as DI
        o1, a0 = _try(DI.audit, bytearray(orig))
        if o1:
            o2, a1 = _try(DI.audit, bytearray(out))
            if not o2 or a1['pages_ok'] < a0['pages_ok'] or a1['objects'] < a0['objects'] or a1['obj_bad'] > a0['obj_bad']:
                return False, 'dwg audit worse'
        return True, 'dwg audit'
    if route in ('jt', 'nx', 'catia', 'parasolid_bin'):
        if len(orig) != len(out):
            return False, 'size changed'
        if route in ('nx', 'parasolid_bin', 'catia'):
            ok, why = same_shape(orig, out)
            if not ok and route == 'catia':
                ok, why = _preview_only(orig, out)
            if not ok:
                return False, route + ' ' + why
        if route == 'jt':
            a, b = _xz_streams(orig), _xz_streams(out)
            if a[0] != b[0] or b[1] < a[1]:
                return False, 'jt xz streams %s -> %s' % (a, b)
        return True, route
    if route == 'png':
        o1, v1 = _try(_img, orig)
        o2, v2 = _try(_img, out)
        if o1 and (not o2 or v1 != v2):
            return False, 'PIL %s->%s' % (v1, v2)
        return True, 'PIL'
    if route == 'stl_bin':
        if len(orig) != len(out) or orig[80:] != out[80:]:
            return False, 'stl body changed'
        return True, 'stl header only'
    # text-like and same-length routes: syntax must be untouched, and no number may change
    ok, why = same_shape(orig, out)
    if not ok:
        return False, why
    if route in ('text', 'text_same', 'stl_ascii', 'iges', 'step'):
        if not numbers_equal(orig, out, step=(route == 'step' or b'ISO-10303-21' in orig[:400]), iges=(route == 'iges'),
                             sat=rel.lower().endswith(('.sat', '.sab'))):
            return False, 'NUMBER GATE: numeric tokens changed'
    if route == 'text' and (b'ISO-10303-21' in orig[:400]):
        if len(_STEP_ENT.findall(orig)) != len(_STEP_ENT.findall(out)):
            return False, 'STEP entity count changed'
    if route == 'text' and (ext in ('.svg', '.xml', '.xaml', '.urdf', '.sdf', '.kicad_sym') or orig.lstrip()[:5] == b'<?xml'):
        o1, _ = _try(_xml, orig)
        if o1 and not _try(_xml, out)[0]:
            return False, 'XML no longer parses'
    if route == 'text' and ext == '.dxf' and len(orig) < 30_000_000:
        o1, v1 = _try(_dxf, orig)
        if o1:
            o2, v2 = _try(_dxf, out)
            if not o2 or v1 != v2:
                return False, 'ezdxf %s -> %s' % (v1, v2)
    if route in ('text_same', 'iges') and orig[:1] != b'\x00':
        if orig.count(b'\n') != out.count(b'\n'):
            return False, 'line count changed'
    return True, 'shape'
