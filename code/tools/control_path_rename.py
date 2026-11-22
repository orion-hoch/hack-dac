"""Positive control for the local scorer: rename only the identifying path spans (hardlinks, no copies).

Usage: python -I tools/control_path_rename.py <truth_dir> <input_root> <out_corpus>
Expected: names.paths and path-based consistency pass; content leaks fail; usability unchanged.
"""
import gzip
import hashlib
import json
import os
import sys

truth, inp, out = sys.argv[1:4]
sd = os.path.join(truth, 'shards')
n = 0
for fn in sorted(os.listdir(sd)):
    if not fn.startswith('p2-'):
        continue
    with gzip.open(os.path.join(sd, fn), 'rt', encoding='utf-8') as fi:
        for line in fi:
            F = json.loads(line)
            rel = F['rel']
            new, pos = [], 0
            for s, e, iid in sorted(F['path_occ']):
                if s < pos:
                    continue
                new.append(rel[pos:s])
                new.append('px' + hashlib.sha1(iid.encode()).hexdigest()[:6])
                pos = e
            new.append(rel[pos:])
            dst = os.path.join(out, ''.join(new))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.exists(dst):
                os.link(os.path.join(inp, rel), dst)
            n += 1
print('linked', n, 'files into', out)
