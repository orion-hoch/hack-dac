#!/usr/bin/env python3
"""Fill the content-addressed OCR cache over a corpus tree, in parallel. PIXELS builder.

It runs the REAL handlers (deid/handlers/pdf.py with DEID_PDF_OCR=1, deid/handlers/image.py) on every distinct PDF and
raster image, so every OCR read the full run will ask for (page reads, second reads after painting) is computed once
and stored by ocr.read_words(). Output bytes are discarded; their sha256 goes to the log, so two runs can be compared.

Usage:  ocr_precompute.py ROOT [--list FILE] [--workers N] [--threads T] [--log PATH] [--cache DIR]
  ROOT     corpus root (relative paths under it decide the source, as in the pipeline)
  --list   optional file of relative paths (one per line) instead of walking ROOT
  --workers processes (default 2); --threads onnxruntime threads per process (default 2). 30 cores: 15 x 2.
  --log    jsonl, one record per distinct file: rel, sha, kind, status, sec, ocr hit/miss/fresh_sec, out_sha, ocr info.
           Resumable: files whose sha is already in the log are skipped.
  --cache  cache directory (default $DEID_OCR_CACHE or ~/hack-osu/private/ocr_cache)
Identical bytes are processed once (the cache key is the pixels, so duplicates would only hit the cache anyway).
"""
import os
os.environ["DEID_OCR_FRESH"] = "1"   # this tool fills the cache: fresh OCR on a miss, sys, json, time, hashlib, argparse
from multiprocessing import Pool

PDF_EXT = (".pdf",)
IMG_EXT = (".png", ".jpg", ".jpeg", ".jpe", ".tif", ".tiff", ".bmp", ".gif")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
G = {}


def _init(threads, cache, root):
    os.environ["DEID_OCR_THREADS"] = str(threads); os.environ["DEID_PDF_OCR"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"; os.environ["OPENBLAS_NUM_THREADS"] = "1"
    if cache is not None:
        os.environ["DEID_OCR_CACHE"] = cache
    sys.path.insert(0, HERE)
    try:
        import cv2; cv2.setNumThreads(1)
    except Exception:
        pass
    from deid.mapping import Mapping
    G["M"] = Mapping(); G["root"] = root


def _sha(args):
    root, rel = args
    try:
        h = hashlib.sha256()
        with open(os.path.join(root, rel), "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        return rel, h.hexdigest(), os.path.getsize(os.path.join(root, rel))
    except Exception:
        return rel, None, 0


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items() if k not in ("_boxes", "keywords")}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (str, int, float, bool)) or o is None:
        return o
    try:
        return float(o)
    except Exception:
        return str(o)


def one(args):
    rel, sha = args
    from deid.api import Ctx, Result
    from deid.mapping import source_of
    from deid.vision import ocr
    from deid.handlers import pdf as hpdf, image as himg
    data = open(os.path.join(G["root"], rel), "rb").read()
    ctx = Ctx(old_path=rel, new_path=rel, source=source_of(rel), mapping=G["M"], rename=lambda s: s,
              recurse=lambda b, p: Result(None, status="unchanged"))
    s0 = dict(ocr.STATS); t0 = time.perf_counter(); rec = dict(rel=rel, sha=sha)
    try:
        if rel.lower().endswith(PDF_EXT):
            rec["kind"] = "pdf"; r, st = hpdf.process(data, ctx)
            rec["ocr"] = _clean(st.get("_ocr", [])); rec["pages"] = len(st.get("_ocr", []))
        else:
            rec["kind"] = "image"; r, info = himg.process(data, ctx)
            rec["ocr"] = _clean({k: v for k, v in (info or {}).items()})
        rec["status"] = r.status; rec["notes"] = r.notes[:6]
        out = r.data if r.data is not None else data
        rec["out_sha"] = hashlib.sha256(out).hexdigest(); rec["changed"] = r.data is not None
    except Exception as e:
        rec["err"] = "%s: %s" % (type(e).__name__, str(e)[:160])
    rec["sec"] = round(time.perf_counter() - t0, 3)
    rec["cache_hit"] = ocr.STATS["hit"] - s0["hit"]; rec["cache_miss"] = ocr.STATS["miss"] - s0["miss"]
    rec["fresh_sec"] = round(ocr.STATS["fresh_sec"] - s0["fresh_sec"], 3)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root"); ap.add_argument("--list"); ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--threads", type=int, default=2); ap.add_argument("--log", default="ocr_precompute.jsonl")
    ap.add_argument("--cache", default=None)
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    if a.list:
        rels = [l.rstrip("\n") for l in open(a.list) if l.strip()]
    else:
        rels = []
        for d, _, fs in os.walk(root):
            for f in fs:
                if f.lower().endswith(PDF_EXT + IMG_EXT):
                    rels.append(os.path.relpath(os.path.join(d, f), root))
        rels.sort()
    rels = [r for r in rels if r.lower().endswith(PDF_EXT + IMG_EXT)]
    done = set()
    if os.path.exists(a.log):
        for l in open(a.log):
            try:
                done.add(json.loads(l)["sha"])
            except Exception:
                pass
    with Pool(a.workers) as p:
        hs = p.map(_sha, [(root, r) for r in rels], chunksize=16)
    seen = {}; jobs = []
    for rel, sha, size in sorted(hs, key=lambda t: (-t[2], t[0])):
        if sha and sha not in seen and sha not in done:
            seen[sha] = rel; jobs.append((rel, sha))
    print("files %d distinct new %d (already logged %d)" % (len(rels), len(jobs), len(done)), flush=True)
    t0 = time.time(); n = 0
    with Pool(a.workers, initializer=_init, initargs=(a.threads, a.cache, root)) as p, open(a.log, "a") as log:
        for rec in p.imap_unordered(one, jobs, chunksize=1):
            log.write(json.dumps(rec, sort_keys=True) + "\n"); log.flush(); n += 1
            if n % 50 == 0:
                print("%d/%d %.0fs" % (n, len(jobs), time.time() - t0), flush=True)
    print("done %d in %.0fs" % (n, time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
