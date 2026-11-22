"""Remove pass-1 records of given kinds from truth shards so `truth --append` re-parses them with newer code.
Usage: python -I tools/drop_from_shards.py <truth_dir> <kind> [<kind> ...]"""
import gzip
import json
import os
import sys

truth, kinds = sys.argv[1], set(sys.argv[2:])
sd = os.path.join(truth, 'shards')
kept = dropped = 0
for fn in sorted(os.listdir(sd)):
    if not fn.startswith('p1-'):
        continue
    src = os.path.join(sd, fn)
    tmp = src + '.tmp'
    with gzip.open(src, 'rt', encoding='utf-8') as fi, open(tmp, 'wb') as fo:
        for line in fi:
            if json.loads(line).get('kind') in kinds:
                dropped += 1
                continue
            fo.write(gzip.compress(line.encode('utf-8'), compresslevel=1)); kept += 1
    os.replace(tmp, src)
for fn in os.listdir(sd):
    if fn.startswith('p2-'):
        os.remove(os.path.join(sd, fn))
print(f'kept {kept}, dropped {dropped} records of kinds {sorted(kinds)}')
