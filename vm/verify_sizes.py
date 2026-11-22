"""verify_sizes.py MANIFEST ROOT [--todo]: every manifest file present with its exact size. --todo prints the
manifest rows still to fetch (missing or short; files that are too long are deleted so they restart)."""
import os, sys
man, root, todo = sys.argv[1], sys.argv[2], '--todo' in sys.argv
ok = bad = 0
for line in open(man, encoding='utf-8'):
    if line.startswith('#') or not line.strip():
        continue
    f = line.rstrip('\n').split('\t')
    p = os.path.join(root, f[0]); want = int(f[1] or 0)
    have = os.path.getsize(p) if os.path.exists(p) else -1
    if have == want:
        ok += 1; continue
    bad += 1
    if todo:
        if have > want:
            os.remove(p)
        sys.stdout.write(line)
if not todo:
    print('files ok %d, missing or wrong size %d' % (ok, bad))
sys.exit(1 if bad else 0)
