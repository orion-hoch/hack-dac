"""Delete files in an attempt corpus that the latest pipeline run did not produce (stale names from interim runs).
Usage: python -I tools/prune_stale.py <corpus> <run.jsonl> [--list-only]   Prints the stale paths it removed."""
import json
import os
import sys

corpus, log = sys.argv[1], sys.argv[2]
keep = {json.loads(l)['new'] for l in open(log, encoding='utf-8')}
stale = []
for dp, dn, fn in os.walk(corpus):
    for n in fn:
        rel = os.path.relpath(os.path.join(dp, n), corpus)
        if rel not in keep:
            stale.append(rel)
for rel in stale:
    print('stale', rel)
    if '--list-only' not in sys.argv:
        os.remove(os.path.join(corpus, rel))
print(f'stale files: {len(stale)}' + (' (listed only)' if '--list-only' in sys.argv else ' (removed)'))
