"""Run the de-identification pipeline over an input tree.

python -I run_pipeline.py --input ~/machina/input --identities <truth>/identities.json \
    --out <attempt>/corpus --log <private>/run.jsonl --workers 12
"""
import collections
import json
import multiprocessing as mp
import os
import shutil
import sys
import time

from deid_eval import fmt
from deid_eval.truth import list_files

from .engine import Engine
from .process import process, rename

VALIDATE = {'pdf', 'zip', 'gzip', 'tar', 'image'}
MAX_PROC = 1_200_000_000

_E = None
_IN = _OUT = None


def _init(ids_path, inp, out):
    global _E, _IN, _OUT
    ids = json.load(open(ids_path, encoding='utf-8'))
    _E, _IN, _OUT = Engine(ids), inp, out


def _one(rel):
    t = time.time()
    src = os.path.join(_IN, rel)
    newrel = rename(_E, rel)
    dst = os.path.join(_OUT, newrel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    rec = {'rel': rel, 'new': newrel, 'renamed': newrel != rel}
    size = os.path.getsize(src)
    if size > MAX_PROC:
        shutil.copyfile(src, dst)
        rec.update(kind='big', n=0, status='copied-big')
        return rec
    with open(src, 'rb') as fh:
        data = fh.read()
    out, n, kind = process(_E, rel, data)
    status = 'ok'
    if kind.split('!')[0] in VALIDATE and out != data:
        before = fmt.analyze(None, rel, data=data)
        after = fmt.analyze(None, newrel, data=out)
        if before['opens'] and (not after['opens'] or after['struct'] != before['struct']):
            out2, n2 = _E.sub_bytes(data, same_len=True)
            a2 = fmt.analyze(None, newrel, data=out2)
            if a2['opens'] and a2['struct'] == before['struct']:
                out, n, status = out2, n2, 'fallback-bytes'
            else:
                out, n, status = data, 0, 'fallback-copy'
    with open(dst, 'wb') as fh:
        fh.write(out)
    rec.update(kind=kind, n=n, status=status, s=round(time.time() - t, 2))
    return rec


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='run_pipeline')
    ap.add_argument('--input', required=True)
    ap.add_argument('--identities', required=True)
    ap.add_argument('--out', required=True, help='attempt corpus/ folder (created)')
    ap.add_argument('--log', required=True, help='private per-file log (jsonl); keep outside the submission')
    ap.add_argument('--workers', type=int, default=max(1, os.cpu_count() - 4))
    ap.add_argument('--prefix', default='')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--files', default='', help='optional text file listing relative paths to process')
    a = ap.parse_args(argv)
    files = [l.rstrip('\n') for l in open(a.files, encoding='utf-8')] if a.files else list_files(a.input, a.prefix, a.limit)
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    stats = collections.Counter()
    seen = {}
    with mp.Pool(a.workers, initializer=_init, initargs=(a.identities, a.input, a.out)) as pool, open(a.log, 'w', encoding='utf-8') as lg:
        for i, rec in enumerate(pool.imap_unordered(_one, files, chunksize=4)):
            stats[rec['status']] += 1
            stats['renamed'] += rec['renamed']
            stats['replacements'] += rec['n']
            if rec['new'] in seen:
                stats['COLLISION'] += 1
            seen[rec['new']] = rec['rel']
            lg.write(json.dumps(rec, ensure_ascii=False) + '\n')
            if (i + 1) % 5000 == 0:
                print(f'[pipeline] {i + 1}/{len(files)} {time.time() - t0:.0f}s {dict(stats)}', flush=True)
    print(f'[pipeline] done {len(files)} files in {time.time() - t0:.0f}s {dict(stats)}', flush=True)


if __name__ == '__main__':
    main(sys.argv[1:])
