"""Submission-level rules: object count, file sizes, required files, stray private files."""
import os
import re
import sys

SUSPECT = re.compile(r'(?i)(mapping|pseudonym|identit(y|ies)|\.tmp$|~\$|\.log$|\.pyc$|^\.ds_store$|\.swp$|credentials|\.env$)')


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='deid preflight')
    ap.add_argument('--attempt', required=True, help='folder holding corpus/, code/, README.txt')
    ap.add_argument('--input', default='', help='raw input root, to compare file counts and extensions')
    a = ap.parse_args(argv)
    corpus = os.path.join(a.attempt, 'corpus')
    problems, n, big, sus = [], 0, [], []
    exts_out = {}
    for dp, dn, fn in os.walk(corpus):
        for f in fn:
            n += 1
            p = os.path.join(dp, f)
            if os.path.getsize(p) > 2 * 1024 ** 3:
                big.append(os.path.relpath(p, corpus))
            if SUSPECT.search(f):
                sus.append(os.path.relpath(p, corpus))
            e = os.path.splitext(f)[1].lower()
            exts_out[e] = exts_out.get(e, 0) + 1
    if not os.path.isdir(corpus) or n == 0:
        problems.append('corpus/ missing or empty (submission would be rejected)')
    if n > 400_000:
        problems.append(f'corpus/ has {n} objects > 400,000 (rejected)')
    if not os.path.isfile(os.path.join(a.attempt, 'README.txt')):
        problems.append('README.txt missing')
    if not os.path.isdir(os.path.join(a.attempt, 'code')):
        problems.append('code/ missing')
    print(f'corpus objects: {n}')
    if big:
        print(f'files over 2 GB (skipped by grader): {len(big)}', *big[:10], sep='\n  ')
    if sus:
        print(f'suspicious names in corpus/ (mappings/temp/logs?): {len(sus)}', *sus[:20], sep='\n  ')
    if a.input:
        ni, exts_in = 0, {}
        for dp, dn, fn in os.walk(a.input):
            dn[:] = [d for d in dn if not d.startswith('.get-input')]
            for f in fn:
                ni += 1
                e = os.path.splitext(f)[1].lower()
                exts_in[e] = exts_in.get(e, 0) + 1
        print(f'input files: {ni}  output files: {n}  difference: {n - ni}')
        diff = {e: exts_out.get(e, 0) - c for e, c in exts_in.items() if exts_out.get(e, 0) != c}
        if diff:
            print('extension count changes (input -> output):', dict(sorted(diff.items(), key=lambda x: x[1])[:20]))
    for p in problems:
        print('PROBLEM:', p)
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
