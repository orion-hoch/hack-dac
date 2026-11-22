"""Score an output corpus against a checks file, imitating the hidden scorer.

Prints the total and per-category passed / total / not-checked, and writes a private JSON
report listing every failed check with the reason (the real scorer never tells you this).
"""
import collections
import json
import os
import re
import sys
import time

from . import fmt, idents

PLACEHOLDER = idents.PLACEHOLDER


class Corpus:
    def __init__(self, root):
        self.root = root
        self.rels = set()
        self.index = collections.defaultdict(list)
        for dp, dn, fn in os.walk(root):
            for n in fn:
                rel = os.path.relpath(os.path.join(dp, n), root)
                self.rels.add(rel)
                self.index[(rel.count('/'), fmt.ext_full(n).lower())].append(rel)
        self._align = {}
        self._facts = {}
        self._text = {}
        self._lines = {}

    def regex(self, fr, groups=True):
        rel, spans = fr['rel'], sorted(fr['spans'])
        out, pos = [], 0
        for s, e in spans:
            if s < pos:
                continue
            out.append(re.escape(rel[pos:s])); out.append('([^/]*?)' if groups else '[^/]*?'); pos = e
        out.append(re.escape(rel[pos:]))
        return re.compile(''.join(out), re.S)

    def align(self, fr):
        rel = fr['rel']
        if rel in self._align:
            return self._align[rel]
        if rel in self.rels:
            self._align[rel] = rel
            return rel
        cands = self.index.get((rel.count('/'), fmt.ext_full(os.path.basename(rel)).lower()), [])
        rx = self.regex(fr)
        hits = [c for c in cands if rx.fullmatch(c)]
        if not hits:
            # extension may differ only in case
            rx2 = re.compile(rx.pattern, re.S | re.I)
            hits = [c for c in cands if rx2.fullmatch(c)]
        best = None
        if len(hits) == 1:
            best = hits[0]
        elif hits:
            size = fr['size'] or 1
            best = min(hits, key=lambda c: abs(os.path.getsize(os.path.join(self.root, c)) - size) / size)
        self._align[rel] = best
        return best

    def facts(self, out_rel):
        if out_rel not in self._facts:
            self._facts[out_rel] = fmt.analyze(os.path.join(self.root, out_rel), out_rel)
        return self._facts[out_rel]

    def text(self, out_rel):
        if out_rel not in self._text:
            F = self.facts(out_rel)
            self._text[out_rel] = idents.norm('\n'.join(t for _, _, t in F['bundle']))
        return self._text[out_rel]

    def line_hashes(self, out_rel):
        if out_rel not in self._lines:
            with open(os.path.join(self.root, out_rel), 'rb') as fh:
                self._lines[out_rel] = fmt.line_hashes(fh.read())
        return self._lines[out_rel]


def contains(text_norm, ident):
    for v in ident['variants']:
        if len(v) < 2:
            continue
        if ident['mode'] == 'sub' and len(v) >= 6:
            if v in text_norm:
                return True
        else:
            for m in re.finditer(re.escape(v), text_norm):
                a, b = m.start(), m.end()
                if (a == 0 or not text_norm[a - 1].isalnum()) and (b >= len(text_norm) or not text_norm[b].isalnum()):
                    return True
    return False


def capture_path(C, fr, span):
    out = C.align(fr)
    if not out:
        return None
    rx = C.regex(fr)
    m = rx.fullmatch(out) or re.compile(rx.pattern, re.I | re.S).fullmatch(out)
    if not m:
        return None
    spans = sorted(fr['spans'])
    try:
        gi = [list(x) for x in spans].index(list(span))
    except ValueError:
        return None
    return m.group(gi + 1)


def capture_slot(C, occ):
    out = C.align(occ['file'])
    if not out:
        return None, 'file not found'
    F = C.facts(out)
    if not F['opens']:
        return None, 'file does not open'
    cand = [s for s in F['slots'] if s[0] == occ['cat'] and s[1] == occ['field']]
    if not cand:
        return None, 'field missing in output'
    sl = next((s for s in cand if s[2] == occ['idx']), cand[0])
    val = sl[3]
    v, spans = occ['value'], sorted(occ['spans'])
    parts, pos, gi, target = [], 0, 0, None
    for s, e in spans:
        if s < pos:
            continue
        parts.append(re.escape(v[pos:s]).replace(r'\ ', r'\s*')); parts.append('(.*?)'); gi += 1
        if [s, e] == occ['span']:
            target = gi
        pos = e
    parts.append(re.escape(v[pos:]).replace(r'\ ', r'\s*'))
    m = re.fullmatch(''.join(parts), val, re.I | re.S)
    if not m or not target:
        return val, None  # structure changed: fall back to the whole value
    return m.group(target), None


def good_pseudo(p, ident):
    if p is None:
        return False
    p2 = p.strip()
    if not p2 or PLACEHOLDER.match(p2) or len(re.findall(r'[^\W\d_]', p2)) < 2:
        return False
    return idents.norm(p2) not in ident['variants'] and not contains(idents.norm(p2), ident)


def evaluate(C, c):
    t = c['type']
    if t in ('leak_path', 'leak_content', 'opens', 'geometry', 'engineering', 'native', 'meaningful'):
        out = C.align(c['file'])
        if not out:
            return 'not_checked', 'file not found'
    if t == 'leak_path':
        if c['ident']['mode'] == 'slot':
            p = capture_path(C, c['file'], c['span'])
            return ('pass', '') if p is not None and idents.norm(p) not in c['ident']['variants'] else ('fail', f'path still has {c["ident"]["id"]!r}')
        return ('fail', f'path still has {c["ident"]["id"]!r}') if contains(idents.norm(out), c['ident']) else ('pass', '')
    if t in ('leak_content', 'opens', 'geometry', 'engineering', 'native'):
        F = C.facts(out)
        if not F['opens']:
            return 'not_checked', 'does not open: ' + F['open_err'][:80]
    if t == 'leak_content':
        return ('fail', f'{c["layer"]} still has {c["ident"]["id"]!r}') if contains(C.text(out), c['ident']) else ('pass', '')
    if t == 'opens':
        exp = c['struct']
        return ('pass', '') if F['struct'] == exp else ('fail', f'structure {F["struct"]} != {exp}')
    if t == 'native':
        return ('pass', '') if F['kind'] == c['file']['kind'] and F['ext_full'].lower() == c['ext'].lower() else ('fail', f'kind {F["kind"]} != {c["file"]["kind"]}')
    if t == 'geometry':
        g = c.get('geom')
        if g:
            if g.startswith('img:') and (F['geom'] or '').startswith('img:'):
                a = bytes.fromhex(g[4:]); b = bytes.fromhex(F['geom'][4:])
                if len(a) != len(b) or sum(abs(x - y) for x, y in zip(a, b)) / max(1, len(a)) >= 3:
                    return 'fail', 'image pixels changed'
            elif F['geom'] != g:
                return 'fail', 'geometry fingerprint changed'
        if c['free_chunks']:
            have = collections.Counter(hh for _, hh in F['chunks'])
            need = collections.Counter(c['free_chunks'])
            missing = sum((need - have).values())
            if missing:
                return 'fail', f'{missing} identifier-free chunks changed'
        if c['line_sample']:
            hs = C.line_hashes(out)
            got = sum(1 for x in c['line_sample'] if x in hs)
            if got < 0.99 * len(c['line_sample']):
                return 'fail', f'only {got}/{len(c["line_sample"])} identifier-free lines kept'
        return 'pass', ''
    if t == 'engineering':
        if c['field'] == 'page1_token':
            return ('pass', '') if idents.norm(c['value']) in C.text(out) else ('fail', f'missing token {c["value"]!r}')
        ok = any(e[1] == c['field'] and e[2] == c['value'] for e in F['eng'])
        return ('pass', '') if ok else ('fail', f'{c["field"]} != {c["value"]!r}')
    if t == 'reference':
        src, tgt = C.align(c['file']), C.align(c['target'])
        if not src or not tgt:
            return 'not_checked', 'source or target not found'
        F = C.facts(src)
        if not F['opens']:
            return 'not_checked', 'source does not open'
        want = os.path.basename(tgt).lower()
        bases = {r.replace('\\', '/').split('/')[-1].strip().lower() for _, r in F['refs']}
        return ('pass', '') if want in bases else ('fail', f'no reference to {want!r}')
    if t == 'duplicate':
        x, y = C.align(c['file']), C.align(c['other'])
        if not x or not y:
            return 'not_checked', 'duplicate not found'
        return ('pass', '') if C.facts(x)['sha1'] == C.facts(y)['sha1'] else ('fail', 'duplicates differ in output')
    if t in ('consistency', 'distinct'):
        def pseudo(occ, ident):
            if occ['where'] == 'path':
                return capture_path(C, occ['file'], occ['span'])
            p, err = capture_slot(C, occ)
            return p
        if t == 'consistency':
            ps = [pseudo(o, c['ident']) for o in c['occ']]
            if any(p is None for p in ps):
                return 'fail', 'pseudonym not recoverable at some occurrence'
            if not all(good_pseudo(p, c['ident']) for p in ps):
                return 'fail', 'identity not replaced at some occurrence'
            ns = {re.sub(r'[\s_.\-]', '', idents.norm(p)) for p in ps}
            return ('pass', '') if len(ns) == 1 else ('fail', f'{len(ns)} different pseudonyms')
        a = pseudo(c['a']['occ'], c['a']['ident']); b = pseudo(c['b']['occ'], c['b']['ident'])
        if not good_pseudo(a, c['a']['ident']) or not good_pseudo(b, c['b']['ident']):
            return 'fail', 'identity not replaced'
        sq = lambda x: re.sub(r'[\s_.\-]', '', idents.norm(x))
        return ('pass', '') if sq(a) != sq(b) else ('fail', 'two identities share a pseudonym')
    if t == 'meaningful':
        p, err = capture_slot(C, {**c, 'span': c['spans'][0], 'where': 'slot'})
        if err:
            return 'fail', err
        return ('pass', '') if good_pseudo(p, c['ident']) else ('fail', f'not a meaningful pseudonym: {p!r}')
    return 'fail', 'unknown check type'


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='deid score')
    ap.add_argument('--checks', required=True)
    ap.add_argument('--corpus', required=True, help='output corpus/ to score')
    ap.add_argument('--report', default='', help='private JSON report with every failure')
    a = ap.parse_args(argv)
    t0 = time.time()
    checks = [json.loads(l) for l in open(a.checks, encoding='utf-8')]
    C = Corpus(a.corpus)
    res = collections.defaultdict(collections.Counter)
    fails = []
    for c in checks:
        try:
            st, why = evaluate(C, c)
        except Exception as e:
            st, why = 'not_checked', 'scorer error: ' + repr(e)[:120]
        res[c['category']][st] += 1
        if st != 'pass':
            fails.append({'n': c['n'], 'category': c['category'], 'status': st, 'why': why,
                          'file': (c.get('file') or c.get('occ', [{}])[0].get('file') or c.get('a', {}).get('occ', {}).get('file', {})).get('rel')})
    total = sum(r['pass'] for r in res.values())
    order = sorted(res, key=lambda k: (not k.startswith('names'), not k.startswith(('consistency', 'meaningful')), k))
    print(f'\nLOCAL SCORE: {total} / {len(checks)}   ({time.time() - t0:.0f}s, {len(C.rels)} output files)')
    print(f'  {"category":26s} {"passed":>7s} {"total":>6s} {"not chk":>8s}')
    for k in order:
        r = res[k]
        print(f'  {k:26s} {r["pass"]:7d} {sum(r.values()):6d} {r["not_checked"]:8d}')
    if a.report:
        with open(a.report, 'w', encoding='utf-8') as fo:
            json.dump({'score': total, 'categories': {k: dict(v) for k, v in res.items()}, 'failures': fails}, fo, ensure_ascii=False, indent=1)
        print(f'  failures -> {a.report}')
    return total


if __name__ == '__main__':
    main(sys.argv[1:])
