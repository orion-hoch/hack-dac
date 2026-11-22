"""Sample a fixed set of checks (default 1,000) from the truth.

Output is private: check records embed original identifiers.
"""
import collections
import gzip
import json
import os
import random
import sys

from . import fmt, idents

NAMES = [('names.paths', 170), ('names.pdf_metadata', 55), ('names.visible_text', 65), ('names.native_properties', 140),
         ('names.cad_headers', 80), ('names.archives', 50), ('consistency', 90), ('meaningful', 58)]
USABLE = [('usable.opens', 90), ('usable.geometry', 70), ('usable.engineering', 60), ('usable.references', 35),
          ('usable.native_format', 20), ('usable.duplicates', 17)]
ID_FIELD = ('author', 'last_saved_by', 'manager', 'company', 'designer', 'engineer', 'checked', 'approved', 'creator', 'lastmodifiedby',
            'createdby', 'organization', 'drawn', 'swprop:', 'dc:creator', 'ps:%%for', 'exif:artist', 'xpauthor', 'username')


def group_of(rel):
    p = rel.split('/')
    return '/'.join(p[:3]) if p[0] == 'sources' else p[0]


def fileref(F):
    return {'rel': F['rel'], 'spans': [[s, e] for s, e, _ in F['path_occ']], 'size': F['size'], 'kind': F['kind']}


def stratified(cands, n, rng, key):
    buckets = collections.defaultdict(list)
    for c in cands:
        buckets[key(c)].append(c)
    for b in buckets.values():
        rng.shuffle(b)
    order = list(buckets)
    rng.shuffle(order)
    out = []
    while len(out) < n and order:
        nxt = []
        for k in order:
            if buckets[k]:
                out.append(buckets[k].pop())
                if len(out) >= n:
                    break
            if buckets[k]:
                nxt.append(k)
        order = nxt
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='deid checks')
    ap.add_argument('--truth', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--seed', type=int, default=1120)
    ap.add_argument('--total', type=int, default=1000)
    a = ap.parse_args(argv)
    rng = random.Random(a.seed)
    ids = json.load(open(os.path.join(a.truth, 'identities.json'), encoding='utf-8'))
    files = {}
    sd = os.path.join(a.truth, 'shards')
    for fn in sorted(os.listdir(sd)):
        if not fn.startswith('p2-'):
            continue
        with gzip.open(os.path.join(sd, fn), 'rt', encoding='utf-8') as fi:
            for line in fi:
                F = json.loads(line)
                files[F['rel']] = F
    files = dict(sorted(files.items()))

    def idrec(iid):
        r = ids[iid]
        return {'id': iid, 'kind': r['kind'], 'mode': r['mode'], 'variants': sorted(idents.variants(iid))}

    pools = collections.defaultdict(list)
    occ_by_id = collections.defaultdict(list)          # for consistency
    for rel, F in files.items():
        fr = fileref(F)
        for s, e, iid in F['path_occ']:
            if iid in ids:
                pools['names.paths'].append({'type': 'leak_path', 'file': fr, 'ident': idrec(iid), 'span': [s, e], '_g': group_of(rel), '_k': iid})
                occ_by_id[iid].append({'where': 'path', 'file': fr, 'span': [s, e]})
        if F['opens']:
            for iid, cats in F['occ'].items():
                if iid not in ids or ids[iid]['mode'] == 'slot':
                    continue
                for c in cats:
                    if c.startswith('slot:'):
                        continue
                    pools['names.' + c].append({'type': 'leak_content', 'file': fr, 'ident': idrec(iid), 'layer': c, '_g': F['kind'], '_k': iid})
        for cat, field, idx, v, kind, sp in F['slot_occ']:
            for s, e, iid in sp:
                if iid in ids:
                    occ_by_id[iid].append({'where': 'slot', 'file': fr, 'cat': cat, 'field': field, 'idx': idx, 'value': v,
                                           'spans': [[x[0], x[1]] for x in sp], 'span': [s, e]})
            if any(t in field.lower() for t in ID_FIELD) and sp:
                cover = sum(e - s for s, e, _ in sp)
                if cover >= 0.5 * len(v.strip()):
                    pools['meaningful'].append({'type': 'meaningful', 'file': fr, 'cat': cat, 'field': field, 'idx': idx, 'value': v,
                                                'spans': [[x[0], x[1]] for x in sp], 'ident': idrec(sp[0][2]), '_g': F['kind'], '_k': sp[0][2]})
        if F['opens'] and F['kind'] != 'empty':
            pools['usable.opens'].append({'type': 'opens', 'file': fr, 'struct': F['struct'], '_g': F['kind'], '_k': rel})
            pools['usable.native_format'].append({'type': 'native', 'file': fr, 'ext': F['ext_full'], '_g': F['kind'], '_k': rel})
            img_ok = F['kind'] != 'image' or not F['occ']
            if (F['geom'] and img_ok) or F['free_chunks'] or F['line_sample']:
                pools['usable.geometry'].append({'type': 'geometry', 'file': fr, 'geom': F['geom'] if img_ok else None,
                                                 'free_chunks': F['free_chunks'][:400], 'line_sample': F['line_sample'],
                                                 '_g': F['kind'], '_k': rel})
            if F['eng']:
                cat, field, v = rng.choice(F['eng'])
                pools['usable.engineering'].append({'type': 'engineering', 'file': fr, 'cat': cat, 'field': field, 'value': v, '_g': F['kind'], '_k': rel})
    # references: resolve basenames within the same repo/group folder
    by_base = collections.defaultdict(list)
    for rel in files:
        by_base[(group_of(rel), os.path.basename(rel).lower())].append(rel)
    for rel, F in files.items():
        seen = set()
        for cat, ref in F['refs']:
            base = ref.replace('\\', '/').split('/')[-1].strip().lower()
            tg = by_base.get((group_of(rel), base), [])
            if len(tg) == 1 and tg[0] != rel and base not in seen:
                seen.add(base)
                pools['usable.references'].append({'type': 'reference', 'file': fileref(F), 'target': fileref(files[tg[0]]),
                                                   'ref': ref, '_g': F['kind'], '_k': rel})
    # duplicates
    by_sha = collections.defaultdict(list)
    for rel, F in files.items():
        if F['kind'] != 'empty' and F['sha1'] and not F['sha1'].startswith('big:'):
            by_sha[F['sha1']].append(rel)
    for sha, rels in sorted(by_sha.items()):
        if len(rels) >= 2:
            x, y = rng.sample(rels, 2)
            pools['usable.duplicates'].append({'type': 'duplicate', 'file': fileref(files[x]), 'other': fileref(files[y]), '_g': group_of(x), '_k': sha})
    # consistency (same) and distinct
    multi = {iid: o for iid, o in occ_by_id.items() if len({x['file']['rel'] for x in o}) >= 2 or len(o) >= 2}
    for iid, o in sorted(multi.items()):
        rng.shuffle(o)
        pick, seenf = [], set()
        for x in o:
            key = (x['file']['rel'], x['where'], x.get('field'))
            if key not in seenf:
                seenf.add(key); pick.append(x)
            if len(pick) >= 4:
                break
        if len(pick) >= 2:
            pools['consistency'].append({'type': 'consistency', 'ident': idrec(iid), 'occ': pick, '_g': ids[iid]['kind'], '_k': iid})
    kinds = collections.defaultdict(list)
    for iid in sorted(occ_by_id):
        kinds[ids[iid]['kind']].append(iid)
    distinct = []
    for k, lst in sorted(kinds.items()):
        for _ in range(min(40, len(lst) // 2)):
            p, q = rng.sample(lst, 2)
            if p in q or q in p:
                continue
            distinct.append({'type': 'distinct', 'a': {'ident': idrec(p), 'occ': rng.choice(occ_by_id[p])},
                             'b': {'ident': idrec(q), 'occ': rng.choice(occ_by_id[q])}, '_g': k, '_k': p + '|' + q})

    def take(group, total):
        quotas = dict(group)
        avail = {c: len(pools[c]) + (len(distinct) if c == 'consistency' else 0) for c in quotas}
        scale = total / sum(quotas.values())
        want = {c: round(q * scale) for c, q in quotas.items()}
        # redistribute deficits within the group
        for _ in range(5):
            deficit = sum(max(0, want[c] - avail[c]) for c in want)
            if not deficit:
                break
            for c in want:
                want[c] = min(want[c], avail[c])
            room = {c: avail[c] - want[c] for c in want if avail[c] > want[c]}
            tot = sum(room.values())
            if not tot:
                break
            for c in room:
                want[c] += min(room[c], round(deficit * room[c] / tot))
        out = []
        for c, n in want.items():
            if c == 'consistency':
                nd = min(len(distinct), round(n * 20 / 90))
                chosen = stratified(pools[c], n - nd, rng, lambda x: x['_g']) + stratified(distinct, nd, rng, lambda x: x['_g'])
            else:
                # one check per identity / file first, then fill
                chosen = stratified(pools[c], n, rng, lambda x: (x['_g'], int(fmt.h(x['_k'])[:6], 16) % 7))
            for x in chosen:
                x['category'] = c
            out += chosen
        return out

    n_names = round(a.total * 0.708)
    checks = take(NAMES, n_names) + take(USABLE, a.total - n_names)
    for i, c in enumerate(checks):
        c['n'] = i
        c.pop('_g', None); c.pop('_k', None)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, 'w', encoding='utf-8') as fo:
        for c in checks:
            fo.write(json.dumps(c, ensure_ascii=False) + '\n')
    cnt = collections.Counter(c['category'] for c in checks)
    print(f'[checks] {len(checks)} checks -> {a.out}')
    for k in [x for x, _ in NAMES + USABLE]:
        print(f'  {k:26s} {cnt.get(k, 0):4d}   (pool {len(pools[k]) + (len(distinct) if k == "consistency" else 0)})')


if __name__ == '__main__':
    main(sys.argv[1:])
