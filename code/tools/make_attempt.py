"""Assemble code/ and README.txt for an attempt folder whose corpus/ the pipeline already wrote.

Usage: python -I tools/make_attempt.py --repo <this repo> --attempt <attempt dir> --log <private run.jsonl>
       [--truth <private truth dir>]   (to list source defects: unreadable/invalid input files)
Never copies private material: no identities, mappings, logs, raw input or local-only tools.
"""
import argparse
import collections
import datetime
import gzip
import json
import os
import shutil

CODE_ITEMS = ['deid_eval', 'pipeline', 'run_pipeline.py', 'deid.py', 'requirements.txt', 'docs/approaches.md',
              'docs/scorer-model.md', 'tools/complete_files.py', 'tools/make_attempt.py']

ap = argparse.ArgumentParser()
ap.add_argument('--repo', required=True)
ap.add_argument('--attempt', required=True)
ap.add_argument('--log', required=True)
ap.add_argument('--truth', default='')
ap.add_argument('--label', default='attempt')
a = ap.parse_args()

code = os.path.join(a.attempt, 'code')
shutil.rmtree(code, ignore_errors=True)
for item in CODE_ITEMS:
    src = os.path.join(a.repo, item)
    dst = os.path.join(code, item)
    if os.path.isdir(src):
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    elif os.path.isfile(src):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

recs = [json.loads(l) for l in open(a.log, encoding='utf-8')]
by_rel = {r['rel']: r for r in recs}
stats = collections.Counter(r['status'] for r in recs)
kinds = collections.Counter(r['kind'].split('!')[0] for r in recs)
empty = sorted(r['new'] for r in recs if r['kind'] == 'empty')
big = sorted(r['new'] for r in recs if r['status'] == 'copied-big')
fallback = sorted(r['new'] for r in recs if r['status'].startswith('fallback') or '!fallback' in r['kind'])
opaque = sorted(r['new'] for r in recs if r['kind'] in ('7z', 'rar'))
invalid = []
if a.truth:
    sd = os.path.join(a.truth, 'shards')
    for fn in sorted(os.listdir(sd)):
        if fn.startswith('p2-'):
            with gzip.open(os.path.join(sd, fn), 'rt', encoding='utf-8') as fi:
                for line in fi:
                    F = json.loads(line)
                    if not F['opens'] and F['rel'] in by_rel:
                        invalid.append((by_rel[F['rel']]['new'], F['kind'], F['open_err'].split('(')[0][:40]))   # error class only: messages can quote original names
invalid.sort()


def section(title, items, fmt=lambda x: x, limit=5000):
    out = [f'\n{title} ({len(items)})', '-' * (len(title) + 8)]
    out += ['  ' + fmt(x) for x in items[:limit]]
    if len(items) > limit:
        out.append(f'  ... and {len(items) - limit} more')
    return out


lines = f"""Machina CAD De-ID — {a.label}
Generated {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

WHAT THIS IS
corpus/ is a de-identified copy of the 188,722-file input. Every file is kept at the same relative path, except that
identifying spans of folder and file names are replaced; extensions are never changed. code/ holds the complete
pipeline that produced it.

HOW IT WORKS
1. Identity discovery (code/deid_eval/truth.py, rules in code/deid_eval/idents.py): every file is parsed
   format-aware (PDF metadata/XMP/text/attachments, OLE property sets for Inventor/Solid Edge/legacy SolidWorks,
   SolidWorks 2015+ deflate sections, FreeCAD/3MF/zip members, STEP/IGES/SAT/Parasolid/Creo headers, DXF/SVG/
   PostScript text, image EXIF/PNG text). Identities come from author/company/designer fields, user names in
   paths, e-mails, phone/QQ numbers, domains, repository owners and project names in folder names, and drawing
   labels such as "Designed by". Software vendors, standards, units, materials and generic accounts are excluded.
2. Pseudonyms (code/pipeline/pseudo.py): deterministic and format-preserving. Each word of an identity is mapped
   letter-for-letter (vowel->vowel, consonant->consonant, digit->digit, accents and scripts kept), seeded by the
   word itself, so the same identity gets the same pseudonym everywhere (full name, user name and camelCase/
   underscore forms agree) and every replacement has exactly the original byte length.
3. Rewriting (code/pipeline/process.py, engine.py): Aho-Corasick over raw bytes in UTF-8 and UTF-16LE.
   - SolidWorks 2015+: each section is inflated, rewritten, re-deflated and zero-padded to its original
     compressed size with its CRC32 updated, so no offsets move.
   - OLE natives, IGES, ACIS, Parasolid, Creo, DWG, STL headers: same-length in-place substitution.
   - PDF: objects, streams, Info and XMP, embedded files; names drawn as text the stream pass cannot see are
     redacted and re-written in place (line art and images untouched); full rewrite drops old revisions.
   - zip/FCStd/3MF, tar, gzip: unpacked, processed recursively (member names too), repacked in the same order.
   - PNG text chunks are rewritten with fixed CRCs.
   Files with nothing identifying are copied byte-for-byte. Rebuilt PDFs/archives/images are re-validated and
   fall back to same-length substitution (or an unchanged copy) if validation fails.
4. Folder and file names: the same substitution on path segments; references inside files change identically.

HOW TO RUN (Python 3.12, pip install -r code/requirements.txt)
  python3 -I code/deid.py truth --input <input>/ --out <private>/truth --workers 12
  python3 -I code/run_pipeline.py --input <input>/ --identities <private>/truth/identities.json \\
      --out <attempt>/corpus --log <private>/run.jsonl --workers 12
  python3 -I code/tools/make_attempt.py --repo code --attempt <attempt> --log <private>/run.jsonl --truth <private>/truth
Keep <private>/ (identity list, logs) outside the submission.

RUN SUMMARY
  files: {len(recs)}   renamed paths: {sum(r['renamed'] for r in recs)}   replacements: {sum(r['n'] for r in recs)}
  status: {dict(stats)}
  kinds: {dict(kinds.most_common(30))}

LIMITATIONS
- Identities are found by rules over structured fields and patterns; names that only appear as free prose, in
  raster images or inside logos may remain. Logos are not replaced.
- 7z and RAR archives are kept as-is apart from same-length substitution in their uncompressed headers.
- Files over 1.2 GB are copied unchanged (the grader skips files over 2 GB).
- Names inside compressed streams of formats we do not decode (e.g. some DWG/JT/CATIA payloads) may remain.
""".splitlines()
lines += section('SOURCE DEFECTS: empty input files (kept as empty files)', empty)
lines += section('SOURCE DEFECTS: input files that do not open as their format (kept, best-effort processed)', invalid,
                 fmt=lambda x: f'{x[0]}  [{x[1]}: {x[2]}]')
lines += section('OPAQUE ARCHIVES not unpacked (7z/rar)', opaque)
lines += section('LARGE FILES copied unchanged (> 1.2 GB)', big)
lines += section('FALLBACKS: rewrite failed validation, kept with same-length substitution or unchanged', fallback)
with open(os.path.join(a.attempt, 'README.txt'), 'w', encoding='utf-8') as fo:
    fo.write('\n'.join(lines) + '\n')
print('wrote', os.path.join(a.attempt, 'README.txt'), 'and code/ with', sum(len(f) for _, _, f in os.walk(code)), 'files')
