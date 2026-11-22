"""Logo table: recognise image XObjects by a pixel key and replace the ones labelled "logo".

Pure and deterministic. Depends on pikepdf (MPL-2.0) and Pillow (MIT-CMU) only.

    image_key(pdf, xobj) -> str | None   sha1 of decoded base pixels + size + bpc + colour space + decoded mask pixels
    load_table(path=None) -> LogoTable    dict key -> "logo" | "keep" | "unknown"  (data/logo_hashes.tsv)
    replace_logos(pdf, table=None) -> (count, notes)

The key decodes pixels, so it does not change when a JPEG loses its APP/COM metadata segments or a Flate stream is
recompressed. It covers the soft mask (/SMask) and an explicit /Mask, because many logos keep their shape only in the
mask (the "black tiles" of the first census).
"""
import hashlib, io, os, zlib

import pikepdf
from pikepdf import Array, Name

PLACEHOLDER_GREY = 235            # flat light grey, DeviceGray 8 bit
MAX_PIXELS = 40_000_000           # larger images are never logos; no key (my number, not measured)
_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "logo_hashes.tsv")


class LogoTable(dict):
    """key -> label, plus .sizes {(w, h)} of the logo rows, used to skip decoding images that cannot match."""
    sizes = frozenset()


def _filters(o):
    f = o.get("/Filter")
    if f is None:
        return []
    return [str(x) for x in f] if isinstance(f, Array) else [str(f)]


def _cs_sig(cs, depth=0):
    if cs is None:
        return "none"
    if depth > 4:
        return "deep"
    if isinstance(cs, Name):
        return str(cs)
    if isinstance(cs, Array) and len(cs):
        head = str(cs[0])
        if head == "/Indexed" and len(cs) >= 4:
            lut = cs[3]
            lb = lut.read_bytes() if isinstance(lut, pikepdf.Stream) else bytes(lut)
            return "/Indexed(%s,%d,%s)" % (_cs_sig(cs[1], depth + 1), int(cs[2]), hashlib.sha1(lb).hexdigest())
        if head == "/ICCBased":
            try:
                return "/ICCBased%d" % int(cs[1].get("/N", 0))
            except Exception:
                return "/ICCBased"
        if head in ("/Separation", "/DeviceN"):
            return head + "(%s)" % hashlib.sha1(repr(cs[1]).encode()).hexdigest()[:12]
        return head
    return type(cs).__name__


def _pixels(o):
    """Decoded sample bytes of an image stream. JPEG and JPEG 2000 go through Pillow; JBIG2 and CCITT stay raw."""
    fl = _filters(o)
    last = fl[-1] if fl else None
    if last in ("/DCTDecode", "/JPXDecode"):
        raw = o.read_raw_bytes()
        for f in fl[:-1]:                      # e.g. [/FlateDecode /DCTDecode]: qpdf will not stop half way
            if f != "/FlateDecode":
                raise ValueError("filter chain")
            raw = zlib.decompressobj().decompress(raw)
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        im.load()
        return im.mode.encode() + b"|" + im.tobytes()
    if last in ("/JBIG2Decode", "/CCITTFaxDecode"):
        parts = [o.read_raw_bytes(), repr(o.get("/DecodeParms")).encode()]
        try:
            g = o.get("/DecodeParms")
            if g is not None and "/JBIG2Globals" in g:
                parts.append(g.JBIG2Globals.read_bytes())
        except Exception:
            pass
        return b"raw|" + b"|".join(parts)
    return o.read_bytes()


def _sig(o):
    w = int(o.get("/Width", 0)); h = int(o.get("/Height", 0))
    if w <= 0 or h <= 0 or w * h > MAX_PIXELS:
        raise ValueError("size")
    bpc = int(o.get("/BitsPerComponent", 0)) if "/BitsPerComponent" in o else 0
    dec = repr([float(x) for x in o.Decode]) if "/Decode" in o else ""
    head = b"%d|%d|%d|%s|%s|" % (w, h, bpc, _cs_sig(o.get("/ColorSpace")).encode(), dec.encode())
    return head + _pixels(o)


def image_key(pdf, xobj):
    """sha1 hex of decoded base pixels (+ width, height, bpc, colour space, /Decode) and of the decoded mask, or None."""
    try:
        if bool(xobj.get("/ImageMask", False)):
            return None
        h = hashlib.sha1(_sig(xobj))
        sm = xobj.get("/SMask"); mk = xobj.get("/Mask")
        if isinstance(sm, pikepdf.Stream):
            h.update(b"|smask|" + hashlib.sha1(_sig(sm)).digest())
        elif isinstance(mk, pikepdf.Stream):
            mw = int(mk.get("/Width", 0)); mh = int(mk.get("/Height", 0))
            h.update(b"|mask|%d|%d|" % (mw, mh) + hashlib.sha1(_pixels(mk)).digest())
        elif isinstance(mk, Array):
            h.update(b"|ckey|" + repr([int(x) for x in mk]).encode())
        else:
            h.update(b"|nomask")
        return h.hexdigest()
    except Exception:
        return None


def load_table(path=None):
    t = LogoTable(); sizes = set()
    p = path or _DEFAULT
    if not os.path.exists(p):
        return t
    for line in open(p, encoding="utf-8"):
        if not line.strip() or line.startswith("#"):
            continue
        a = line.rstrip("\n").split("\t")
        if len(a) < 2:
            continue
        t[a[0]] = a[1]
        if a[1] == "logo" and len(a) > 3 and "x" in a[3]:
            try:
                w, hh = a[3].split("x"); sizes.add((int(w), int(hh)))
            except ValueError:
                pass
    t.sizes = frozenset(sizes)
    return t


_CACHE = {}


def _default_table():
    if "t" not in _CACHE:
        _CACHE["t"] = load_table()
    return _CACHE["t"]


_KEY = None


def _key():
    global _KEY
    if _KEY is None:
        p = os.environ.get("DEID_PSEUDO_KEY") or os.path.join(os.environ.get("DEID_IDENTITIES_DIR") or os.path.expanduser("~/hack-osu/private"), "pseudo.key")
        try:
            _KEY = open(p, "rb").read().strip()
        except Exception:
            _KEY = b"dev-key-change-before-the-real-run"
    return _KEY


def placeholder_code(key):
    """Council D8: short code from the private key and the image key; same logo -> same code, different logos differ."""
    import hmac
    return "LOGO-" + hmac.new(_key(), b"logo:" + (key or "").encode(), hashlib.sha256).hexdigest()[:6].upper()


def _placeholder_pixels(w, h, code):
    """Flat grey with the code in dark grey, fitted to the box (Pillow default font; deterministic per Pillow version)."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("L", (w, h), PLACEHOLDER_GREY)
    if w >= 24 and h >= 8:
        try:
            f = ImageFont.load_default()
            l, t, r, b = ImageDraw.Draw(im).textbbox((0, 0), code, font=f)
            tw, th = max(1, r - l), max(1, b - t)
            tile = Image.new("L", (tw + 4, th + 4), PLACEHOLDER_GREY)
            ImageDraw.Draw(tile).text((2 - l, 2 - t), code, fill=90, font=f)
            sc = min(0.9 * w / tile.width, 0.6 * h / tile.height)
            nw, nh = max(1, int(tile.width * sc)), max(1, int(tile.height * sc))
            tile = tile.resize((nw, nh), Image.NEAREST)
            im.paste(tile, ((w - nw) // 2, (h - nh) // 2))
        except Exception:
            pass
    return im.tobytes()


def _placeholder(o, w, h, key=None):
    for k in ("/SMask", "/Mask", "/Decode", "/DecodeParms", "/Intent", "/Interpolate", "/Alternates", "/Metadata",
              "/SMaskInData", "/Matte", "/OPI", "/Name", "/StructParent", "/ID"):
        if k in o:
            del o[k]
    o.write(zlib.compress(_placeholder_pixels(w, h, placeholder_code(key)), 9), filter=Name.FlateDecode)
    o.ColorSpace = Name.DeviceGray; o.BitsPerComponent = 8; o.Width = w; o.Height = h


def replace_logos(pdf, table=None):
    """Replace every image XObject whose key is labelled "logo" by a flat grey image of the same pixel size.
    The stream object is rewritten in place, so every page, form and pattern that shares it gets the placeholder.
    Images labelled keep or unknown, stencil masks and images used only as masks are never touched."""
    t = _default_table() if table is None else table
    logos = {k for k, v in t.items() if v == "logo"}
    if not logos:
        return 0, []
    sizes = getattr(t, "sizes", None) or None
    masks = set()
    for o in pdf.objects:
        if isinstance(o, pikepdf.Stream) and o.get("/Subtype") == "/Image":
            for k in ("/SMask", "/Mask"):
                m = o.get(k)
                if isinstance(m, pikepdf.Stream):
                    masks.add(m.objgen)
    n = 0
    for o in list(pdf.objects):
        try:
            if not isinstance(o, pikepdf.Stream) or o.get("/Subtype") != "/Image" or o.objgen in masks:
                continue
            if bool(o.get("/ImageMask", False)):
                continue
            w = int(o.get("/Width", 0)); h = int(o.get("/Height", 0))
            if sizes is not None and (w, h) not in sizes:
                continue
            k = image_key(pdf, o)
            if k in logos:
                _placeholder(o, w, h, k); n += 1
        except Exception:
            continue
    return n, (["logos replaced: %d" % n] if n else [])


def _mul(a, b):
    return [a[0]*b[0]+a[1]*b[2], a[0]*b[1]+a[1]*b[3], a[2]*b[0]+a[3]*b[2], a[2]*b[1]+a[3]*b[3],
            a[4]*b[0]+a[5]*b[2]+b[4], a[4]*b[1]+a[5]*b[3]+b[5]]


def image_placements(page, max_depth=4):
    """[(image stream, (x0, y0, x1, y1) in default user space)] for every image XObject drawn on the page,
    including images inside form XObjects. Inline images are not covered."""
    out = []

    def walk(holder, res, ctm0, depth, seen):
        try:
            instrs = pikepdf.parse_content_stream(holder)
        except Exception:
            return
        ctm = list(ctm0); stack = []
        xo = res.get("/XObject") if res is not None else None
        for ops, op in instrs:
            o = str(op)
            if o == "q":
                stack.append(list(ctm))
            elif o == "Q":
                if stack:
                    ctm = stack.pop()
            elif o == "cm" and len(ops) == 6:
                ctm = _mul([float(x) for x in ops], ctm)
            elif o == "Do" and xo is not None:
                x = xo.get(str(ops[0]))
                if not isinstance(x, pikepdf.Stream):
                    continue
                st = x.get("/Subtype")
                if st == "/Image":
                    pts = [(ctm[0]*u + ctm[2]*v + ctm[4], ctm[1]*u + ctm[3]*v + ctm[5]) for u in (0, 1) for v in (0, 1)]
                    out.append((x, (min(p[0] for p in pts), min(p[1] for p in pts),
                                    max(p[0] for p in pts), max(p[1] for p in pts))))
                elif st == "/Form" and depth < max_depth and x.objgen not in seen:
                    m = [float(v) for v in x.get("/Matrix", [1, 0, 0, 1, 0, 0])]
                    r2 = x.get("/Resources")
                    walk(x, r2 if r2 is not None else res, _mul(m, ctm), depth + 1, seen | {x.objgen})

    res = page.obj.get("/Resources")
    p = page.obj
    while res is None and p.get("/Parent") is not None:
        p = p.Parent; res = p.get("/Resources")
    walk(page, res, [1, 0, 0, 1, 0, 0], 0, frozenset())
    return out


def replace_titleblock_singletons(pdf, page_index, box_pts, table=None, inside=0.5):
    """D8 rule: replace every image on the page whose placement lies at least `inside` of its area within box_pts
    (x0, y0, x1, y1, default user space, same frame as the page's MediaBox) unless the table labels it "keep".
    Measured on 61 hits from 47 drawing pages: 36 logos, 5 QR codes, 2 vendor marks, 10 drawing figures, 6 blank
    strips, 2 table images. So it also greys out some drawing figures that sit in the title block."""
    t = _default_table() if table is None else table
    bx0, by0, bx1, by1 = box_pts; n = 0
    try:
        page = pdf.pages[page_index]
    except Exception:
        return 0, ["no page %d" % page_index]
    for x, (x0, y0, x1, y1) in image_placements(page):
        try:
            if bool(x.get("/ImageMask", False)):
                continue
            a = max(1e-6, (x1 - x0) * (y1 - y0))
            ov = max(0.0, min(bx1, x1) - max(bx0, x0)) * max(0.0, min(by1, y1) - max(by0, y0))
            if ov / a < inside:
                continue
            kx = image_key(pdf, x)
            if t.get(kx) == "keep":
                continue
            w = int(x.get("/Width", 0)); h = int(x.get("/Height", 0))
            if w > 0 and h > 0:
                _placeholder(x, w, h, kx); n += 1
        except Exception:
            continue
    return n, (["title-block images replaced: %d" % n] if n else [])
