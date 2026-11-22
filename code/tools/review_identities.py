"""Print a review of the identity dictionary (PRIVATE output: contains real identifiers; never commit it).

Usage: python -I tools/review_identities.py <truth_dir>/identities.json
Flags likely false positives: identities seen in very many files, plain lowercase words, engineering vocabulary.
"""
import collections
import json
import re
import sys

ENG = set('''part parts assembly asm drawing drw sheet scale material steel aluminium aluminum brass copper plastic rubber
bolt nut screw washer bearing shaft gear motor plate bracket housing cover base frame mount holder spacer pin spring
default config configuration body sketch extrude feature front top right left back bottom view section detail
standard iso din ansi asme metric inch mm cm units unit mass weight volume density finish surface tolerance revision
title date name description design designer drawn checked approved project number qty quantity item total sheetmetal
model models solid solidworks inventor freecad step stp iges stl pdf dxf dwg file files folder template templates
plane axis origin point line circle arc spline hole thread chamfer fillet pattern mirror shell rib draft cut boss
temp tmp users user documents desktop downloads appdata local roaming program data system windows library'''.split())

ids = json.load(open(sys.argv[1], encoding='utf-8'))
print('identities:', len(ids))
print('by kind:', collections.Counter(r['kind'] for r in ids.values()))
print('by mode:', collections.Counter(r['mode'] for r in ids.values()))
print('by source:', collections.Counter(k for r in ids.values() for k in r['src']))
print('\nTOP 60 by file count')
for n, r in sorted(ids.items(), key=lambda kv: -kv[1]['files'])[:60]:
    print(f"  {r['files']:6d} {r['kind']:8s} {r['mode']:5s} {n[:50]!r:52s} src={r['src']} forms={r['forms'][:3]}")
print('\nSUSPECT: engineering vocabulary / plain words')
for n, r in sorted(ids.items(), key=lambda kv: -kv[1]['files']):
    words = re.findall(r'[a-z]+', n)
    if words and all(w in ENG for w in words) and r['mode'] != 'slot':
        print(f"  {r['files']:6d} {r['kind']:8s} {r['mode']:5s} {n!r} src={r['src']}")
print('\nSUSPECT: many files from text only (likely generic)')
for n, r in sorted(ids.items(), key=lambda kv: -kv[1]['files'])[:400]:
    if r['src'].get('text', 0) > 300 and not r['src'].get('slot') and not r['src'].get('path'):
        print(f"  {r['files']:6d} {r['kind']:8s} {r['mode']:5s} {n!r} src={r['src']}")
