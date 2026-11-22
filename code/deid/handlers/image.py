"""Raster images: metadata stripped without re-encoding; identity text found by OCR and painted over. PIXELS builder.

Order: (1) true type by magic bytes; (2) metadata removed without touching coded pixels (deid.vision.imgmeta: JPEG
segments, PNG chunks, TIFF tags zeroed in place, GIF comment/application extensions); (3) OCR (cached, deid.vision.ocr);
(4) drawing gate (3+ title-block keywords, ocr.is_drawing): on drawings the rules in DEID_OCR_RULES apply (default
all six, see ocr.py), on everything else only DEID_OCR_RULES_OTHER (default mapping,url: names already in the table);
(5) only if a hit is found: paint, re-encode, second OCR read of every box. If an old string is still read, the
metadata-stripped original goes out with status partial and a note.
Painting keeps every pixel outside the changed ones exactly for lossless formats (PNG, GIF, TIFF, BMP): only changed
pixels are written back into the image's own mode (palette index, grey, 1-bit, CMYK, 16-bit through OpenCV). JPEG is
re-encoded with the original quantisation tables and subsampling; two encodes are tried (RGB, and YCbCr straight from
the decoder without colour conversion) and the one with the smaller change outside the boxes is kept.
Painted outputs carry no thumbnails: JPEG APP1/APP13 are dropped by the re-strip; TIFF/PNG/GIF are written fresh.
Not painted (status partial, listed): multi-frame images (animated GIF/PNG, multi-page TIFF) with a hit.
Limits I set (my numbers): OCR skipped below 32 px on a side; images above OCR_PIXELS (25 MP, e.g. 600 dpi scans of
large sheets) are read from a Lanczos-reduced copy (still >= 200 dpi for an ANSI D sheet) and the boxes scaled back to
full size for painting and the second read; above MAX_PIXELS (250 MP) the image is not read (status partial if so).
"""
import io, os
import numpy as np
from deid.api import register, Result
from deid.vision import imgmeta

MIN_SIDE = 32
MAX_PIXELS = 250_000_000
OCR_PIXELS = 25_000_000
EXTS = (".png", ".jpg", ".jpeg", ".jpe", ".tif", ".tiff", ".bmp", ".gif")
RULES_DRAWING = tuple(r for r in os.environ.get("DEID_OCR_RULES", "mapping,extra,label,fullname,column,url").split(",") if r)
RULES_OTHER = tuple(r for r in os.environ.get("DEID_OCR_RULES_OTHER", "mapping,url").split(",") if r)


def _kind(head):
    if head[:2] == b"\xff\xd8":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:2] == b"BM":
        return "bmp"
    return None


def _orientation(stripped_notes):
    for r in stripped_notes:
        if r.startswith("kept:Orientation="):
            return int(r.split("=")[1])
    return None


def _deep(im, data, kind):
    """16-bit (or float) image -> (native array via OpenCV, max value) or None for 8-bit images."""
    bits = 8
    if kind == "png" and len(data) > 24:
        bits = data[24]
    elif kind == "tiff":
        try:
            b = im.tag_v2.get(258)
            bits = max(b) if isinstance(b, (tuple, list)) else int(b or 8)
        except Exception:
            pass
    if im.mode in ("I", "F") or im.mode.startswith("I;16"):
        bits = max(bits, 16)
    if bits <= 8:
        return None
    import cv2
    a = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if a is None or a.dtype not in (np.uint16, np.uint8):
        return None
    return a, int(a.max()) or 1


def decode(data, kind):
    """-> (PIL image of frame 0, RGB uint8 array as OCR sees it, deep or None)."""
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS + 1
    im = Image.open(io.BytesIO(data))
    im.load()
    dp = _deep(im, data, kind)
    if dp is not None:
        a, mx = dp
        a8 = np.round(a.astype(np.float64) * 255.0 / mx).astype(np.uint8)
        if a8.ndim == 2:
            rgb = np.stack([a8] * 3, axis=2)
        else:
            rgb = a8[:, :, 2::-1] if a8.shape[2] >= 3 else np.stack([a8[:, :, 0]] * 3, axis=2)
        return im, np.ascontiguousarray(rgb), dp
    if im.mode == "CMYK" or im.mode not in ("RGB",):
        rgb = np.array(im.convert("RGB"))
    else:
        rgb = np.array(im)
    return im, rgb, None


def _grey(px):
    px = px.astype(np.int64)
    return ((px[:, 0] * 19595 + px[:, 1] * 38470 + px[:, 2] * 7471 + 0x8000) >> 16).astype(np.uint8)


def _native(im, out, chg, deep):
    """Write only the changed pixels back into the image's own representation. -> PIL image or (cv2 array,)."""
    from PIL import Image
    if deep is not None:
        a, mx = deep; a = a.copy()
        v = np.round(out[chg].astype(np.float64) * mx / 255.0).astype(a.dtype)
        if a.ndim == 2:
            a[chg] = np.round(_grey(out[chg]).astype(np.float64) * mx / 255.0).astype(a.dtype)
        else:
            a[chg, :3] = v[:, ::-1]
        return (a,)
    m = im.mode
    if m == "P":
        idx = np.array(im); pal = np.array((im.getpalette() or [])[:768], dtype=np.int64).reshape(-1, 3)
        px = out[chg].astype(np.int64)
        idx[chg] = np.argmin(((px[:, None, :] - pal[None, :, :]) ** 2).sum(axis=2), axis=1).astype(idx.dtype)
        new = Image.fromarray(idx, "P"); new.putpalette(im.getpalette())
        if "transparency" in im.info:
            new.info["transparency"] = im.info["transparency"]
        return new
    if m == "CMYK":
        cm = np.array(im)
        cm[chg] = np.array(Image.fromarray(out[chg].reshape(-1, 1, 3)).convert("CMYK")).reshape(-1, 4)
        return Image.fromarray(cm, "CMYK")
    if m in ("RGB", "RGBA", "L", "LA", "1"):
        a = np.array(im)
        if m == "RGB":
            a[chg] = out[chg]
        elif m == "RGBA":
            a[chg, :3] = out[chg]
        elif m == "L":
            a[chg] = _grey(out[chg])
        elif m == "LA":
            a[chg, 0] = _grey(out[chg])
        else:                              # 1-bit: fromarray(bool, "1") is unreliable; go through L
            a[chg] = _grey(out[chg]) >= 128
            return Image.fromarray(a.astype(np.uint8) * 255, "L").convert("1", dither=Image.Dither.NONE)
        return Image.fromarray(a, m)
    base = im.convert("RGBA" if "A" in m else "RGB"); a = np.array(base); a[chg, :3] = out[chg]
    return Image.fromarray(a, base.mode)


def _jpeg_candidates(data, im, out, chg):
    """Two re-encodes with the original tables; -> list of bytes."""
    from PIL import Image, JpegImagePlugin
    kw = {}
    try:
        kw["qtables"] = im.quantization
        s = JpegImagePlugin.get_sampling(im)
        if s != -1:
            kw["subsampling"] = s
    except Exception:
        kw = {"quality": 95}
    if im.info.get("icc_profile"):
        kw["icc_profile"] = im.info["icc_profile"]
    cands = []
    if im.mode in ("RGB", "L", "CMYK"):
        b = io.BytesIO(); _native(im, out, chg, None).save(b, "JPEG", **kw); cands.append(b.getvalue())
    if im.mode == "RGB":
        try:
            y = Image.open(io.BytesIO(data)); y.draft("YCbCr", y.size); y.load()
            if y.mode == "YCbCr" and y.size == im.size:
                ya = np.array(y)
                ya[chg] = np.array(Image.fromarray(out[chg].reshape(-1, 1, 3)).convert("YCbCr")).reshape(-1, 3)
                b = io.BytesIO(); Image.fromarray(ya, "YCbCr").save(b, "JPEG", **kw); cands.append(b.getvalue())
        except Exception:
            pass
    return cands


def _encode(data, kind, im, out, chg, deep, boxes_mask):
    """-> (bytes, info about the method)."""
    if deep is not None:
        import cv2
        ok, buf = cv2.imencode(".png" if kind == "png" else ".tiff", _native(im, out, chg, deep)[0])
        return (buf.tobytes() if ok else None), "opencv-16bit"
    kw = {}
    if im.info.get("icc_profile"):
        kw["icc_profile"] = im.info["icc_profile"]
    if kind == "jpeg":
        best = None
        for i, c in enumerate(_jpeg_candidates(data, im, out, chg)):
            _, dec, _ = decode(c, "jpeg")
            ref = np.array(im.convert("RGB")).astype(np.int16)
            d = np.abs(dec.astype(np.int16) - ref).max(axis=2)
            d[boxes_mask] = 0
            score = (int(d.max()), int((d > 0).sum()))
            if best is None or score < best[0]:
                best = (score, c, ("rgb", "ycbcr")[i] if im.mode == "RGB" else im.mode.lower())
        return (best[1], "jpeg-%s max%d n%d" % (best[2], best[0][0], best[0][1])) if best else (None, "jpeg-none")
    new = _native(im, out, chg, None); buf = io.BytesIO()
    if im.info.get("dpi"):
        kw["dpi"] = im.info["dpi"]
    if new.mode == "P" and "transparency" in new.info:
        kw["transparency"] = new.info["transparency"]
    if kind == "png":
        new.save(buf, "PNG", **kw)
    elif kind == "gif":
        for k in ("duration", "loop", "background"):
            if k in im.info:
                kw[k] = im.info[k]
        kw.pop("icc_profile", None); kw.pop("dpi", None)
        new.save(buf, "GIF", optimize=False, **kw)
    elif kind == "tiff":
        comp = im.info.get("compression", "raw")
        if comp in ("jpeg", "tiff_jpeg"):
            comp = "tiff_lzw"
        if comp in ("group3", "group4", "tiff_ccitt") and new.mode != "1":
            comp = "tiff_lzw"
        new.save(buf, "TIFF", compression=comp, **kw)
    else:
        new.save(buf, "BMP")
    return buf.getvalue(), "%s-%s" % (kind, new.mode)


def ocr_and_paint(data, kind, mapping, source, extra_values=()):
    """-> (new_bytes or None, info dict). new_bytes is None when nothing was found or nothing could be painted."""
    from deid.vision import ocr
    info = dict(words=0, hits=0, painted=0, skipped=None)
    im, rgb, deep = decode(data, kind)
    H, W = rgb.shape[:2]
    if min(W, H) < MIN_SIDE:
        info["skipped"] = "small"; return None, info
    if W * H > MAX_PIXELS:
        info["skipped"] = "too large for OCR (%dx%d)" % (W, H); return None, info
    if W * H > OCR_PIXELS:
        from PIL import Image
        sc = (OCR_PIXELS / float(W * H)) ** 0.5; sw, sh = max(1, int(W * sc)), max(1, int(H * sc))
        small = np.array(Image.fromarray(rgb).resize((sw, sh), Image.LANCZOS))
        fx, fy = W / float(sw), H / float(sh)
        words = [dict(w, quad=[[x * fx, y * fy] for x, y in w["quad"]]) for w in ocr.read_words(small)]
        info["ocr_scale"] = round(1.0 / fx, 4)
    else:
        words = ocr.read_words(rgb)
    info["words"] = len(words)
    drawing = ocr.is_drawing(words); info["gate"] = "drawing" if drawing else "other"
    cand = ocr.find_hits(words, mapping, source, extra_values, rules=ocr.RULES_ALL)
    info["candidates"] = [(h["why"], h["old"], h["new"], ocr.box_of(h["quad"], rgb.shape)) for h in cand]
    rules = RULES_DRAWING if drawing else RULES_OTHER
    hits = [h for h in ocr.find_hits(words, mapping, source, extra_values, rules=rules)]
    info["hits"] = len(hits)
    info["hit_list"] = [(h["why"], h["old"], h["new"]) for h in hits]
    info["boxes"] = [ocr.box_of(h["quad"], rgb.shape) for h in hits]
    if not hits:
        return None, info
    if getattr(im, "n_frames", 1) > 1:
        info["skipped"] = "hit found but %s has %d frames; not painted" % (kind, im.n_frames); return None, info
    out = rgb.copy()
    for h in hits:
        ocr.paint(out, h["quad"], h["old"], h["new"])
    chg = (out != rgb).any(axis=2)
    if not chg.any():
        info["skipped"] = "hit found but no ink to paint"; return None, info
    mask = np.zeros(chg.shape, bool)
    for (x0, y0, x1, y1) in info["boxes"]:
        mask[y0:y1, x0:x1] = True
    new, how = _encode(data, kind, im, out, chg, deep, mask)
    info["method"] = how
    if new is None:
        info["skipped"] = "hit found but re-encode failed (%s)" % how; return None, info
    _, dec, _ = decode(new, kind)
    if dec.shape != rgb.shape:
        info["skipped"] = "painted image did not decode at the same size"; return None, info
    left = ocr.verify_gone(dec, hits)
    info["verify"] = "failed" if left else "ok"
    if left:
        info["verify_left"] = left
        info["skipped"] = "second OCR read still finds %s; original kept" % ",".join(left); return None, info
    d = np.abs(dec.astype(np.int16) - rgb.astype(np.int16)).max(axis=2); d[mask] = 0
    info["out_px"] = int((d > 0).sum()); info["out_max"] = int(d.max())
    info["painted"] = len(hits)
    return new, info


def _strip(kind, data):
    if kind == "jpeg":
        return imgmeta.strip_jpeg(data)
    if kind == "png":
        return imgmeta.strip_png(data)
    if kind == "tiff":
        return imgmeta.strip_tiff(data)
    if kind == "gif":
        return imgmeta.strip_gif(data)
    return data, []


@register("image", lambda path, head: path.endswith(EXTS), priority=50)
def handle(data, ctx):
    return process(data, ctx)[0]


def process(data, ctx):
    """-> (Result, info). info has the OCR word count, gate, candidates, hit list and boxes (used by the tests)."""
    info = {}
    try:
        kind = _kind(data[:16])
        if kind is None:
            return Result(None, status="unchanged", notes=["image extension but unknown content; left as is"]), info
        notes = []; changed = 0
        cur, removed = _strip(kind, data)
        if any(r.startswith("error:") for r in removed):
            notes.append("metadata strip skipped: " + ",".join(removed)); removed = []; cur = data
        real = [r for r in removed if not r.startswith("kept:")]
        if real:
            changed += len(real); notes.append("metadata removed: " + ",".join(sorted(set(real))))
        orient = _orientation(removed)
        try:
            painted, info = ocr_and_paint(cur, kind, ctx.mapping, ctx.source)
        except Exception as e:
            painted, info = None, dict(skipped="ocr error %s" % type(e).__name__, hits=0, painted=0)
        status = "ok"
        if info.get("skipped") and info.get("hits"):
            status = "partial"; notes.append(info["skipped"])
        if painted is not None:
            painted, _ = _strip(kind, painted)
            if kind == "jpeg" and orient and orient != 1:
                seg = b"Exif\x00\x00" + imgmeta._min_tiff(orient)
                seg = b"\xff\xe1" + (len(seg) + 2).to_bytes(2, "big") + seg
                at = 2
                if painted[2:4] == b"\xff\xe0":          # keep JFIF APP0 first
                    at = 4 + int.from_bytes(painted[4:6], "big")
                painted = painted[:at] + seg + painted[at:]
            cur = painted; changed += info["painted"]; notes.append("painted %d OCR hits" % info["painted"])
        if cur is data or cur == data:
            return Result(None, status="unchanged" if status == "ok" else status, notes=notes), info
        return Result(cur, changed=changed, status=status, notes=notes), info
    except Exception as e:
        return Result(None, status="failed", notes=["image handler error: %s" % type(e).__name__]), info
