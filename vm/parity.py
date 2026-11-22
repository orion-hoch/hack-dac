"""Cross-machine parity. make (on the Spark) / check (on the VM).
  parity.py make  INPUT_ROOT LIST50 OUTDIR_PRIVATE      -> parity_standins.tsv, parity_50.tsv (private: real key)
  parity.py check INPUT_ROOT PRIVATE_DIR                -> exit 0 and PRIVATE_DIR/PARITY_OK only if every stand-in and
                                                           every one of the 50 outputs (bytes and new path) is equal
Also compares the public test vectors of pseudo.py (TEST_KEY) with the bundled parity/vectors_public.tsv."""
import hashlib, json, os, subprocess, sys
B = os.path.dirname(os.path.abspath(__file__)); CODE = os.path.join(B, 'code')
sys.path.insert(0, CODE)
from pipeline import v2
from pipeline.pseudo import pseudo
mode, root = sys.argv[1], sys.argv[2]
priv = sys.argv[4] if mode == 'make' else sys.argv[3]
ids = os.path.join(priv, 'identities_v3.json')
S = v2.Setup(ids)
EXTRA = ['Jane Doering', 'ÅSA Öberg', 'José Núñez', 'Ærøskøbing', 'Łódź', 'Привет Мир', '東京大学', 'Straße', 'İstanbul',
         'ǅemal', 'ﬁle', 'ｆｕｌｌｗｉｄｔｈ', 'C:\\Users\\name\\Desktop', 'www.example.de', 'a.b@c.com', 'JDOERI~1']


def sig():
    h = hashlib.sha256()
    for d, _ds, fs in sorted(os.walk(CODE)):
        for f in sorted(fs):
            if f.endswith('.pyc'):
                continue
            h.update(open(os.path.join(d, f), 'rb').read())
    h.update(open(ids, 'rb').read())
    h.update(hashlib.sha256(open(os.environ['DEID_PSEUDO_KEY_FILE'], 'rb').read().strip()).digest())
    return h.hexdigest()


def standins():
    forms = sorted(S.E.ids)[::max(1, len(S.E.ids) // 400)]
    return [(s, pseudo(s)) for s in forms + EXTRA]


def outputs(rels):
    rows = []
    for rel in rels:
        data = open(os.path.join(root, rel), 'rb').read()
        out, r = v2.process(S, rel, data)
        rows.append((rel, v2.rename(S, rel), hashlib.sha256(out).hexdigest(), r['status']))
    return rows


pub = subprocess.run([sys.executable, '-I', os.path.join(CODE, 'pipeline', 'pseudo.py'), 'vectors'],
                     capture_output=True, text=True).stdout
if mode == 'make':
    rels = [l.strip() for l in open(sys.argv[3]) if l.strip()]
    os.makedirs(os.path.join(B, 'parity'), exist_ok=True)
    open(os.path.join(B, 'parity', 'vectors_public.tsv'), 'w', encoding='utf-8').write(pub)
    json.dump(standins(), open(os.path.join(priv, 'parity_standins.json'), 'w', encoding='utf-8'), ensure_ascii=False)
    json.dump(outputs(rels), open(os.path.join(priv, 'parity_50.json'), 'w', encoding='utf-8'), ensure_ascii=False)
    print('parity made: %d stand-ins, %d files, signature %s' % (len(standins()), len(rels), sig()[:12]))
else:
    fails = []
    if pub != open(os.path.join(B, 'parity', 'vectors_public.tsv'), encoding='utf-8').read():
        fails.append('public test vectors differ')
    want = [tuple(x) for x in json.load(open(os.path.join(priv, 'parity_standins.json'), encoding='utf-8'))]
    got = standins()
    fails += ['stand-in differs for entry %d' % i for i, (a, b) in enumerate(zip(want, got)) if a != b][:5]
    if len(want) != len(got):
        fails.append('stand-in list length %d vs %d (table differs)' % (len(want), len(got)))
    w50 = [tuple(x) for x in json.load(open(os.path.join(priv, 'parity_50.json'), encoding='utf-8'))]
    g50 = outputs([x[0] for x in w50])
    fails += ['output differs for file %d (%s)' % (i, os.path.splitext(a[0])[1]) for i, (a, b) in enumerate(zip(w50, g50)) if a != b]
    print(json.dumps({'fails': fails, 'standins': len(got), 'files': len(g50)}))
    if fails:
        sys.exit(1)
    open(os.path.join(priv, 'PARITY_OK'), 'w').write(sig() + '\n')
    print('PARITY OK', sig()[:12])
