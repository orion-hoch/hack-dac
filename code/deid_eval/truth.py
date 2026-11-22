"""Build ground truth from the raw input: identities, occurrences, fingerprints.

Output (private, never commit): <out>/identities.json, <out>/files.jsonl.gz
"""
import collections
import gzip
import json
import multiprocessing as mp
import os
import sys
import time
import unicodedata

import ahocorasick

from . import fmt, idents

BUNDLE_CATS = {'pdf_metadata', 'visible_text', 'native_properties', 'cad_headers', 'archives', 'images', 'raw'}
OPAQUE = {'creo', 'dwg', 'bin', 'parasolid_bin', '7z', 'rar'}


def list_files(root, prefix='', limit=0):
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if not d.startswith('.get-input'))
        for n in sorted(fn):
            rel = os.path.relpath(os.path.join(dp, n), root)
            if rel.startswith(prefix):
                out.append(rel)
    return out[:limit] if limit else out


def norm_map(s):
    """Normalized string (accent-folded, lowercased) plus map normalized-index -> original-index."""
    chars, mp_ = [], []
    for i, c in enumerate(s):
        d = unicodedata.normalize('NFKD', c)
        d = ''.join(x for x in d if not unicodedata.combining(x)).lower()
        for x in d:
            chars.append(x); mp_.append(i)
    return ''.join(chars), mp_


# ---------------------------------------------------------------- pass 1
_ROOT = None
_OUT = None
MAX_FILE_TEXT = 6 * 1024 * 1024


def _init1(root, out):
    global _ROOT, _OUT
    _ROOT, _OUT = root, out


def _append(prefix, rec):
    path = os.path.join(_OUT, 'shards', f'{prefix}-{os.getpid()}.jsonl.gz')
    with open(path, 'ab') as fh:
        fh.write(gzip.compress((json.dumps(rec, ensure_ascii=False) + '\n').encode('utf-8'), compresslevel=1))


def _pass1(rel):
    F = fmt.analyze(os.path.join(_ROOT, rel), rel)
    tot, keep = 0, []
    for e in F['bundle']:
        if tot >= MAX_FILE_TEXT:
            break
        e[2] = e[2][:MAX_FILE_TEXT - tot]; tot += len(e[2]); keep.append(e)
    F['bundle'] = keep
    cand = set()
    for cat, key, t in F['bundle']:
        for s, k in idents.from_text(t, cat):
            cand.add((s, k, 'text'))
    for cat, field, idx, v, kind in F['slots']:
        for s, k in idents.from_slot(v, kind):
            cand.add((s, k, 'slot'))
    for a, b, s, k in idents.path_structure(rel):
        cand.add((s, k, 'path'))
    F['cand'] = sorted(cand)
    _append('p1', F)
    return {'rel': rel, 'cand': F['cand']}


# ---------------------------------------------------------------- pass 2
_A = None
_IDS = None


def _init2(root, automaton, ids, out):
    global _ROOT, _A, _IDS, _OUT
    _ROOT, _A, _IDS, _OUT = root, automaton, ids, out


def _pass2_shard(path):
    n = 0
    with gzip.open(path, 'rt', encoding='utf-8') as fi:
        for line in fi:
            _append('p2', _pass2(line)); n += 1
    return n


def find(text_norm, want_modes=('word', 'sub')):
    """Yield (start, end, ident_id) over normalized text."""
    if not text_norm:
        return
    for end, (iid, mode, L) in _A.iter(text_norm):
        if mode not in want_modes:
            continue
        s = end - L + 1
        if mode in ('word', 'slot'):
            if (s > 0 and text_norm[s - 1].isalnum()) or (end + 1 < len(text_norm) and text_norm[end + 1].isalnum()):
                continue
        yield s, end + 1, iid


def spans_in(original, modes=('word', 'sub', 'slot')):
    n, m = norm_map(original)
    out = []
    for s, e, iid in find(n, modes):
        out.append((m[s], m[e - 1] + 1, iid))
    # keep longest non-overlapping
    out.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    res, last = [], -1
    for s, e, iid in out:
        if s >= last:
            res.append([s, e, iid]); last = e
    return res


def _pass2(line):
    F = json.loads(line)
    rel = F['rel']
    occ = collections.defaultdict(set)
    hit_keys = set()
    for cat, key, t in F.get('bundle', []):
        c = 'cad_headers' if cat == 'raw' else cat
        tn = idents.norm(t) if len(t) < 200000 else t.lower()
        for s, e, iid in find(tn):
            occ[iid].add(c); hit_keys.add(key)
    # path: structural spans + any dictionary identity (word/sub, plus slot-mode identities inside path segments)
    pspans = spans_in(rel, ('word', 'sub', 'slot'))
    for a, b, s, k in idents.path_structure(rel):
        iid = idents.norm(s)
        if iid in _IDS and not any(x[0] <= a < x[1] or a <= x[0] < b for x in pspans):
            pspans.append([a, b, iid])
    pspans.sort()
    # slot occurrences (for consistency / meaningful)
    socc = []
    for cat, field, idx, v, kind in F['slots']:
        sp = spans_in(v)
        if sp:
            socc.append([cat, field, idx, v, kind, sp])
            for _, _, iid in sp:
                occ[iid].add('slot:' + cat)
    # identity-free chunks
    free = []
    for key, hh in F['chunks']:
        if not any(k == key or k.startswith(key + ':') for k in hit_keys):
            free.append(hh)
    lines = []
    if F['kind'] in OPAQUE and F['opens'] and 0 < F['size'] <= 64_000_000:
        try:
            with open(os.path.join(_ROOT, rel), 'rb') as fh:
                b = fh.read()
            lines = fmt.line_sample(b, lambda s: not any(True for _ in find(idents.norm(s), ('word', 'sub', 'slot'))))
        except Exception:
            lines = []
    F.pop('bundle', None)
    F.pop('cand', None)
    F['occ'] = {k: sorted(v) for k, v in occ.items()}
    F['path_occ'] = pspans
    F['slot_occ'] = socc
    F['free_chunks'] = free
    F['line_sample'] = lines
    return F


def build_automaton(ids):
    A = ahocorasick.Automaton()
    for iid, rec in ids.items():
        for v in idents.variants(iid):
            if len(v) >= 2:
                A.add_word(v, (iid, rec['mode'], len(v)))
    A.make_automaton()
    return A


def mode_for(n, kind, src_kinds):
    if n in {idents.norm(x) for x in idents.MANUAL_MIN_CTX}:
        return 'slot'      # short acronyms (ABC, XYZ, QRS...): only as whole path/field tokens
    L = len(n)
    if idents.path_only(n):
        return 'slot'
    if L < 4 or (kind == 'project' and set(src_kinds) <= {'path'} and not idents.distinctive_repo(n)):
        return 'slot'      # short or generic names (e.g. repo 'cnc'): only as whole path/field tokens
    if L < 6 or n.isalpha() and L < 8 and kind in ('account', 'person'):
        return 'word'
    return 'sub'


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='deid truth')
    ap.add_argument('--input', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--workers', type=int, default=max(1, os.cpu_count() - 2))
    ap.add_argument('--prefix', default='')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--files', default='', help='text file with relative paths to (pass-1) process')
    ap.add_argument('--append', action='store_true', help='keep existing pass-1 shards; only process new files')
    ap.add_argument('--pass1-only', action='store_true')
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    files = [l.rstrip('\n') for l in open(a.files, encoding='utf-8') if l.strip()] if a.files else list_files(a.input, a.prefix, a.limit)
    t0 = time.time()
    print(f'[truth] {len(files)} files, {a.workers} workers', flush=True)

    import shutil
    sd = os.path.join(a.out, 'shards')
    done_rels = set()
    if a.append and os.path.isdir(sd):
        for fn in os.listdir(sd):
            if fn.startswith('p1-'):
                with gzip.open(os.path.join(sd, fn), 'rt', encoding='utf-8') as fi:
                    for line in fi:
                        done_rels.add(json.loads(line)['rel'])
        for fn in os.listdir(sd):
            if fn.startswith('p2-'):
                os.remove(os.path.join(sd, fn))
    else:
        shutil.rmtree(sd, ignore_errors=True)
    os.makedirs(sd, exist_ok=True)
    files = [f for f in files if f not in done_rels]
    print(f'[truth] {len(done_rels)} files already in pass-1 shards; {len(files)} to process', flush=True)
    cand = collections.defaultdict(lambda: {'forms': collections.Counter(), 'kinds': collections.Counter(), 'files': 0, 'src': collections.Counter()})
    with mp.Pool(a.workers, initializer=_init1, initargs=(a.input, a.out)) as pool:
        for i, F in enumerate(pool.imap_unordered(_pass1, files, chunksize=4)):
            for s, k, src in F['cand']:
                n = idents.norm(s)
                if len(n) < 2:
                    continue
                c = cand[n]
                c['forms'][s] += 1; c['kinds'][k] += 1; c['files'] += 1; c['src'][src] += 1
            if (i + 1) % 2000 == 0:
                print(f'[truth] pass1 {i + 1}/{len(files)} {time.time() - t0:.0f}s', flush=True)
    if done_rels:   # add candidates from earlier pass-1 shards
        for fn in os.listdir(sd):
            if fn.startswith('p1-'):
                with gzip.open(os.path.join(sd, fn), 'rt', encoding='utf-8') as fi:
                    for line in fi:
                        F = json.loads(line)
                        if F['rel'] not in done_rels:
                            continue
                        for s_, k, src in F['cand']:
                            n_ = idents.norm(s_)
                            if len(n_) >= 2:
                                c = cand[n_]; c['forms'][s_] += 1; c['kinds'][k] += 1; c['files'] += 1; c['src'][src] += 1
    if a.pass1_only:
        print(f'[truth] pass-1 only: done in {time.time() - t0:.0f}s', flush=True)
        return
    files = files + sorted(done_rels)
    # manual identities: only if they appear in some path
    allpaths = '\n'.join(idents.norm(r) for r in files)
    for s, k in idents.MANUAL:
        n = idents.norm(s)
        if n in allpaths:
            c = cand[n]; c['forms'][s] += 1; c['kinds'][k] += 1; c['src']['manual'] += 1

    ids = {}
    for n, c in cand.items():
        kind = c['kinds'].most_common(1)[0][0]
        if not idents.ok_identity(n, kind, c['src']):
            continue
        ids[n] = {'forms': [f for f, _ in c['forms'].most_common(8)], 'kind': kind, 'files': c['files'],
                  'src': dict(c['src']), 'mode': mode_for(n, kind, c['src'])}
    # drop identities that are substrings-of-everything noise: appear as candidates in > 20% of files from text only
    nfiles = max(1, len(files))
    ids = {n: r for n, r in ids.items() if not (r['src'].get('text', 0) > 0.2 * nfiles and not r['src'].get('slot') and not r['src'].get('path'))}
    with open(os.path.join(a.out, 'identities.json'), 'w', encoding='utf-8') as fo:
        json.dump(ids, fo, ensure_ascii=False, indent=0)
    print(f'[truth] {len(ids)} identities ({collections.Counter(r["kind"] for r in ids.values())}) {time.time() - t0:.0f}s', flush=True)

    A = build_automaton(ids)
    shards = sorted(os.path.join(a.out, 'shards', f) for f in os.listdir(os.path.join(a.out, 'shards')) if f.startswith('p1-'))
    done = 0
    with mp.Pool(min(a.workers, len(shards)), initializer=_init2, initargs=(a.input, A, set(ids), a.out)) as pool:
        for n in pool.imap_unordered(_pass2_shard, shards):
            done += n
            print(f'[truth] pass2 {done}/{len(files)} {time.time() - t0:.0f}s', flush=True)
    print(f'[truth] done in {time.time() - t0:.0f}s -> {a.out}', flush=True)


if __name__ == '__main__':
    main(sys.argv[1:])
