"""Fast, resumable uploader for an attempt folder (same object layout as the organisers' upload.py).

- smallest files first (the scorer's 12 GB budget means it mostly checks small files)
- identical files are uploaded once; the other copies are made with GCS server-side copy
- skips objects already present with the same size+MD5; deletes nothing unless --prune
- --loop: keeps rescanning (the pipeline may still be writing) until <attempt>/.final exists and a pass uploads nothing
- token is re-read from --token-file whenever the server says it expired (drop a fresh token in that file)

Usage: python3 tools/uploader.py --attempt ~/machina/attempts/attempt-1 \
         --dest gs://builders-machina-submissions-1120/teams/<team>/attempt-1/ --token-file ~/machina/private/token.txt
"""
import argparse
import base64
import collections
import hashlib
import http.client
import os
import re
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HOST = 'storage.googleapis.com'
_local = threading.local()
_lock = threading.Lock()


class Tok:
    def __init__(self, path):
        self.path = Path(path)
        self.value = self.path.read_text().strip()

    def renew(self, stale):
        with _lock:
            if self.value != stale:
                return self.value
            print('[uploader] token expired: waiting for a new one in', self.path, flush=True)
            deadline = time.time() + 3 * 3600
            while time.time() < deadline:
                v = self.path.read_text().strip()
                if v and v != stale:
                    self.value = v
                    print('[uploader] got new token', flush=True)
                    return v
                time.sleep(10)
            raise SystemExit('no new token')


IFACE = os.environ.get('UPLOAD_IFACE', '')


class BoundHTTPS(http.client.HTTPSConnection):
    """HTTPS connection pinned to one network interface (SO_BINDTODEVICE), e.g. a faster Wi-Fi uplink."""
    def connect(self):
        if not IFACE:
            return super().connect()
        import socket
        fam, typ, proto, _, addr = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)[0]
        s = socket.socket(fam, typ, proto)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, IFACE.encode())
        s.settimeout(self.timeout)
        s.connect(addr)
        self.sock = self._context.wrap_socket(s, server_hostname=self.host)


def conn():
    c = getattr(_local, 'c', None)
    if c is None:
        c = _local.c = BoundHTTPS(HOST, timeout=300)
    return c


def reset():
    c = getattr(_local, 'c', None)
    if c:
        try:
            c.close()
        except Exception:
            pass
    _local.c = None


def expired(status, body):
    return status == 401 or (status == 403 and b'xpired' in body)


def request(tok, method, path, headers=None, body_path=None):
    for attempt in range(8):
        t = tok.value
        h = {'Authorization': f'Bearer {t}'}
        h.update(headers or {})
        try:
            c = conn()
            if body_path is not None:
                size = os.path.getsize(body_path)
                c.putrequest(method, path)
                for k, v in {**h, 'Content-Length': str(size), 'Content-Type': 'application/octet-stream'}.items():
                    c.putheader(k, v)
                c.endheaders()
                with open(body_path, 'rb') as f:
                    while True:
                        chunk = f.read(1 << 20)
                        if not chunk:
                            break
                        c.send(chunk)
            else:
                c.request(method, path, headers=h)
            r = c.getresponse()
            body = r.read()
            if expired(r.status, body):
                tok.renew(t)
                continue
            if r.status >= 500 or r.status == 429:
                reset(); time.sleep(min(30, 0.5 * 2 ** attempt)); continue
            return r.status, body
        except (OSError, http.client.HTTPException):
            reset(); time.sleep(min(30, 0.5 * 2 ** attempt))
    raise RuntimeError(f'{method} {path} failed')


def list_remote(tok, bucket, prefix):
    out, ct = {}, None
    while True:
        q = {'list-type': '2', 'prefix': prefix, 'max-keys': '1000'}
        if ct:
            q['continuation-token'] = ct
        st, body = request(tok, 'GET', f'/{bucket}?' + urllib.parse.urlencode(q))
        if st != 200:
            raise SystemExit(f'list failed {st}: {body[:300]!r}')
        txt = body.decode('utf-8')
        for m in re.finditer(r'<Contents>(.*?)</Contents>', txt, re.S):
            blk = m.group(1)
            k = re.search(r'<Key>(.*?)</Key>', blk).group(1)
            k = k.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"').replace('&apos;', "'")
            size = int(re.search(r'<Size>(\d+)</Size>', blk).group(1))
            etag = re.search(r'<ETag>"?([^<"]*)"?</ETag>', blk).group(1).replace('&quot;', '')
            out[k] = (size, etag)
        m = re.search(r'<NextContinuationToken>(.*?)</NextContinuationToken>', txt)
        ct = m.group(1) if m else None
        if not ct:
            return out


def md5_of(p):
    h = hashlib.md5()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 22), b''):
            h.update(chunk)
    return h.hexdigest()


def local_files(attempt):
    items = []
    for sub in ('corpus', 'code'):
        root = os.path.join(attempt, sub)
        for dp, dn, fn in os.walk(root):
            for n in fn:
                p = os.path.join(dp, n)
                items.append((p, sub + '/' + os.path.relpath(p, root).replace(os.sep, '/')))
    rd = os.path.join(attempt, 'README.txt')
    if os.path.isfile(rd):
        items.append((rd, 'README.txt'))
    return items


def one_pass(a, tok, bucket, prefix):
    t0 = time.time()
    remote = list_remote(tok, bucket, prefix)
    items = local_files(a.attempt)
    md5cache = {}
    todo = []
    for p, rel in items:
        key = prefix + rel
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        r = remote.get(key)
        if r and r[0] == size and len(r[1]) == 32:
            m = md5_of(p); md5cache[p] = m
            if m == r[1]:
                continue
        todo.append((size, p, key))
    if not todo:
        return 0, 0
    # group identical content: upload one, server-side copy the rest
    groups = collections.defaultdict(list)
    for size, p, key in todo:
        m = md5cache.get(p) or md5_of(p)
        groups[(size, m)].append((p, key))
    reps = sorted(groups.items(), key=lambda kv: kv[0][0])
    # if an identical object already exists remotely, copy from it instead of uploading
    remote_by = {}
    for k, (s, e) in remote.items():
        if len(e) == 32:
            remote_by.setdefault((s, e), k)
    n_up = n_cp = 0
    sent = 0
    print(f'[uploader] pass: {len(todo)} files to send ({sum(s for s, _, _ in todo) / 1e9:.2f} GB), '
          f'{len(reps)} unique, remote has {len(remote)}', flush=True)

    def do_group(item):
        (size, m), members = item
        src = remote_by.get((size, m))
        done = []
        for p, key in members:
            if src is None:
                st, body = request(tok, 'PUT', f'/{bucket}/' + urllib.parse.quote(key), headers={'Content-MD5': base64.b64encode(bytes.fromhex(m)).decode()}, body_path=p)
                if not 200 <= st < 300:
                    raise RuntimeError(f'PUT {key}: {st} {body[:200]!r}')
                src = key; done.append(('up', size))
            else:
                st, body = request(tok, 'PUT', f'/{bucket}/' + urllib.parse.quote(key),
                                   headers={'x-goog-copy-source': f'/{bucket}/' + urllib.parse.quote(src), 'Content-Length': '0'})
                if not 200 <= st < 300:
                    raise RuntimeError(f'COPY {key}: {st} {body[:200]!r}')
                done.append(('cp', size))
        return done

    with ThreadPoolExecutor(a.workers) as ex:
        futs = [ex.submit(do_group, it) for it in reps]
        last = time.time()
        for i, f in enumerate(as_completed(futs)):
            for kind, size in f.result():
                if kind == 'up':
                    n_up += 1; sent += size
                else:
                    n_cp += 1
            if time.time() - last > 30:
                el = time.time() - t0
                print(f'[uploader] {i + 1}/{len(reps)} groups, uploaded {n_up} ({sent / 1e9:.2f} GB, {sent / 1e6 / el:.1f} MB/s), copied {n_cp}', flush=True)
                last = time.time()
    el = time.time() - t0
    print(f'[uploader] pass done: uploaded {n_up} ({sent / 1e9:.2f} GB, {sent / 1e6 / max(el, 1):.1f} MB/s), server-side copies {n_cp}, {el:.0f}s', flush=True)
    return n_up, n_cp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--attempt', required=True)
    ap.add_argument('--dest', required=True)
    ap.add_argument('--token-file', required=True)
    ap.add_argument('--workers', type=int, default=64)
    ap.add_argument('--loop', action='store_true')
    a = ap.parse_args()
    assert a.dest.startswith('gs://') and a.dest.endswith('/')
    bucket, prefix = a.dest[5:].split('/', 1)
    tok = Tok(a.token_file)
    while True:
        up, cp = one_pass(a, tok, bucket, prefix)
        final = os.path.exists(os.path.join(a.attempt, '.final'))
        if not a.loop or (final and up == 0 and cp == 0):
            break
        if up == 0 and cp == 0:
            time.sleep(60)
    print(f'[uploader] COMPLETE. Submit: {a.dest}', flush=True)


if __name__ == '__main__':
    main()
