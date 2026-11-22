#!/usr/bin/env python3
"""Assemble and check a submission folder (integrator's code, MIT).

  venv/bin/python -I submit.py make      ATTEMPT_DIR RUN_LOG.jsonl     # adds code/ and README.txt next to corpus/
  venv/bin/python -I submit.py preflight ATTEMPT_DIR RUN_LOG.jsonl IDENTITIES.json [INPUT_ROOT]

make: code/ is a WHITELIST copy of pipeline_v2/code (no identity table, key, log, or scorer with name lists);
README.txt lists omissions and limits by FINAL path only.
preflight: exits 1 on any failure. Checks corpus/ (object count, 2 GB, temp files, one output per input, extensions),
code/ (pinned requirements, no PyMuPDF, no keys/tables/logs, no identity hit), README.txt (no identity hit, no
original path, every listed path exists), and that the stand-in key is not anywhere in code/ or README.txt.
"""
import json, os, re, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.join(HERE, 'code')
SHIP = ['run_v2.py', 'requirements.txt', 'LICENSE', 'pipeline/__init__.py', 'pipeline/engine.py', 'pipeline/v2.py',
        'pipeline/gate.py', 'pipeline/pseudo.py', 'pipeline/creo.py', 'pipeline/dwg_inplace.py',
        'pipeline/creo_route.py', 'pipeline/andre_image.py', 'deid_eval/__init__.py', 'deid_eval/fmt.py',
        'data/stoplist.tsv', 'data/logo_hashes.tsv', 'data/vector_logos.tsv', 'data/words_en.txt',
        'pipeline/fieldrules.py', 'pipeline/filematch.py', 'tools/filter_ids.py']
SHIP_DIRS = ['deid']
LISTED = {'kept': 'content unchanged (format not edited in this attempt)', 'gate-failed':
          'content unchanged (edit refused by its reader check)', 'error': 'content unchanged (handler error)',
          'worker-died': 'content unchanged (worker died)', 'timeout': 'content unchanged (time limit)',
          'copied-big': 'content unchanged (over size cap)'}
LIMITS = """Limitations (attempt A):
- OCR of drawings without a text layer is off and its font is not shipped.
- Two- and three-letter initials in title blocks are not replaced (field and file modes not implemented).
- Stand-ins are same-length letter substitutions under one secret key (vowel->vowel, consonant->consonant,
  digit->digit, case kept). They defeat search and keep names consistent everywhere, but a determined reader can
  recognise a public repository name from its length and shape. The key is not part of this submission.
- Formats whose content is NOT edited in this attempt (path renamed, bytes unchanged): DWG, 7z, JPEG/TIFF/BMP/GIF
  images, binary Parasolid, unknown binary formats. Names inside them remain. Listed below by final path.
- Not handled: logos drawn as vector shapes, names drawn as glyph outlines or pixels in PDFs without a text layer
  (OCR is off), names inside fonts without ToUnicode maps, previews inside drawing files.
- Archives: tar and .Z carry no checksum over member data, so their reader check covers structure only.
"""


def make(att, log):
    dst = os.path.join(att, 'code')
    if os.path.exists(dst):
        shutil.rmtree(dst)
    for f in SHIP:
        os.makedirs(os.path.dirname(os.path.join(dst, f)) or dst, exist_ok=True)
        shutil.copy2(os.path.join(CODE, f), os.path.join(dst, f))
    for d in SHIP_DIRS:
        shutil.copytree(os.path.join(CODE, d), os.path.join(dst, d), ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'tests', 'fonts'))
    listed, n = [], 0
    for line in open(log, encoding='utf-8'):
        r = json.loads(line)
        n += 1
        if r.get('status') in LISTED:
            listed.append((r['new'], r.get('route', ''), LISTED[r['status']]))
    listed.sort()
    v = sys.version.split()[0]
    with open(os.path.join(att, 'README.txt'), 'w', encoding='utf-8') as f:
        f.write('De-identified corpus, attempt A. Pipeline: code/ (MIT). Built on the teammate pipeline (runner, rename,\n'
                'engine) with gated format handlers.\n\nHow to run (Linux, Python %s exactly: the stand-in tables use\n'
                "Python's Unicode data, so one Python version must be used for the whole run):\n"
                '  python3 -m venv venv && venv/bin/pip install -r code/requirements.txt\n'
                '  export DEID_PSEUDO_KEY_FILE=/path/to/private/key   # secret, not included\n'
                '  venv/bin/python -I code/run_v2.py --input INPUT --out OUT/corpus --log PRIVATE/run.jsonl \\\n'
                '      --identities PRIVATE/identities.json --workers 8\n'
                'The identity table and the key are private and not included. Every output is re-opened by a reader\n'
                'before it is written (code/pipeline/gate.py); a refused edit is replaced by the original bytes under\n'
                'the new name and listed below. No input file is omitted: %d files in, %d out.\n\n' % (v, n, n))
        f.write(LIMITS)
        f.write('\nFiles whose content was not edited, by final path (%d):\n' % len(listed))
        for p, route, why in listed:
            f.write('%s\t%s\n' % (p, why))
    print('made', att, 'listed', len(listed))


def preflight(att, log, ids_path, inp=None):
    sys.path.insert(0, CODE)
    from pipeline.engine import Engine
    fails = []
    corp = os.path.join(att, 'corpus')
    nobj, big, tmp = 0, [], 0
    for d, _ds, fs in os.walk(corp):
        for x in fs:
            nobj += 1
            p = os.path.join(d, x)
            if x.endswith('.v2tmp'):
                tmp += 1
            if os.path.getsize(p) > 2 * 2**30:
                big.append(p)
    recs = [json.loads(l) for l in open(log, encoding='utf-8')]
    news = {r['new'] for r in recs}
    missing = [r['new'] for r in recs if not os.path.exists(os.path.join(corp, r['new']))]
    extbad = [r for r in recs if os.path.splitext(r['rel'])[1] != os.path.splitext(r['new'])[1]]
    if nobj > 400_000: fails.append('corpus objects %d > 400000' % nobj)
    if big: fails.append('%d files over 2 GB' % len(big))
    if tmp: fails.append('%d temp files' % tmp)
    if missing: fails.append('%d logged outputs missing' % len(missing))
    if extbad: fails.append('%d extensions changed' % len(extbad))
    if nobj != len(news): fails.append('corpus has %d objects, log has %d outputs' % (nobj, len(news)))
    if inp:
        nin = sum(len(fs) for d, ds, fs in os.walk(inp) if '.get-input' not in d)
        if nin != nobj: fails.append('input %d files, corpus %d' % (nin, nobj))
    code = os.path.join(att, 'code')
    req = open(os.path.join(code, 'requirements.txt')).read().split('\n')
    unpinned = [l for l in req if l.strip() and not l.startswith('#') and '==' not in l]
    if unpinned: fails.append('unpinned requirements: %s' % unpinned)
    key = b''
    kp = os.environ.get('DEID_PSEUDO_KEY_FILE')
    if kp and os.path.exists(kp):
        key = open(kp, 'rb').read().strip()
    E = Engine(json.load(open(ids_path, encoding='utf-8')))
    WORDS = {w.strip() for w in open(os.path.join(CODE, 'data', 'words_en.txt'), encoding='utf-8') if w.strip() and not w.startswith('#')}
    ALLOW = {}                      # reviewed false positives: relpath <TAB> token <TAB> reason (kept outside code/)
    ap = os.path.join(HERE, 'preflight_allow.tsv')
    if os.path.exists(ap):
        for line in open(ap, encoding='utf-8'):
            a = line.rstrip('\n').split('\t')
            if len(a) >= 3 and not a[0].startswith('#'):
                ALLOW.setdefault(a[0], set()).add(a[1])
                print('allowed by review: %s %r (%s)' % (a[0], a[1], a[2]))
    hits = {}
    files = [os.path.join(d, x) for d, _ds, fs in os.walk(code) for x in fs] + [os.path.join(att, 'README.txt')]
    for p in files:
        rel = os.path.relpath(p, att)
        if re.search(r'\.(jsonl|log|key)$|identit', rel, re.I):
            fails.append('forbidden file in submission: %s' % rel)
        b = open(p, 'rb').read()
        if re.search(rb'^\s*(import|from)\s+(pymupdf|fitz)\b', b, re.M) and not rel.endswith('fmt.py'):
            fails.append('PyMuPDF import in %s' % rel)
        if key and key in b:
            fails.append('stand-in key found in %s' % rel)
        h = E.find(b)
        toks = sorted({b[s:e].decode('latin-1') for s, e, _i, _e in h} - ALLOW.get(rel, set()) - ALLOW.get(os.path.basename(rel), set()))
        toks = [t for t in toks if t.lower() not in WORDS]       # dictionary words are not names in code or README
        if toks:
            hits[rel] = toks[:8]
    if hits:
        fails.append('identity hits in code/README: %s' % json.dumps(hits, ensure_ascii=False)[:1500])
    readme = open(os.path.join(att, 'README.txt'), encoding='utf-8').read()
    leaked_old = [r['rel'] for r in recs if r['rel'] != r['new'] and r['rel'] in readme]
    if leaked_old: fails.append('%d original paths in README' % len(leaked_old))
    for line in readme.split('\n'):
        if '\t' in line and not os.path.exists(os.path.join(corp, line.split('\t')[0])):
            fails.append('README lists a path not in corpus/'); break
    print(json.dumps({'objects': nobj, 'outputs_logged': len(news), 'fails': fails}, indent=1, ensure_ascii=False))
    return 1 if fails else 0


if __name__ == '__main__':
    if sys.argv[1] == 'make':
        make(sys.argv[2], sys.argv[3])
    else:
        sys.exit(preflight(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5] if len(sys.argv) > 5 else None))
