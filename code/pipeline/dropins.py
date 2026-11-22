"""Drop-in fixes for the teammate's pipeline (code/pipeline). Written by the other half of the team; nothing of his is edited.
Each function is self-contained. Tested on 89 corpus files (see COMPARE.md, section 6). MIT, like the rest of code/.

1. filter_identities(ids)       -> drops format keywords, keeps English-word identities to path context only
2. patch_engine(EngineClass)    -> 'slot' identities in file CONTENT match only inside a path (\\x\\ or /x/), never
                                   between spaces/newlines/quotes (that hit DXF hex handles, Creo tokens, PDF names)
3. keyed pseudo()               -> HMAC with a secret kept outside code/ + tokenisation by the identity key, so
                                   'PartsHub'/'traceparts' and 'AlexMorgan'/'Alex Morgan' agree
4. do_creo(engine, data)        -> section-aware Creo rewrite: LZW sections decoded, both TOC checksums rebuilt
5. do_dwg(engine, data)         -> DWG R2004+ rewrite inside decompressed sections, page checksums and object CRCs fixed

Wiring (process.py):  elif kind == 'creo': out, n = dropins.do_creo(engine, data)
                      elif kind == 'dwg':  out, n = dropins.do_dwg(engine, data)
creo.py and dwg_inplace.py come from ~/hack-osu (prior/native/creo.py, work/gaps/dwg_inplace.py); copy them next to
this file. Both are our own code, standard library only.
"""
import hashlib, hmac, os, re, unicodedata

# ----------------------------------------------------------------------------------------------- 1. dictionary
FORMAT_WORDS = set('''type space all and first master spec meta license terminator noname it hi di fp yp
model paper page pages font catalog string name value data part body solid face edge line point layer block text
color style default standard metric inch unit units material config version date time title author company
description comment file path root node object document sheet view drawing assembly length width height size
left right top bottom front back center origin plane axis sketch feature extrude cut hole fillet chamfer pattern
mirror shell rib draft revolve sweep loft thread mate group array list table index count number total true false
none null empty default normal custom user users admin public private shared temp tmp test new old copy final draft
release work home desktop documents downloads library libraries resources support engineering design designs
project projects hardware mechanical electronics cad models parts tools source sources master main dev develop'''.split())


def _english(path='/usr/share/dict/words'):
    try:
        # lower-case entries only: capitalised entries are proper nouns (Dana, Morgan), which stay identities
        return {w.strip() for w in open(path, encoding='utf-8', errors='ignore') if w.strip() and w.strip() == w.strip().lower()}
    except OSError:
        return set()


def filter_identities(ids, english=None):
    """ids: the teammate's identities.json dict. Returns (new_ids, report). Never adds identities."""
    english = _english() if english is None else english
    out, rep = {}, {'dropped_format_word': [], 'to_path_only': []}
    for iid, rec in ids.items():
        words = re.findall(r'[^\W_]+', iid)
        if iid in FORMAT_WORDS or (len(words) == 1 and words[0] in FORMAT_WORDS):
            rep['dropped_format_word'].append(iid); continue
        rec = dict(rec)
        hexlike = len(iid) <= 4 and re.fullmatch(r'[0-9a-f]+', iid)
        single_english = len(words) == 1 and words[0] in english and rec.get('kind') not in ('domain', 'contact')
        if (hexlike or single_english) and rec.get('mode') != 'slot':
            rec['mode'] = 'slot'; rep['to_path_only'].append(iid)
        out[iid] = rec
    return out, rep


# ----------------------------------------------------------------------------------------------- 2. engine
_ALNUM = re.compile(r'[0-9a-z]')
_SEP = set('/\\:"\'<>|\x00 \t\r\n')


def patch_engine(Engine):
    """Monkey-patch Engine._ok_boundary: in content, a 'slot' identity must sit inside a path segment."""
    orig = Engine._ok_boundary

    def _ok(self, low, s, e, enc, mode, path=False):
        if mode != 'slot':
            return orig(self, low, s, e, enc, mode, path)
        w = 2 if enc == 'utf-16le' else 1
        b1 = low[s - w] if s >= w else ''
        a1 = low[e] if e < len(low) else ''
        if path:
            # rename() passes folder part and stem separately: a slot id must be a whole segment or a whole stem
            # prefix up to '.', exactly what the content rule below can also see (FAB-OX0005.idw stays FAB-...)
            return b1 in ('', '/', '\\') and a1 in ('', '/', '\\', '.')
        # content: C:\Users\name\ , \\host\share , name.DOMAIN , /home/name/ ; never between spaces/newlines
        if b1 == '\\':
            return a1 == '' or a1 in _SEP or a1 == '.'
        if b1 == '/':
            return a1 in ('', '/', '\\', '.')
        return False
    Engine._ok_boundary = _ok
    return Engine


# ----------------------------------------------------------------------------------------------- 3. pseudonyms
VOW, CON = 'aeiou', 'bcdfghjklmnprstvz'
CYR_VOW, CYR_CON = 'аеиоуя', 'бвгдклмнпрстфх'
CJK_POOL = '安白陈德方高韩何黄江金康李林刘罗马孟宁潘钱秦任宋孙唐田王吴肖谢徐许杨叶于张赵郑周朱文明华建国平志海军伟强磊洋勇艳杰涛超秀英'
TOKEN = re.compile(r'[^\W_]+')


def _secret():
    v = os.environ.get('DEID_SECRET')
    if not v:
        p = os.environ.get('DEID_SECRET_FILE', os.path.expanduser('~/machina/private/pseudo.key'))
        v = open(p, encoding='utf-8').read().strip()          # fail loudly: never run keyless
    return v.encode('utf-8')


_KEY = None


def norm(s):
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return ' '.join(s.lower().split())


def _rs(word, n):
    global _KEY
    if _KEY is None:
        _KEY = _secret()
    out, i = [], 0
    while len(out) < n:
        out += hmac.new(_KEY, f'{word}|{i}'.encode('utf-8'), hashlib.sha256).digest(); i += 1
    return out[:n]


def _map(ch, r):
    """Same per-character rule as the teammate's _token (class, case, accents, byte width kept)."""
    d = unicodedata.normalize('NFD', ch)
    base = ''.join(c for c in d if not unicodedata.combining(c)); marks = d[len(base):] if d.startswith(base) else ''
    lb = base.lower()
    if len(lb) == 1 and 'a' <= lb <= 'z':
        pool = VOW if lb in VOW else CON
        nb = pool[r % len(pool)]
        nb = pool[(r + 1) % len(pool)] if nb == lb else nb
        nb = nb.upper() if base.isupper() else nb
        new = unicodedata.normalize('NFC', nb + marks) if marks else nb
        return new if len(new.encode('utf-8')) == len(ch.encode('utf-8')) else (ch if marks else nb)
    if ch.isdigit() and ch.isascii():
        nd = str(r % 10); return str((r + 1) % 10) if nd == ch else nd
    if '\u4e00' <= ch <= '\u9fff':
        return CJK_POOL[r % len(CJK_POOL)]
    if '\u0430' <= ch.lower() <= '\u044f':
        pool = CYR_VOW if ch.lower() in CYR_VOW else CYR_CON
        nc = pool[r % len(pool)]; return nc.upper() if ch.isupper() else nc
    return ch


def _word(tok, seed):
    rs = _rs(seed, len(tok))
    return ''.join(_map(c, r) for c, r in zip(tok, rs))


def pseudo(surface, key=None):
    """Keyed, length-preserving. With key (the identity id the engine already passes), word boundaries come from the
    key, not from the surface's case, so every case and separator variant of one identity agrees."""
    if key:
        words = TOKEN.findall(norm(key))
        pos = [m.start() + j for m in TOKEN.finditer(surface) for j in range(m.end() - m.start())]
        flat = ''.join(surface[i] for i in pos)
        if words and norm(flat) == ''.join(words) and len(flat) == len(''.join(words)):
            out, k = list(surface), 0
            for w in words:
                rs = _rs(w, len(w))
                for j in range(len(w)):
                    out[pos[k]] = _map(surface[pos[k]], rs[j]); k += 1
            res = ''.join(out)
            if norm(res) != norm(surface):
                return res
    res = TOKEN.sub(lambda m: _word(m.group(), norm(m.group())), surface)
    if norm(res) == norm(surface):
        res = TOKEN.sub(lambda m: _word(m.group(), norm(m.group()) + 'x'), surface)
    return res


# ----------------------------------------------------------------------------------------------- 4. Creo
def do_creo(engine, data):
    from . import creo
    info = creo.parse(data)
    edits, n = {}, 0
    for s in info['secs']:
        b = creo.body(data, s)
        if b[:2] == b'\x1f\x9d':
            try:
                u, _ = creo.unlzw(b)
            except Exception:
                continue                      # 23% of LZW-magic sections do not decode: leave them
            if len(u) != s['unp']:
                continue
        else:
            u = b
        new, k = engine.sub_bytes(u, same_len=True)
        if k:
            edits[s['name']] = new; n += k
    head = data[:info['base']]
    nh, k = engine.sub_bytes(head, same_len=True)
    n += k
    if not n:
        return data, 0
    out = creo.rebuild(data, edits, [(head, nh)] if k else None)
    secs, ok, nl, lok, bad = creo.verify(out)
    s0, ok0, _, _, _ = creo.verify(data)
    if secs != s0 or ok < ok0:
        raise ValueError('creo rebuild failed verification: %r' % bad[:3])
    return out, n


# ----------------------------------------------------------------------------------------------- 5. DWG
def _shifts(buf):
    """The buffer read at each of the 8 bit offsets (DWG strings in object streams are not byte-aligned)."""
    x = int.from_bytes(buf, 'big'); L = len(buf)
    for k in range(8):
        yield k, ((x << k) & ((1 << (8 * L)) - 1)).to_bytes(L, 'big')


def do_dwg(engine, data):
    from . import dwg_inplace
    S = dwg_inplace.load_sections(bytes(data))
    pairs = {}
    for s in S.values():
        for k, view in _shifts(bytes(s['buf'])):
            for st, e, iid, enc in engine.find(view):
                old = view[st:e]
                try:
                    new = pseudo(old.decode(enc), iid).encode(enc)
                except UnicodeDecodeError:
                    continue
                if len(new) == len(old) and new != old:
                    pairs[old] = new
    if not pairs:
        return data, 0
    out, st = dwg_inplace.patch(bytes(data), list(pairs.items()))
    a0, a1 = dwg_inplace.audit(bytearray(data)), dwg_inplace.audit(bytearray(out))
    if a1['pages_ok'] < a0['pages_ok'] or a1['objects'] < a0['objects'] or a1['obj_bad'] != a0['obj_bad']:
        raise ValueError('dwg patch failed audit %r' % ((a0, a1),))
    return out, st['hits']
