#!/usr/bin/env python3
"""Local scorer CLI.  python3 -I deid.py {truth|checks|score|preflight} --help"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CMDS = {'truth': 'deid_eval.truth', 'checks': 'deid_eval.checks', 'score': 'deid_eval.score', 'preflight': 'deid_eval.preflight'}

if __name__ == '__main__':
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        print(__doc__); sys.exit(2)
    import importlib
    mod = importlib.import_module(CMDS[sys.argv[1]])
    r = mod.main(sys.argv[2:])
    sys.exit(r if isinstance(r, int) and sys.argv[1] == 'preflight' else 0)
