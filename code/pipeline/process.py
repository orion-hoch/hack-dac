"""Per-format rewriting. Every handler returns new bytes; on any doubt it falls back to a safe variant."""
import gzip
import io
import os
import re
import struct
import tarfile
import zipfile
import zlib

try:  # V2: PyMuPDF is AGPL; optional, used only by the local scorer and the legacy PDF path
    import pymupdf
except ImportError:
    pymupdf = None

from deid_eval import fmt

pymupdf.TOOLS.mupdf_display_errors(False) if pymupdf else None

SKIP_STREAM = re.compile(r'/Subtype\s*/Image|/FontFile|/Length1|/Length2|/Length3|/DCTDecode|/JPXDecode|/CCITTFax|/JBIG2|/Type\s*/ObjStm|/Type\s*/XRef|/ICCBased|/N\s+[34]\b.*?/Alternate')
TEXT_KINDS = {'step', 'dxf', 'svg', 'xml', 'obj', 'wrl', 'text', 'ps'}           # length may change
SAME_LEN_KINDS = {'iges', 'sat', 'parasolid', 'parasolid_bin', 'creo', 'dwg', 'bin', 'stl', 'ole', 'sw'}


def rename(engine, rel):
    """Rename identity spans in a relative path; never touch the extension."""
    d, base = os.path.split(rel)
    ef = fmt.ext_full(base)
    stem = base[:len(base) - len(ef)] if ef else base
    nd = engine.sub_text(d, path=True)[0] if d else d
    ns = engine.sub_text(stem, path=True)[0]
    return os.path.join(nd, ns + ef) if nd else ns + ef


def process(engine, rel, data, depth=0):
    """Return (new_bytes, n_replacements, kind)."""
    ext = fmt.ext_full(os.path.basename(rel)).lower()
    ext = re.sub(r'\.\d+$', '', ext) if ext.count('.') > 1 else ext
    kind = fmt.classify(data, ext)
    if kind == 'empty':
        return data, 0, kind
    try:
        if kind == 'pdf':
            out, n = do_pdf(engine, data, depth)
        elif kind == 'zip':
            out, n = do_zip(engine, data, depth)
        elif kind == 'gzip':
            out, n = do_gzip(engine, rel, data, depth)
        elif kind == 'tar':
            out, n = do_tar(engine, data, depth)
        elif kind == 'image':
            out, n = do_image(engine, data)
        elif kind == 'sw':
            out, n = do_sw(engine, data)
        elif kind == 'ole':
            out, n = do_ole(engine, data)
        elif kind in TEXT_KINDS:
            out, n = engine.sub_bytes(data, same_len=False)
        else:   # binary / length-sensitive formats, incl. 7z/rar headers (best effort)
            out, n = engine.sub_bytes(data, same_len=True)
        if n == 0:
            return data, 0, kind          # nothing identifying found: keep the original bytes exactly
        return out, n, kind
    except Exception:
        out, n = engine.sub_bytes(data, same_len=True)
        return out, n, kind + '!fallback'


def do_pdf(engine, data, depth):
    doc = pymupdf.open(stream=data, filetype='pdf')
    if doc.needs_pass:
        return engine.sub_bytes(data, same_len=True)
    n = 0
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref, compressed=False)
            if doc.xref_is_stream(xref) and not SKIP_STREAM.search(obj or ''):
                raw = doc.xref_stream(xref)
                if raw:
                    new, k = engine.sub_bytes(raw, same_len=False)
                    if k:
                        doc.update_stream(xref, new); n += k
            if obj and not obj.startswith('null'):
                newo, k = engine.sub_text(obj)
                if k:
                    doc.update_object(xref, newo); n += k
        except Exception:
            continue
    meta = doc.metadata or {}
    nm = {}
    for k2, v in meta.items():
        if isinstance(v, str) and k2 not in ('format', 'encryption'):
            nv, k = engine.sub_text(v); nm[k2] = nv; n += k
    try:
        doc.set_metadata({k: v for k, v in nm.items() if k in ('author', 'title', 'subject', 'keywords', 'creator', 'producer', 'creationDate', 'modDate')})
    except Exception:
        pass
    # embedded files: process recursively and rename
    for name in list(doc.embfile_names()):
        try:
            inner = doc.embfile_get(name)
            new_inner, k, _ = process(engine, name, inner, depth + 1)
            info = doc.embfile_info(name)
            newname = engine.sub_text(name)[0]
            fn = engine.sub_text(info.get('filename') or name)[0]
            doc.embfile_upd(name, buffer_=new_inner, filename=fn, ufilename=fn, desc=engine.sub_text(info.get('description') or '')[0])
            n += k
        except Exception:
            continue
    n += pdf_visible_text(engine, doc)
    if n == 0:
        return data, 0
    out = doc.tobytes(garbage=1, deflate=True)   # full rewrite drops old incremental revisions
    return out, n


def pdf_visible_text(engine, doc, max_pages=60):
    """Names drawn as text that the stream-level pass could not see (hex strings, TJ kerning arrays, CID fonts):
    find them in the extracted page text, then redact just those glyphs and write the pseudonym in place.
    Line art and images are left untouched."""
    from .pseudo import pseudo
    n = 0
    for pno in range(min(doc.page_count, max_pages)):
        page = doc[pno]
        text = page.get_text()
        if not text:
            continue
        tb = text.encode('utf-8')
        hits = engine.find(tb)
        if not hits:
            continue
        terms = {}
        for s_, e_, iid, enc in hits:
            surf = tb[s_:e_].decode('utf-8', 'ignore')
            if surf.strip() and '\n' not in surf:
                terms[surf] = pseudo(surf, iid)
        k = 0
        for surf, rep in terms.items():
            for q in page.search_for(surf, quads=True):
                r = q.rect
                if r.is_empty or r.height <= 0:
                    continue
                fs = max(2.0, min(r.height * 0.75, 40.0))
                page.add_redact_annot(q, text=rep, fontname='helv', fontsize=fs, fill=False, cross_out=False)
                k += 1
        if k:
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                  text=pymupdf.PDF_REDACT_TEXT_REMOVE)
            n += k
    return n


def do_zip(engine, data, depth):
    zin = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, 'w') as zout:
        for info in zin.infolist():
            content = zin.read(info) if not info.is_dir() else b''
            newname = rename(engine, info.filename.rstrip('/')) + ('/' if info.filename.endswith('/') else '')
            if newname != info.filename:
                n += 1
            if content and depth < 4:
                content, k, _ = process(engine, info.filename, content, depth + 1)
                n += k
            zi = zipfile.ZipInfo(newname, date_time=info.date_time)
            zi.compress_type = info.compress_type if info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) else zipfile.ZIP_DEFLATED
            zi.external_attr = info.external_attr
            zi.create_system = info.create_system
            zi.comment = engine.sub_bytes(info.comment, same_len=False)[0]
            zout.writestr(zi, content)
        zout.comment = engine.sub_bytes(zin.comment, same_len=False)[0]
    return buf.getvalue(), n


def do_gzip(engine, rel, data, depth):
    inner = gzip.decompress(data)
    new_inner, n, _ = process(engine, rel[:-3] if rel.lower().endswith('.gz') else rel, inner, depth + 1)
    # rebuild header without the original file name field; keep mtime
    mtime = struct.unpack_from('<I', data, 4)[0]
    return gzip.compress(new_inner, mtime=mtime), n


def do_tar(engine, data, depth):
    tin = tarfile.open(fileobj=io.BytesIO(data))
    buf = io.BytesIO()
    n = 0
    with tarfile.open(fileobj=buf, mode='w', format=tin.format or tarfile.GNU_FORMAT) as tout:
        for m in tin.getmembers():
            content = tin.extractfile(m).read() if m.isfile() else None
            m2 = tarfile.TarInfo(rename(engine, m.name))
            for a in ('mode', 'mtime', 'type', 'uid', 'gid', 'devmajor', 'devminor'):
                setattr(m2, a, getattr(m, a))
            m2.linkname = engine.sub_text(m.linkname)[0] if m.linkname else ''
            m2.uname = engine.sub_text(m.uname or '')[0]
            m2.gname = engine.sub_text(m.gname or '')[0]
            if content is not None:
                if depth < 4:
                    content, k, _ = process(engine, m.name, content, depth + 1); n += k
                m2.size = len(content)
                tout.addfile(m2, io.BytesIO(content))
            else:
                tout.addfile(m2)
    return buf.getvalue(), n


def do_image(engine, data):
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        out, pos, n = [data[:8]], 8, 0
        while pos + 8 <= len(data):
            L = struct.unpack('>I', data[pos:pos + 4])[0]
            typ = data[pos + 4:pos + 8]
            body = data[pos + 8:pos + 8 + L]
            if typ in (b'tEXt', b'iTXt', b'eXIf'):
                body, k = engine.sub_bytes(body, same_len=False); n += k
            elif typ == b'zTXt':
                try:
                    kw, rest = body.split(b'\x00', 1)
                    txt = zlib.decompress(rest[1:])
                    txt, k = engine.sub_bytes(txt, same_len=False); n += k
                    body = kw + b'\x00' + rest[:1] + zlib.compress(txt)
                except Exception:
                    pass
            out.append(struct.pack('>I', len(body)) + typ + body + struct.pack('>I', zlib.crc32(typ + body) & 0xffffffff))
            pos += 12 + L
            if typ == b'IEND':
                break
        out.append(data[pos:])
        return b''.join(out), n
    return engine.sub_bytes(data, same_len=True)


def _deflate(data, level):
    c = zlib.compressobj(level, zlib.DEFLATED, -15, 9)
    return c.compress(data) + c.flush()


def do_sw(engine, data):
    """Rewrite SolidWorks 2015+ sections in place: same-length substitution inside each inflated section,
    recompress, zero-pad to the original compressed size (inflate ignores trailing bytes), fix the CRC.
    No offsets move. Bytes outside sections get same-length raw substitution."""
    secs = fmt.sw_parse(data)
    if not secs:
        return engine.sub_bytes(data, same_len=True)
    out = bytearray(data)
    n, prev = 0, 0
    for i, a, comp, unc, raw in secs:
        gap, k = engine.sub_bytes(bytes(out[prev:i]), same_len=True)
        if k:
            out[prev:i] = gap; n += k
        new, k = engine.sub_bytes(raw, same_len=True)
        if k:
            best = None
            for lvl in (9, 6, 1):
                c = _deflate(new, lvl)
                if len(c) <= comp and (best is None or len(c) < len(best)):
                    best = c
            if best is not None:
                out[a:a + comp] = best + b'\x00' * (comp - len(best))
                st_pack = struct.pack('<I', zlib.crc32(new) & 0xffffffff)
                out[i + 10:i + 14] = st_pack
                n += k
        prev = a + comp
    tail, k = engine.sub_bytes(bytes(out[prev:]), same_len=True)
    if k:
        out[prev:] = tail; n += k
    return bytes(out), n


def do_ole(engine, data):
    """OLE natives: same-length substitution in raw bytes, plus zlib-compressed streams (Inventor) rewritten
    in place: inflate, substitute, re-deflate, zero-pad to the original compressed length. Stream sizes never change."""
    import olefile
    import tempfile
    out, n = engine.sub_bytes(data, same_len=True)
    try:
        ole = olefile.OleFileIO(out)
        todo = []
        for s_ in ole.listdir(streams=True, storages=False):
            raw = ole.openstream(s_).read()
            z = fmt.ole_inflate(raw)
            if z is None:
                continue
            off, inner, consumed = z
            new, k = engine.sub_bytes(inner, same_len=True)
            if not k:
                continue
            for lvl in (9, 6):
                c = zlib.compress(new, lvl)
                if len(c) <= consumed:
                    todo.append((s_, raw[:off] + c + b'\x00' * (consumed - len(c)) + raw[off + consumed:], k))
                    break
        ole.close()
        if todo:
            with tempfile.NamedTemporaryFile(suffix='.ole', delete=False, dir=os.environ.get('DEID_TMP')) as tf:
                tf.write(out); tmp = tf.name
            try:
                w = olefile.OleFileIO(tmp, write_mode=True)
                for s_, newraw, k in todo:
                    w.write_stream(s_, newraw); n += k
                w.close()
                with open(tmp, 'rb') as fh:
                    out = fh.read()
            finally:
                os.remove(tmp)
    except Exception:
        pass
    return out, n
