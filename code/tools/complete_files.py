"""List input files whose size already matches the manifest (download complete).
Usage: python -I tools/complete_files.py <input_root> <manifest.tsv> <out.txt>"""
import os
import sys

root, man, out = sys.argv[1:4]
n = 0
with open(man, encoding='utf-8') as fi, open(out, 'w', encoding='utf-8') as fo:
    for line in fi:
        if line.startswith('#'):
            continue
        p, size = line.split('\t')[:2]
        try:
            if os.path.getsize(os.path.join(root, p)) == int(size):
                fo.write(p + '\n'); n += 1
        except OSError:
            pass
print('complete files:', n)
