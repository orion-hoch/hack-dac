"""Cross-machine parity (repository layout: vm/ beside code/).
  parity.py make  INPUT_ROOT PRIVATE_DIR LIST50   (Spark) -> PRIVATE_DIR/parity_standins.json, parity_50.json, and
                                                            vm/parity/vectors_public.tsv (public test key only)
  parity.py check INPUT_ROOT PRIVATE_DIR          (VM)    -> exit 0 and PRIVATE_DIR/PARITY_OK only if the public test
                                                            vectors, every stand-in under the real key, and every one of
                                                            the 50 outputs (bytes and new path) are equal
  parity.py sig   INPUT_ROOT PRIVATE_DIR                  -> prints the signature of this code + table + key"""
import hashlib, json, os, subprocess, sys
VM = os.path.dirname(os.path.abspath(__file__)); CODE = os.path.join(os.path.dirname(VM), 'code')
sys.path.insert(0, CODE)
mode, root, priv = sys.argv[1], sys.argv[2], sys.argv[3]
ids = os.path.join(priv, 'identities_v3.json'); paths = os.path.join(priv, 'identities_v3_paths.json')
EXTRA = ['Jane Doering', 'ÅSA Öberg', 'José Núñez', 'Ærøskøbing', 'Łódź', 'Привет Мир', '東京大学', 'Straße', 'İstanbul',
         'ǅemal', 'ﬁle', 'ｆｕｌｌｗｉｄｔｈ', 'C:\\Users\\name\\Desktop', 'www.example.de', 'a.b@c.com', 'JDOERI~1']


def sig():
    h = hashlib.sha256()
    for d, ds, fs in os.walk(CODE):
        ds[:] = sorted(x for x in ds if x not in ('__pycache__', 'fonts'))     # the font is installed by setup.sh, checked by sha256
        for f in sorted(fs):
            if not f.endswith('.pyc'):
                h.update(os.path.relpath(os.path.join(d, f), CODE).encode()); h.update(open(os.path.join(d, f), 'rb').read())
    for p in (ids, paths):
        h.update(open(p, 'rb').read())
    h.update(hashlib.sha256(open(os.environ['DEID_PSEUDO_KEY_FILE'], 'rb').read().strip()).digest())
    return h.hexdigest()


if mode == 'sig':
    print(sig()); sys.exit(0)
from pipeline import v2
from pipeline.pseudo import pseudo
S = v2.Setup(ids, paths)


def standins():
    forms = sorted(S.E.ids)[::max(1, len(S.E.ids) // 400)]
    return [(s, pseudo(s)) for s in forms + EXTRA]


def _norm_hash(out):
    """PDFs are compared with every stream decompressed (zlib builds differ between machines: 1.3 vs 1.2.13 gave other
    deflate bytes for identical content); everything else byte for byte."""
    if out[:5] == b'%PDF-':
        import io, pikepdf
        try:
            with pikepdf.open(io.BytesIO(out)) as p:
                buf = io.BytesIO()
                p.save(buf, compress_streams=False, stream_decode_level=pikepdf.StreamDecodeLevel.generalized,
                       object_stream_mode=pikepdf.ObjectStreamMode.disable, deterministic_id=True, static_id=True)
                return 'pdf:' + hashlib.sha256(buf.getvalue()).hexdigest()
        except Exception:
            pass
    return hashlib.sha256(out).hexdigest()


def outputs(rels):
    rows = []
    for rel in rels:
        out, r = v2.process(S, rel, open(os.path.join(root, rel), 'rb').read())
        rows.append((rel, v2.rename(S, rel), _norm_hash(out), r['status']))
    return rows


pub = subprocess.run([sys.executable, '-I', os.path.join(CODE, 'pipeline', 'pseudo.py'), 'vectors'], capture_output=True, text=True).stdout
if mode == 'make':
    rels = [l.strip() for l in open(sys.argv[4]) if l.strip()]
    os.makedirs(os.path.join(VM, 'parity'), exist_ok=True)
    open(os.path.join(VM, 'parity', 'vectors_public.tsv'), 'w', encoding='utf-8').write(pub)
    json.dump(standins(), open(os.path.join(priv, 'parity_standins.json'), 'w', encoding='utf-8'), ensure_ascii=False)
    json.dump(outputs(rels), open(os.path.join(priv, 'parity_50.json'), 'w', encoding='utf-8'), ensure_ascii=False)
    print('parity made: %d stand-ins, %d files, signature %s' % (len(standins()), len(rels), sig()[:12]))
else:
    fails = []
    if pub != open(os.path.join(VM, 'parity', 'vectors_public.tsv'), encoding='utf-8').read():
        fails.append('public test vectors differ')
    want = [tuple(x) for x in json.load(open(os.path.join(priv, 'parity_standins.json'), encoding='utf-8'))]
    got = standins()
    if len(want) != len(got):
        fails.append('stand-in list length %d vs %d (table differs)' % (len(want), len(got)))
    fails += ['stand-in differs for entry %d' % i for i, (a, b) in enumerate(zip(want, got)) if a != b][:5]
    w50 = [tuple(x) for x in json.load(open(os.path.join(priv, 'parity_50.json'), encoding='utf-8'))]
    g50 = outputs([x[0] for x in w50])
    fails += ['output differs for file %d (%s)' % (i, os.path.splitext(a[0])[1]) for i, (a, b) in enumerate(zip(w50, g50)) if a != b]
    print(json.dumps({'fails': fails, 'standins': len(got), 'files': len(g50)}))
    if fails:
        sys.exit(1)
    open(os.path.join(priv, 'PARITY_OK'), 'w').write(sig() + '\n')
    print('PARITY OK', sig()[:12])
