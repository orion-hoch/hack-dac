"""List (and optionally delete) objects in our attempt folder that are not part of the final local attempt.

Interim uploads can leave objects under paths the final dictionary renamed differently. Uses the same
1-hour upload token as upload.py and only touches our own team's attempt prefix.

Usage: MACHINA_UPLOAD_TOKEN=... python3 tools/remote_stale.py <gs://bucket/teams/<team>/attempt-N/> <local attempt dir> [--delete]
"""
import http.client
import os
import sys
import urllib.parse
import xml.etree.ElementTree as ET

dest, local = sys.argv[1], sys.argv[2]
delete = '--delete' in sys.argv
token = os.environ['MACHINA_UPLOAD_TOKEN'].strip()
assert dest.startswith('gs://') and dest.endswith('/')
bucket, prefix = dest[5:].split('/', 1)

want = set()
for sub in ('corpus', 'code'):
    root = os.path.join(local, sub)
    for dp, dn, fn in os.walk(root):
        for n in fn:
            want.add(prefix + sub + '/' + os.path.relpath(os.path.join(dp, n), root).replace(os.sep, '/'))
want.add(prefix + 'README.txt')

conn = http.client.HTTPSConnection('storage.googleapis.com', timeout=120)
remote, token_c = [], None
while True:
    q = {'list-type': '2', 'prefix': prefix, 'max-keys': '1000'}
    if token_c:
        q['continuation-token'] = token_c
    conn.request('GET', f'/{bucket}?' + urllib.parse.urlencode(q), headers={'Authorization': f'Bearer {token}'})
    r = conn.getresponse(); body = r.read()
    if r.status != 200:
        sys.exit(f'list failed {r.status}: {body[:300]!r}')
    root = ET.fromstring(body)
    ns = root.tag.split('}')[0] + '}' if root.tag.startswith('{') else ''
    remote += [c.findtext(f'{ns}Key') for c in root.findall(f'{ns}Contents')]
    token_c = root.findtext(f'{ns}NextContinuationToken')
    if not token_c:
        break
stale = [k for k in remote if k not in want]
missing = len(want - set(remote))
print(f'remote objects: {len(remote)}  local files: {len(want)}  stale remote: {len(stale)}  not yet uploaded: {missing}')
for k in stale[:50]:
    print('  stale', k[len(prefix):])
if delete and stale:
    bad = 0
    for k in stale:
        conn.request('DELETE', f'/{bucket}/' + urllib.parse.quote(k, safe='/'), headers={'Authorization': f'Bearer {token}'})
        r = conn.getresponse(); r.read()
        if r.status not in (200, 204):
            bad += 1
    print(f'deleted {len(stale) - bad}, failed {bad}')
