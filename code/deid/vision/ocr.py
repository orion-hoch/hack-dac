"""Shared OCR helper for pixels (rendered PDF pages, raster images, previews). Owned by the PIXELS builder.

read_words(img, rotate_pass=True) -> [dict(text, quad, score, angle)]
    img: HxWx3 uint8 RGB numpy array (or HxW grey). quad: 4 (x, y) points in img pixels, in reading order
    (top-left, top-right, bottom-right, bottom-left of the text as read). angle 0 = upright pass, 90 = rotated pass.
    RapidOCR (PP-OCRv4 ONNX, CPU, 2 intra-op threads, no spinning). Images above TILE px on a side are read in
    overlapping tiles; each tile owns the core half of its overlaps, so a word is kept once, by the tile that owns its
    centre. The rotated pass reads the image turned 90 degrees (and RapidOCR's angle classifier flips 180), and keeps
    only words that are vertical in the original and do not overlap an upright word.
find_hits(words, mapping, source, extra_values=()) -> [dict(word_index, old, new, quad, why, span, angle)]
    why: "mapping" (mapping.sub_text knows it) | "extra" (exact substring of an extra value, 4+ alphanumerics, glued
    words included) | "label" (value after a person label on the same line, or the adjacent word right of / below a
    label that stands alone). NAME / COMPANY-type labels count only as "NAME: value" on one line. Values that are
    dates, numbers, scales, sizes, revision letters, single letters or template words (_STOP) are never taken.
    Exact matching only (fuzzy matching added no recall and 9x false hits in the vision experiments).
paint(img, quad, old, new) -> img   (in place; returns img)
    Component erase plus stand-in. Only pixels inside the quad's bounding box change.
verify_gone(img, hits) -> [old strings still read]   second OCR read of a crop around each painted box.
Cache: read_words() is content-addressed. Key = sha256(settings JSON + array shape/dtype + rotate flag + pixel bytes);
settings = engine and onnxruntime versions, TILE, OVERLAP, MIN_SCORE, schema. Entry = JSON (words, settings, shape, sec)
under $DEID_OCR_CACHE (default ~/hack-osu/private/ocr_cache)/ab/<key>.json, written atomically. Fresh results are put
through the same JSON round trip as cached ones (quads rounded to 0.01 px), so a run from the cache and a fresh run
return identical words. DEID_OCR_CACHE="" disables the cache. STATS counts hits, misses and fresh OCR seconds.
Deterministic for a given onnxruntime build and thread count; with the cache, identical across runs and machines.
"""
import os, re, json, hashlib, time
import numpy as np

TILE = 2000          # px; RapidOCR segfaulted on whole large pages with the cap lifted (vision.md 1)
OVERLAP = 200        # px; longer than one word at 200 dpi in most title blocks (my number, not measured)
THREADS = int(os.environ.get("DEID_OCR_THREADS", "2"))
MIN_SCORE = 0.5      # my number; vision.md used 0.5 for the lohi variant
HERE = os.path.dirname(os.path.abspath(__file__))
FONT = os.path.join(HERE, "fonts", "LiberationSans-Regular.ttf")

_ENG = None
CACHE_DIR = os.environ.get("DEID_OCR_CACHE", os.path.expanduser("~/hack-osu/private/ocr_cache"))
SCHEMA = 1
STATS = {"hit": 0, "miss": 0, "fresh_sec": 0.0}
_SETTINGS = None


def settings():
    global _SETTINGS
    if _SETTINGS is None:
        import importlib.metadata as md
        v = {}
        for p in ("rapidocr-onnxruntime", "onnxruntime"):
            try:
                v[p] = md.version(p)
            except Exception:
                v[p] = "?"
        _SETTINGS = dict(schema=SCHEMA, engine=v, tile=TILE, overlap=OVERLAP, min_score=MIN_SCORE, det_cls=False)
    return _SETTINGS


def cache_key(a, rotate_pass):
    h = hashlib.sha256()
    h.update(json.dumps(settings(), sort_keys=True).encode())
    h.update(("|%s|%s|%d|" % (a.shape, a.dtype, int(bool(rotate_pass)))).encode())
    h.update(np.ascontiguousarray(a).tobytes())
    return h.hexdigest()


def _cache_path(k):
    return os.path.join(CACHE_DIR, k[:2], k + ".json")


def _cache_get(k):
    if not CACHE_DIR:
        return None
    try:
        with open(_cache_path(k)) as f:
            e = json.load(f)
        return e if e.get("key") == k else None
    except Exception:
        return None


def _cache_put(k, entry):
    if not CACHE_DIR:
        return
    p = _cache_path(k)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = "%s.%d.tmp" % (p, os.getpid())
        with open(tmp, "w") as f:
            json.dump(entry, f, sort_keys=True)
        os.replace(tmp, p)
    except Exception:
        pass


def _engine():
    global _ENG
    if _ENG is None:
        from rapidocr_onnxruntime import RapidOCR
        from rapidocr_onnxruntime.utils import infer_engine as ie
        old = ie.OrtInferSession._init_sess_opts

        def patched(config):
            o = old(config); o.intra_op_num_threads = THREADS; o.inter_op_num_threads = 1
            o.add_session_config_entry("session.intra_op.allow_spinning", "0")
            return o
        ie.OrtInferSession._init_sess_opts = staticmethod(patched)
        _ENG = RapidOCR(det_intra_op_num_threads=THREADS, cls_intra_op_num_threads=THREADS,
                        rec_intra_op_num_threads=THREADS, max_side_len=TILE + 64)
    return _ENG


def _rgb(img):
    a = np.asarray(img)
    if a.ndim == 2:
        a = np.stack([a] * 3, axis=2)
    if a.shape[2] == 4:
        a = a[:, :, :3]
    if a.dtype != np.uint8:
        a = (a / (a.max() or 1) * 255).astype(np.uint8)
    return np.ascontiguousarray(a)


def _starts(n):
    if n <= TILE:
        return [0]
    xs = list(range(0, n - TILE, TILE - OVERLAP)); xs.append(n - TILE)
    return xs


def _read_plain(a):
    """Tiled read of an upright RGB array -> [(quad, text, score)] in a's pixels."""
    H, W = a.shape[:2]; eng = _engine(); out = []
    ys, xs = _starts(H), _starts(W)
    for iy, y0 in enumerate(ys):
        for ix, x0 in enumerate(xs):
            tile = a[y0:y0 + TILE, x0:x0 + TILE]
            res, _ = eng(np.ascontiguousarray(tile[:, :, ::-1]))
            # core region owned by this tile: midpoints of the overlaps with its neighbours
            cx0 = 0 if ix == 0 else (x0 + xs[ix - 1] + TILE) / 2.0
            cx1 = W if ix == len(xs) - 1 else (xs[ix + 1] + x0 + TILE) / 2.0
            cy0 = 0 if iy == 0 else (y0 + ys[iy - 1] + TILE) / 2.0
            cy1 = H if iy == len(ys) - 1 else (ys[iy + 1] + y0 + TILE) / 2.0
            for box, text, score in (res or []):
                q = [(float(p[0]) + x0, float(p[1]) + y0) for p in box]
                mx = sum(p[0] for p in q) / 4; my = sum(p[1] for p in q) / 4
                if cx0 <= mx < cx1 and cy0 <= my < cy1 and float(score) >= MIN_SCORE and text.strip():
                    out.append((q, text, float(score)))
    return out


def _bbox(q):
    xs = [p[0] for p in q]; ys = [p[1] for p in q]
    return min(xs), min(ys), max(xs), max(ys)


def _overlap(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    ar = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    return ix * iy / ar


def _keylog(what, k):
    p = os.environ.get("DEID_OCR_KEYLOG")
    if p:
        with open(p, "a") as f:
            f.write("%s\t%s\n" % (what, k))


def read_words(img, rotate_pass=True):
    a = _rgb(img)
    k = cache_key(a, rotate_pass)
    e = _cache_get(k)
    if e is not None:
        STATS["hit"] += 1
        _keylog("hit", k)
        return e["words"]
    if os.environ.get("DEID_OCR_FRESH", "0") != "1":           # V3: cache decides; a miss is "not read" (no fresh OCR,
        STATS["not_read"] = STATS.get("not_read", 0) + 1       # x86 and ARM OCR read different text): page or image
        _keylog("miss", k)                                      # goes out as it would with OCR off
        return []
    t0 = time.perf_counter()
    words = _read_words_fresh(a, rotate_pass)
    sec = time.perf_counter() - t0
    STATS["miss"] += 1; STATS["fresh_sec"] += sec
    words = [dict(text=w["text"], quad=[[round(float(x), 2), round(float(y), 2)] for x, y in w["quad"]],
                  score=round(float(w["score"]), 4), angle=int(w["angle"])) for w in words]
    words = json.loads(json.dumps(words))
    _cache_put(k, dict(key=k, settings=settings(), shape=list(a.shape), rotate_pass=bool(rotate_pass), words=words,
                       sec=round(sec, 3)))
    return words


def _read_words_fresh(a, rotate_pass):
    H, W = a.shape[:2]
    words = [dict(text=t, quad=q, score=s, angle=0) for q, t, s in _read_plain(a)]
    if rotate_pass:
        r = np.ascontiguousarray(np.rot90(a, 1))        # counter-clockwise: rotated (x', y') = (y, W - x)
        up = [_bbox(w["quad"]) for w in words]
        for q, t, s in _read_plain(r):
            qq = [(W - y, x) for x, y in q]               # back to the original frame, reading order kept
            b = _bbox(qq)
            if (b[3] - b[1]) < 1.5 * (b[2] - b[0]):        # not vertical in the original: the upright pass owns it
                continue
            if any(_overlap(b, u) > 0.3 for u in up):
                continue
            words.append(dict(text=t, quad=qq, score=s, angle=90))
    return words


# ---------------------------------------------------------------------------------------------- matching
HELV = [278,278,355,556,556,889,667,191,333,333,389,584,278,333,278,278,556,556,556,556,556,556,556,556,556,556,278,278,584,584,584,556,1015,667,667,722,722,667,611,778,722,278,500,667,556,833,722,778,667,778,722,667,611,722,667,944,667,667,611,278,278,278,469,556,333,556,556,500,556,556,278,556,556,222,222,500,222,833,556,556,556,556,333,500,278,556,500,722,500,500,500,334,260,334,584]


def _cw(c):
    o = ord(c)
    return HELV[o - 32] if 32 <= o <= 126 else 556


LABELS = ["DRAWN BY", "DRAWN", "DRN", "DRAFTED BY", "CHECKED BY", "CHECKED", "CHK", "CHKD", "APPROVED BY", "APPROVED",
          "APPD", "APPR", "DRN BY", "DWN BY", "CHK BY", "CHKD BY", "APPD BY", "DESIGNED BY", "DESIGNED", "DESIGNER", "DESIGN BY", "ENGINEER", "ENGINEERED BY", "AUTHOR",
          "AUTHORS", "CREATED BY", "MADE BY", "PREPARED BY", "REVIEWED BY", "OWNER", "COMPANY", "CUSTOMER",
          "CLIENT", "SUPPLIER", "VENDOR", "GEZ", "GEZEICHNET", "GEPR", "GEPRÜFT", "BEARB", "BEARBEITET",
          "ERSTELLT", "KONSTRUIERT", "FREIGABE", "DESSINÉ", "DESSINE", "VÉRIFIÉ", "VERIFIE", "DISEÑADO", "DIBUJADO",
          "REVISADO", "DISEGNATO", "CONTROLLATO", "PROGETTISTA"]
# Labels that name a field rather than a person: in title-block templates NAME / COMPANY are often column headers,
# so they trigger only with a colon and a value on the same line ("COMPANY: ACME"), never through the neighbour search.
# "NAME" is not a label at all (run2: "NAME: LEGO Brick", "Playmobil Figure", "Top Level Assm" were part names).
FIELD_LABELS = {"OWNER", "COMPANY", "CUSTOMER", "CLIENT", "SUPPLIER", "VENDOR"}
_LBL = sorted({l.upper() for l in LABELS}, key=len, reverse=True)
_LBL_SET = set(re.sub(r"[^A-Z0-9ÄÖÜÉÈÑ]", "", l) for l in _LBL)
# words that are never a person or company value
_NONVAL = _LBL_SET | {"DATE", "DATUM", "SCALE", "SHEET", "SIZE", "TITLE", "REV", "REVISION", "DWG", "DWGNO", "NO",
                      "MATERIAL", "FINISH", "WEIGHT", "QTY", "SIGNATURE", "SIGN", "UNLESS", "OTHERWISE", "SPECIFIED",
                      "DIMENSIONS", "TOLERANCES", "MM", "INCH", "INCHES", "PART", "NUMBER", "DESCRIPTION", "MASSE",
                      "MASSSTAB", "BLATT", "FORMAT", "ECHELLE", "FECHA", "ESCALA", "DATA", "SCALA", "FOGLIO",
                      "THIRD", "ANGLE", "PROJECTION", "BY", "OF", "AND", "THE", "TO", "FOR", "MFG", "QA", "QC",
                      "APPVD", "APPD", "APPRD", "CHKD", "CHKR", "CKD", "DWN", "DRWN", "DRN", "ENG", "ENGR", "CHECKER",
                      "DRAFTER", "DRAFTSMAN", "APPROVAL", "APPROVALS", "SIGNATURES", "INITIALS", "COMMENTS", "NOTES",
                      "UNLESSOTHERWISESPECIFIED", "TOLERANCE", "TOL", "SURFACE", "TREATMENT", "HARDNESS", "ITEM",
                      "PAGE", "NAME", "KONTROLLIERT", "GEPRUEFT", "GEPRUFT", "NORM", "BENENNUNG", "ZEICHNUNG", "HOW",
                      "WHAT", "WHY", "WHERE", "WHEN"}
_STOP = {"SEE", "NOTES", "NOTE", "CHANGED", "CHANGE", "NONE", "AS", "PER", "STANDARD", "STD", "STEEL", "STAINLESS",
         "ALUMINUM", "ALUMINIUM", "ABS", "PLA", "PETG", "NYLON", "BRASS", "COPPER", "BLACK", "WHITE", "RED", "BLUE",
         "UNLESS", "OTHERWISE", "SPECIFIED", "FIRST", "METRIC", "IMPERIAL", "NA", "N/A", "TBD", "TBC", "ANY",
         "BREAK", "SHARP", "EDGES", "DEBURR", "REMOVE", "BURRS", "ALL", "INITIAL", "RELEASE", "RELEASED", "DRAFT",
         "PRELIMINARY", "PROTOTYPE", "ASSEMBLY", "ASSY", "DRAWING", "SHEETS", "VIEW", "DETAIL", "SECTION", "TYP",
         "REF", "MAX", "MIN", "NOM", "THRU", "DEEP", "ANODIZED", "ANODISED", "PAINTED", "POWDER", "COAT", "CLEAR",
         "NATURAL", "PRINTED", "CUT", "LASER", "CNC", "WELD", "DATE", "APPROVED", "CHECKED", "DRAWN", "DESIGNED"}
_DATE = re.compile(r"^(\d{1,4}[-./]\d{1,2}([-./]\d{1,4})?|\d{1,2}[-./ ]?[A-Za-z]{3,9}[-./ ]?\d{2,4}|[A-Za-z]{3,9}[-./ ]?\d{1,2},?[-./ ]?\d{2,4}|\d{6,8})$")
_SCALE = re.compile(r"^\d+(\.\d+)?\s*[:/]\s*\d+(\.\d+)?$")
_SIZE = re.compile(r"^(A[0-5]|[A-E]|ANSI\s?[A-E]|\d+(\.\d+)?\s*(MM|CM|M|IN|KG|G|LB|%|°)?)$", re.I)


def _norm(text):
    """Upper-case alphanumerics of text and, for each, its index in text."""
    chars, idx = [], []
    for i, c in enumerate(text):
        if c.isalnum():
            chars.append(c.upper()); idx.append(i)
    return "".join(chars), idx


def _span_quad(q, text, i0, i1, pad=0.25):
    """Sub-quad of characters text[i0:i1] by Helvetica advance widths, padded by `pad` of a mean character."""
    ws = [_cw(c) for c in text]; tot = float(sum(ws)) or 1.0
    a = sum(ws[:i0]) / tot; b = sum(ws[:i1]) / tot; m = pad * (tot / max(1, len(text))) / tot
    a = max(0.0, a - m); b = min(1.0, b + m)
    p0, p1, p2, p3 = q
    L = lambda u, v, t: (u[0] + (v[0] - u[0]) * t, u[1] + (v[1] - u[1]) * t)
    return [L(p0, p1, a), L(p0, p1, b), L(p3, p2, b), L(p3, p2, a)]


def _is_value_token(tok):
    t = tok.strip(".,:;()[]")
    if not t or not re.search(r"[A-Za-zÀ-ÿ]", t):
        return False
    u = re.sub(r"[^A-Z0-9ÄÖÜÉÈÑ]", "", t.upper())
    if sum(1 for c in t if c.isalpha()) < 2 or u in _NONVAL or u in _STOP or _DATE.match(t) or _SCALE.match(t) or _SIZE.match(t):
        return False
    if any(u[:i] in _STOP | _NONVAL and u[i:] in _STOP | _NONVAL for i in range(2, len(u) - 1)):
        return False                     # glued template words: "SEENOTES"
    if re.match(r"^REV[A-Z0-9]?$|^[A-Z]\d{0,2}$", u):
        return False                     # revision letters and grid references
    return True


_PARTICLES = {"von", "van", "de", "der", "den", "da", "di", "du", "la", "le", "del", "dos", "y"}
_HEADER = {"DOCUMENT", "DOCUMENTTYPE", "STATUS", "TYPE", "QUALITY", "ASSURANCE", "MANUFACTURING", "ENGINEERING",
           "PRODUCTION", "PROJECT", "DEPARTMENT", "DEPT", "SIGNED", "RESPONSIBLE", "RIGHTS", "RESERVED", "COPYRIGHT"}


def _name_like(text):
    """Adjacent-cell values must look like a person or company name: every token capitalised, all capitals, or an
    initial, lower-case name particles only between such tokens, and no header word."""
    toks = text.split()
    if not toks or len(toks) > 4:
        return False
    for i, tk in enumerate(toks):
        core = tk.strip(".,:;()'")
        if not core:
            continue
        if core.lower() in _PARTICLES and 0 < i < len(toks) - 1:
            continue
        if re.sub(r"[^A-Z0-9]", "", core.upper()) in _HEADER:
            return False
        if not (core[0].isupper() or core[0].isdigit()):
            return False
    return True


def _value_span(text, start):
    """Longest run of name-like tokens in text[start:], as (i0, i1) in text, or None."""
    # a token glued to the label ("DRAWNBY:JOHNSMITH", OCR often drops spaces) contributes its part after `start`
    toks = [(max(m.start(), start), m.end()) for m in re.finditer(r"\S+", text) if m.end() > start]
    best = None; cur = None
    for s, e in toks:
        if _is_value_token(text[s:e]):
            cur = (cur[0], e) if cur else (s, e)
            if best is None or (cur[1] - cur[0]) > (best[1] - best[0]):
                best = cur
        elif cur:
            break                       # value ends at the first non-name token after it started
    if best:
        s, e = best
        while e > s and text[e - 1] in ".,:;": e -= 1
        return (s, e) if e > s else None
    return None


def _label_end(text):
    """End index of a leading or embedded label in text, or None. Glued 'DRAWNBYJSMITH' is allowed for '... BY'."""
    U = text.upper()
    for l in _LBL:
        lp = r"[\s.]*".join(re.escape(w) for w in l.split())
        glue = l.endswith(" BY")
        m = re.search(r"(?<![A-Z0-9ÄÖÜÉÈÑ])" + lp + (r"\.?\s*[:\-]?\s*" if glue else r"(?![A-Z0-9ÄÖÜÉÈÑ])\.?\s*[:\-]?\s*"), U)
        if m:
            return m.end(), l
    return None


def _same_row(a, b):
    ov = min(a[3], b[3]) - max(a[1], b[1])
    return ov > 0.5 * min(a[3] - a[1], b[3] - b[1])


def _extra_ok(nv):
    """Per-file values worth an exact search: not label or template words; 4+ alphanumerics (D9)."""
    return len(nv) >= 4 and nv not in _NONVAL and nv not in _STOP and nv not in _HEADER and nv not in _LBL_SET \
        and not nv.isdigit()


def _edge(a, b):
    """True if there is a word boundary between characters a and b: a non-alphanumeric, or a letter/digit change."""
    return not (a.isalnum() and b.isalnum()) or a.isdigit() != b.isdigit()


def _bounded(t, i0, i1):
    """t[i0:i1] is whole words of the OCR line (glued dates count as a boundary: 'Somoza10/28/2022')."""
    return (i0 == 0 or _edge(t[i0 - 1], t[i0])) and (i1 >= len(t) or _edge(t[i1 - 1], t[i1]))


RULES_ALL = ("mapping", "extra", "label", "fullname", "column", "url")


# --- recall rules (leg 2) --------------------------------------------------------------------------------------
# (a) fullname: a known identity token inside a name field (after a person label or NAME on its line, or in the cell
#     right of / below such a label) is extended to the run of capitalised name tokens around it; each token gets its
#     own stand-in. NAME counts here (a part name rarely holds a known person token) but not in the label rule.
# (b) column: values under a Name / By column header of an approval table (row starts with a role word) or of a
#     revision table (header row has REV and DESCRIPTION; then initials only).
# (c) url / copyright: URL path or host segments and the 1-4 tokens after "copyright" / (c) that the table knows
#     (mapping.is_identity; German ue/oe/ae spellings folded: "Bluemchen AG" = "Blumchen AG").
_NF_WORDS = sorted(set(_LBL) | {"NAME", "NAMEN", "NOM", "NOMBRE"}, key=len, reverse=True)
_NF_SET = set(re.sub(r"[^A-Z0-9ÄÖÜÉÈÑ]", "", l) for l in _NF_WORDS)
_COLHDR = {"NAME", "NAMEN", "NOM", "NOMBRE", "BY", "INITIALS", "INIT"}
_ROLE = {"DRAWN", "DRN", "DWN", "DRWN", "DRAFTED", "CHECKED", "CHK", "CHKD", "CKD", "APPROVED", "APPD", "APPR", "APPVD",
         "DESIGNED", "DESIGN", "ENG", "ENGR", "ENGINEER", "GEZEICHNET", "GEZ", "KONTROLLIERT", "GEPRUFT", "GEPRÜFT",
         "GEPR", "BEARB", "BEARBEITET", "NORM", "FREIGABE", "ERSTELLT", "KONSTRUIERT", "DESSINE", "DESSINÉ", "VERIFIE",
         "VÉRIFIÉ", "MFG", "QA", "QC", "AUTHOR", "CREATED", "PREPARED", "REVIEWED", "RELEASED"}
_REVCTX = {"REV", "REVISION", "DESCRIPTION", "ÄNDERUNG", "AENDERUNG", "ZONE", "ECO", "ECN"}
_INITIALS = re.compile(r"^[A-Z]{2,4}([./][A-Z]{1,4}){0,2}\.?$")
_URLCTX = re.compile(r"https?:|www\.|github|gitlab|\.(com|org|net|edu|ch|de|io)\b", re.I)
_COPY = re.compile(r"(copyright|©|\(c\))\s*", re.I)


def _fold(v):
    v = v.replace("ü", "u").replace("ö", "o").replace("ä", "a").replace("Ü", "U").replace("Ö", "O").replace("Ä", "A")
    return re.sub(r"(?<=[A-Za-z])([uoaUOA])[eE]", r"\1", v)


def _known(mapping, v, source):
    try:
        return mapping.is_identity(v, source) or (_fold(v) != v and mapping.is_identity(_fold(v), source))
    except Exception:
        return False


def _tok_spans(t):
    return [(m.start(), m.end()) for m in re.finditer(r"\S+", t)]


def _core(t, s, e):
    while s < e and t[s] in ".,:;()'\"[]|":
        s += 1
    while e > s and t[e - 1] in ".,:;()'\"[]|":
        e -= 1
    return s, e


def _namey(tok):
    return bool(tok) and (tok[0].isupper() or tok.lower() in _PARTICLES) and _is_value_token(tok) \
        and re.sub(r"[^A-Z0-9ÄÖÜÉÈÑ]", "", tok.upper()) not in _NF_SET | _HEADER


def _name_run(t, i, j):
    """Token spans of the run of name-like tokens containing t[i:j] (whole token), at most 4; None if not a run."""
    sp = [_core(t, s, e) for s, e in _tok_spans(t)]
    k = next((n for n, (s, e) in enumerate(sp) if s <= i < e), None)
    if k is None or sp[k] != (i, j):
        return None
    lo = hi = k
    while lo > 0 and hi - lo < 3 and _namey(t[sp[lo - 1][0]:sp[lo - 1][1]]):
        lo -= 1
    while hi < len(sp) - 1 and hi - lo < 3 and _namey(t[sp[hi + 1][0]:sp[hi + 1][1]]):
        hi += 1
    while lo < k and t[sp[lo][0]:sp[lo][1]].lower() in _PARTICLES:
        lo += 1
    while hi > k and t[sp[hi][0]:sp[hi][1]].lower() in _PARTICLES:
        hi -= 1
    return sp[lo:hi + 1] if hi > lo else None


def _nf_label_before(t, i):
    U = t[:i].upper()
    for l in _NF_WORDS:
        lp = r"[\s.]*".join(re.escape(w) for w in l.split())
        if re.search(r"(?<![A-Z0-9ÄÖÜÉÈÑ])" + lp + r"(?![A-Z0-9ÄÖÜÉÈÑ])", U):
            return True
    return False


def find_hits(words, mapping, source, extra_values=(), rules=RULES_ALL):
    hits = []; taken = {}
    boxes = [_bbox(w["quad"]) for w in words]

    def add(wi, i0, i1, new, why):
        w = words[wi]; old = w["text"][i0:i1]
        if not old.strip() or new == old:
            return False
        for (a0, a1) in taken.get(wi, []):
            if i0 < a1 and a0 < i1:
                return False
        taken.setdefault(wi, []).append((i0, i1))
        hits.append(dict(word_index=wi, old=old, new=new, quad=_span_quad(w["quad"], w["text"], i0, i1), why=why,
                         span=(i0, i1), angle=w.get("angle", 0)))
        return True

    def nf_cell(wi):
        """The word is the cell right of / below a standalone name-field label."""
        b = boxes[wi]; h = max(1.0, b[3] - b[1])
        for wk, bk in enumerate(boxes):
            if wk == wi or words[wk].get("angle", 0) != words[wi].get("angle", 0):
                continue
            if _norm(words[wk]["text"])[0] not in _NF_SET:
                continue
            if _same_row(b, bk) and -0.8 * h <= b[0] - bk[2] < 12 * h:
                return True
            if min(b[2], bk[2]) - max(b[0], bk[0]) > 0.3 * min(b[2] - b[0], bk[2] - bk[0]) and 0 <= b[1] - bk[3] < 2.5 * h:
                return True
        return False

    ex = []
    for v in (extra_values if "extra" in rules else ()):
        nv, _ = _norm(v)
        if _extra_ok(nv):
            ex.append(nv)
    ex = sorted(set(ex), key=len, reverse=True)
    for wi, w in enumerate(words):
        t = w["text"]
        # 1. what the mapping knows (same length, character-wise: a char diff gives the spans)
        nt, n = t, 0
        if "mapping" in rules:
            try:
                nt, n = mapping.sub_text(t, source)
            except Exception:
                nt, n = t, 0
        if n and len(nt) == len(t):
            i = 0
            while i < len(t):
                if nt[i] != t[i]:
                    j = i
                    while j < len(t) and (nt[j] != t[j] or (not t[j].isalnum() and j + 1 < len(t) and nt[j + 1] != t[j + 1])):
                        j += 1
                    if _bounded(t, i, j):          # OCR text: names only as whole words ('rint' in 'Blueprint' is not)
                        run = _name_run(t, i, j) if "fullname" in rules else None
                        if run and (_nf_label_before(t, run[0][0]) or nf_cell(wi)):
                            for s0, e0 in run:
                                add(wi, s0, e0, mapping.sub_field(t[s0:e0], source), "fullname")
                        else:
                            add(wi, i, j, nt[i:j], "mapping")
                    i = j
                else:
                    i += 1
        # 2. per-source list, exact substring of the normalised line (catches glued words)
        if ex:
            ns, idx = _norm(t)
            for v in ex:
                p = ns.find(v)
                while p >= 0:
                    i0, i1 = idx[p], idx[p + len(v) - 1] + 1
                    if len(v) >= 6 or _bounded(t, i0, i1):      # D9: 4-5 characters need word boundaries
                        add(wi, i0, i1, mapping.sub_field(t[i0:i1], source), "extra")
                    p = ns.find(v, p + len(v))
        # 3. label rule: the value after a person label in the same OCR line, else the ADJACENT word right or below
        if "url" in rules and _URLCTX.search(t):
            for m in re.finditer(r"[A-Za-z0-9][A-Za-z0-9_\-]{3,}", t):
                if m.group().lower() not in ("http", "https", "github", "gitlab", "www") and _known(mapping, m.group(), source):
                    add(wi, m.start(), m.end(), mapping.sub_field(m.group(), source), "url")
        mc = _COPY.search(t) if "url" in rules else None
        if mc:
            sp = [_core(t, s0, e0) for s0, e0 in _tok_spans(t) if s0 >= mc.end()][:6]
            done = False
            for n in (4, 3, 2, 1):
                for k in range(0, len(sp) - n + 1):
                    s0, e0 = sp[k][0], sp[k + n - 1][1]
                    v = t[s0:e0]
                    if len(_norm(v)[0]) >= 3 and not v.isdigit() and _known(mapping, v, source):
                        done = add(wi, s0, e0, mapping.sub_field(v, source), "copyright") or done
                if done:
                    break
        lab = _label_end(t) if "label" in rules else None
        if lab is None:
            continue
        le, lname = lab
        field = lname in FIELD_LABELS
        sp = _value_span(t, le)
        if sp:
            v = t[sp[0]:sp[1]]
            login = ((not field) and (":" in t[:sp[0]] or lname.endswith(" BY")) and len(v.split()) == 1
                     and re.match(r"^[a-z][a-z0-9._-]{2,}$", v) and v.lower() not in _PARTICLES)
            if (not field or ":" in t[:sp[0]]) and (login or _name_like(v)):
                add(wi, sp[0], sp[1], mapping.sub_field(v, source), "label")
            continue
        if field or le < len(t.rstrip()) - 1:
            continue
        lb = _bbox(w["quad"]); h = max(1.0, lb[3] - lb[1]); right = below = None
        for wj, w2 in enumerate(words):
            if wj == wi or w2["angle"] != w["angle"]:
                continue
            b = _bbox(w2["quad"])
            if _same_row(lb, b) and -0.8 * h <= b[0] - lb[2] < 12 * h:
                if right is None or b[0] < right[0]:
                    right = (b[0], wj)
            elif min(lb[2], b[2]) - max(lb[0], b[0]) > 0.3 * min(lb[2] - lb[0], b[2] - b[0]) and 0 <= b[1] - lb[3] < 2.5 * h:
                if below is None or b[1] < below[0]:
                    below = (b[1], wj)
        gaps = []                             # nearest cell first: a value under the label beats a title to its right
        if right is not None:                 # (run2: "DESIGNED BY" took "Greenhouse - Window" and missed the name below)
            gaps.append((right[0] - lb[2], right))
        if below is not None:
            gaps.append((below[0] - lb[3], below))
        for _, cand in sorted(gaps, key=lambda g: g[0]):   # only the adjacent word: never jump over a header
            if cand is None:
                continue
            t2 = words[cand[1]]["text"]
            if _label_end(t2) is not None:
                continue
            cb = _bbox(words[cand[1]]["quad"])
            if (cb[3] - cb[1]) > 2.2 * h:       # a title in a big font, not a value cell (run10: "Top Level Assm")
                continue
            sp = _value_span(t2, 0)
            lead = len(t2) - len(t2.lstrip(" .,:;()'\""))
            if sp and sp[0] > lead and re.fullmatch(r"(?:[A-Z]\.\s*){1,3}", t2[lead:sp[0]]):
                sp = (lead, sp[1])               # leading initials belong to the name: "P. COLBACH"
            if sp and sp[0] == lead and _name_like(t2[sp[0]:sp[1]]):
                # the value must START the cell ("How to CAD Almost Anything!", "2x4 LEGO Brick" are titles)
                add(cand[1], sp[0], sp[1], mapping.sub_field(t2[sp[0]:sp[1]], source), "label")
                break
    if "column" in rules:
        _columns(words, boxes, mapping, source, add)
    return hits


def _columns(words, boxes, mapping, source, add):
    for hi, hw in enumerate(words):
        if _norm(hw["text"])[0] not in _COLHDR:
            continue
        hb = boxes[hi]; h = max(1.0, hb[3] - hb[1])
        row = [_norm(words[k]["text"])[0] for k, b in enumerate(boxes) if k != hi and _same_row(hb, b)]
        rev = sum(1 for r in row if r in _REVCTX) >= 1 and any(r in ("DESCRIPTION", "ÄNDERUNG", "AENDERUNG", "DATE", "DATUM") for r in row)
        cells = [k for k, b in enumerate(boxes) if k != hi and words[k].get("angle", 0) == hw.get("angle", 0)
                 and 0 <= b[1] - hb[3] < 10 * h
                 and hb[0] - 0.6 * (hb[2] - hb[0]) <= (b[0] + b[2]) / 2 <= hb[2] + 0.6 * (hb[2] - hb[0])]
        for k in cells:
            b = boxes[k]; t2 = words[k]["text"]
            role = any(_norm(words[r]["text"])[0] in _ROLE or _label_end(words[r]["text"]) is not None
                       for r, br in enumerate(boxes) if r != k and _same_row(b, br) and br[2] <= b[0] + 0.3 * h)
            s0, e0 = _core(t2, 0, len(t2))
            v = t2[s0:e0]
            if not v or _label_end(v) is not None:
                continue
            if role and _value_span(t2, 0) and _name_like(v) and all(_is_value_token(x) or x.lower() in _PARTICLES for x in v.split()):
                add(k, s0, e0, mapping.sub_field(v, source), "column")
            elif (role or rev) and _INITIALS.match(v) and _is_value_token(v):
                add(k, s0, e0, mapping.sub_field(v, source), "column")


# ---------------------------------------------------------------------------------------------- drawing gate
KW = {'DRAWN', 'CHECKED', 'APPROVED', 'SCALE', 'SHEET', 'TITLE', 'DWG', 'REV', 'REVISION', 'MATERIAL', 'SIZE', 'WEIGHT',
      'DATE', 'FINISH', 'TOLERANCES', 'TOLERANCE', 'MASSSTAB', 'BLATT', 'WERKSTOFF', 'DATUM', 'GEZ', 'GEPR', 'NAME',
      'ECHELLE', 'FEUILLE', 'SCALA', 'FOGLIO', 'ESCALA', 'HOJA', 'DESIGNED', 'PROJECTION', 'QTY', 'PART', 'DRAWING',
      'UNLESS', 'DIMENSIONS', 'APPROVAL', 'ENGINEER', 'BENENNUNG', 'ZEICHNUNG', 'DRWN', 'CHK', 'APPD', 'DESCRIPTION'}
GATE_MIN = 3


def keywords(words):
    found = set()
    for w in words:
        for t in re.split(r'[^A-Za-z]+', w['text'].upper()):
            if t in KW:
                found.add(t)
    return found


def is_drawing(words):
    return len(keywords(words)) >= GATE_MIN


def verify_gone(img, hits, margin=1.0):
    """Second read after painting: OCR a crop around each hit box (grown by `margin` x the box's short side, at least
    8 px). -> list of old strings (normalised, 2+ characters) still found in the crop's text. Conservative: words are
    joined, so a match across two words also counts as 'still there'."""
    a = _rgb(img); H, W = a.shape[:2]; left = []
    for h in hits:
        x0, y0, x1, y1 = _bbox(h["quad"]); m = max(8.0, margin * min(x1 - x0, y1 - y0))
        cx0, cy0, cx1, cy1 = int(max(0, x0 - m)), int(max(0, y0 - m)), int(min(W, x1 + m)), int(min(H, y1 + m))
        if cx1 - cx0 < 4 or cy1 - cy0 < 4:
            continue
        crop = np.ascontiguousarray(a[cy0:cy1, cx0:cx1])
        ws = read_words(crop, rotate_pass=True)
        joined = "".join(_norm(w["text"])[0] for w in ws)
        old = _norm(h["old"])[0]
        if len(old) >= 2 and old in joined:
            left.append(h["old"])
    return left


# ---------------------------------------------------------------------------------------------- painting
def _rot_k(q):
    dx = q[1][0] - q[0][0]; dy = q[1][1] - q[0][1]
    if abs(dx) >= abs(dy):
        return 0 if dx >= 0 else 2
    return 1 if dy > 0 else 3


def _rot_pt(x, y, k, H, W):
    for _ in range(k):                     # one counter-clockwise turn of an HxW array: (x, y) -> (y, W - x)
        x, y = y, W - x; H, W = W, H
    return x, y


def _paint_axis(img, box, old, new):
    """img: HxWx3 uint8 view (may be a rotated view). Writes only inside box. Returns info or None."""
    import cv2
    from PIL import Image, ImageDraw, ImageFont
    H, Wd = img.shape[:2]; x0, y0, x1, y1 = [int(round(v)) for v in box]
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(Wd, x1), min(H, y1)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    crop = img[y0:y1, x0:x1].astype(int)
    ring = np.concatenate([img[max(0, y0 - 3):y0, x0:x1].reshape(-1, 3), img[y1:y1 + 3, x0:x1].reshape(-1, 3),
                           img[y0:y1, max(0, x0 - 3):x0].reshape(-1, 3), img[y0:y1, x1:x1 + 3].reshape(-1, 3)])
    bg = np.median(ring, axis=0) if len(ring) else np.array([255, 255, 255])
    diff = np.abs(crop - bg).sum(axis=2); ink = diff > 90
    if not ink.any():
        return None
    n, lab = cv2.connectedComponents(ink.astype(np.uint8), connectivity=8)
    edge = set(lab[0]) | set(lab[-1]) | set(lab[:, 0]) | set(lab[:, -1]); edge.discard(0)
    em = np.isin(lab, list(edge)) if edge else np.zeros_like(ink)
    txt = ink & ~em
    if txt.sum() < 0.3 * ink.sum():
        txt = ink; em = np.zeros_like(ink)          # text itself touches the edge: erase all ink in the box
    if not txt.any():
        return None
    ys, xs = np.where(txt); tx0, tx1, ty0, ty1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    strong = crop[txt & (diff > 0.6 * diff[txt].max())]; col = tuple(int(v) for v in np.median(strong, axis=0))
    er = cv2.dilate(txt.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool) & ~em
    out = crop.copy(); out[er] = bg
    th = int(ty1 - ty0); tw = int(tx1 - tx0); size = 6
    while size < 400:
        f = ImageFont.truetype(FONT, size + 1); bb = f.getbbox(old)
        if bb[3] - bb[1] > th:
            break
        size += 1
    f = ImageFont.truetype(FONT, size); bb = f.getbbox(new)
    layer = Image.new("L", (bb[2] - bb[0] + 2, bb[3] - bb[1] + 2), 0)
    ImageDraw.Draw(layer).text((1 - bb[0], 1 - bb[1]), new, font=f, fill=255)
    if layer.size[0] > tw:
        layer = layer.resize((max(1, tw), layer.size[1]), Image.LANCZOS)
    pil = Image.fromarray(out.astype(np.uint8))
    pil.paste(Image.new("RGB", layer.size, col), (int(tx0), int(ty0 + (th - layer.size[1]) // 2)), layer)
    img[y0:y1, x0:x1] = np.array(pil)
    return dict(box=[x0, y0, x1, y1], erased_px=int(er.sum()), font_px=size)


def paint(img, quad, old, new):
    """Erase `old` inside quad and draw `new`. img: HxWx3 uint8 array, edited in place and returned.
    Vertical or upside-down text is painted through a rotated view, so the stand-in follows the reading direction."""
    k = _rot_k(quad); H, W = img.shape[:2]
    v = np.rot90(img, k) if k else img
    q = [_rot_pt(x, y, k, H, W) for x, y in quad]
    _paint_axis(v, _bbox(q), old, new)
    return img


def box_of(quad, shape):
    """Integer pixel box (x0, y0, x1, y1) that paint() may change, clipped to the image."""
    H, W = shape[:2]; b = _bbox(quad)
    return max(0, int(round(b[0]))), max(0, int(round(b[1]))), min(W, int(round(b[2]))), min(H, int(round(b[3])))
