#!/usr/bin/env python3
"""Drop identities made only of descriptive words (council D11 as amended 2026-11-21: in paths replace identity words
only; never rename descriptive words of a collection title, or the scorer cannot find the file).
  python -I filter_ids.py IN.json OUT.json DECISIONS.tsv [SCOPED_IN.json SCOPED_OUT.json]
A project or company identity of two or more tokens is dropped when every token is an English dictionary word (plural-aware), a number,
a standard designation (d638, m3, 6061) or a standards body. Single-token words were already handled upstream.
Domains switch from sub to word mode, so 'x.com' never matches inside 'yx.com'."""
import json, re, sys
WORDS = set()
for f in ('american-english', 'british-english'):
    try:
        WORDS |= {w.strip() for w in open('/usr/share/dict/' + f, encoding='utf-8', errors='ignore') if w.strip().islower()}
    except OSError:
        pass
NS_HOSTS = ('w3.org', 'web.resource.org', 'purl.org', 'creativecommons.org', 'inkscape.org', 'sodipodi.sourceforge.net',
            'ns.adobe.com', 'schemas.microsoft.com', 'iptc.org', 'xmlns.com', 'openoffice.org', 'freecad.org', 'ietf.org')
ARCH = {'amd64', 'x8664', 'win64', 'win32', 'i386', 'i686', 'arm64', 'aarch64', 'intel64', 'em64t', 'ia64'}
STD = {'astm', 'iso', 'din', 'ansi', 'iec', 'jis', 'bs', 'en', 'sae', 'ieee', 'nist', 'asme', 'mil', 'std', 'nema'}


def word(t):
    return t in WORDS or (t.endswith('s') and t[:-1] in WORDS) or (t.endswith('es') and t[:-2] in WORDS)


def descriptive(k):
    toks = re.findall(r'[^\W_]+', k.lower())
    if len(toks) < 2:
        return False
    return all(t.isdigit() or t in STD or re.fullmatch(r'[a-z]{1,2}\d{1,5}', t) or word(t) for t in toks)


if __name__ == '__main__':
    ids = json.load(open(sys.argv[1], encoding='utf-8'))
    keep, drop = {}, []
    for k, r in ids.items():
        al = re.sub(r'[\W_]', '', k)
        if len(al) < 4 or al.isdigit() or len(set(al.lower())) == 1:
            drop.append(k); continue                # digits only, one character repeated, or under 4 characters
        r = dict(r, forms=[f for f in r.get('forms', []) if len(re.sub(r'[\W_]', '', f)) >= 4
                           and not re.sub(r'[\W_]', '', f).isdigit() and len(set(re.sub(r'[\W_]', '', f).lower())) > 1])
        if al.lower() in ARCH:
            drop.append(k); continue                # machine types (Parasolid MC=AMD64, Windows hosts)
        if any(k == h or k.endswith('.' + h) for h in NS_HOSTS):
            drop.append(k); continue                # XML namespace and standards hosts are format text, not identities
        if r.get('kind') == 'domain' and r.get('mode') == 'sub':
            r = dict(r, mode='word')          # a domain is a whole token: never inside a longer host name
        (drop.append(k) if r.get('kind') in ('project', 'company') and descriptive(k) else keep.__setitem__(k, r))
    json.dump(keep, open(sys.argv[2], 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
    if len(sys.argv) > 5:                           # SCOPED_IN SCOPED_OUT: folder-name identities (path/file modes)
        sc, out = json.load(open(sys.argv[4], encoding='utf-8')), {}
        for k, r in sc.items():
            al = re.sub(r'[\W_]', '', k)
            toks = re.findall(r'[^\W_]+', k.lower())
            if r.get('mode') not in ('path', 'file') or len(al) < 3 or al.isdigit() or len(set(al.lower())) == 1:
                continue
            if all(word(t) or t.isdigit() for t in toks):
                drop.append(k); continue            # descriptive words of a collection title stay (D11 as amended)
            out[k] = r
        json.dump(out, open(sys.argv[5], 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
        print('folder-name identities kept %d' % len(out))
    open(sys.argv[3], 'w', encoding='utf-8').write(''.join('drop_descriptive\t%s\n' % k for k in sorted(drop)))
    print('kept %d dropped %d' % (len(keep), len(drop)))
