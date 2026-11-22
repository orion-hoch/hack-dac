#!/usr/bin/env python3
"""pipeline_v2 runner (integrator's code, MIT; replaces Andre's pool loop, keeps his rename and engine).

  venv/bin/python -I code/run_v2.py --input IN --out OUT/corpus --log PRIVATE/run.jsonl --workers 8 \
      --identities PRIVATE/identities_clean.json --scoped PRIVATE/identities_scoped.json

Properties: resumable (records in --log whose output exists are skipped); files of --big bytes or more go to their
own lane of one worker; each worker has an address-space cap, so a runaway file fails alone; a worker that dies or
exceeds --timeout costs only its own file, which goes out unchanged under its new name and is listed; the pool
continues. Names are planned for every file before anything is written, and any collision aborts the run.
"""
import argparse
import collections
import json
import multiprocessing as mp
import multiprocessing.connection as mpc
import os
import resource
import shutil
import sys
import time

os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('OMP_NUM_THREADS', '1')
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from pipeline import v2  # noqa: E402

S = None
LISTED = ('kept', 'gate-failed', 'error', 'worker-died', 'timeout', 'copied-big')


def list_files(root):
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if not x.startswith('.get-input'))
        for f in sorted(files):
            out.append(os.path.relpath(os.path.join(d, f), root))
    return out


def _write(dst, data):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + '.v2tmp'
    with open(tmp, 'wb') as fh:
        fh.write(data)
    os.replace(tmp, dst)


def worker(conn, inp, out, mem_gb, max_bytes):
    if mem_gb:
        lim = int(mem_gb * 2**30)
        resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
    while True:
        task = conn.recv()
        if task is None:
            return
        rel, new = task
        t = time.time()
        src, dst = os.path.join(inp, rel), os.path.join(out, new)
        size = os.path.getsize(src)
        rec = {'rel': rel, 'new': new, 'size': size}
        try:
            if size > max_bytes:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
                rec.update(kind='big', route='big', status='copied-big', n=0, notes=['KEPT over size cap'])
            else:
                with open(src, 'rb') as fh:
                    data = fh.read()
                res, r = v2.process(S, rel, data)
                del data
                _write(dst, res)
                rec.update(r)
        except MemoryError:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            rec.update(status='error', n=0, notes=['ERROR MemoryError: original emitted'])
        rec['s'] = round(time.time() - t, 3)
        conn.send(rec)


def main(argv=None):
    global S
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--log', required=True)
    ap.add_argument('--identities', required=True)
    ap.add_argument('--scoped', default='')
    ap.add_argument('--stop', default=os.path.join(HERE, 'data', 'stoplist.tsv'))
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--files', default='')
    ap.add_argument('--big', type=float, default=200e6)
    ap.add_argument('--max-bytes', type=float, default=1.5e9)
    ap.add_argument('--mem-gb', type=float, default=6.0)      # measured: a 179 MB STEP needs more than 3 GB of address space
    ap.add_argument('--big-mem-gb', type=float, default=12.0)
    ap.add_argument('--timeout', type=float, default=1800)
    ap.add_argument('--tier', default='A', help="A (default) or all")
    a = ap.parse_args(argv)
    if a.tier == 'all':
        os.environ['DEID_TIER'] = 'all'
    t0 = time.time()
    S = v2.Setup(a.identities, a.scoped, a.stop)
    files = [l.rstrip('\n') for l in open(a.files, encoding='utf-8') if l.strip()] if a.files else list_files(a.input)
    plan = {rel: v2.rename(S, rel) for rel in files}
    seen = {}
    for rel, new in plan.items():
        k = new.lower()
        if k in seen:
            sys.exit('COLLISION: two inputs map to one output name; refusing to run (see --log).')
        seen[k] = rel
    done = set()
    if os.path.exists(a.log):
        for line in open(a.log, encoding='utf-8'):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get('status') not in ('worker-died', 'timeout') and os.path.exists(os.path.join(a.out, r['new'])):
                done.add(r['rel'])
    todo = [f for f in files if f not in done]
    sizes = {f: os.path.getsize(os.path.join(a.input, f)) for f in todo}
    big = sorted((f for f in todo if sizes[f] >= a.big), key=lambda f: sizes[f])
    small = [f for f in todo if sizes[f] < a.big]
    print(f'[v2] setup {time.time() - t0:.0f}s: {len(files)} files, {len(done)} done, {len(small)} small, '
          f'{len(big)} big; workers {a.workers}', flush=True)
    os.makedirs(a.out, exist_ok=True)
    ctx = mp.get_context('fork')
    queues = {'small': collections.deque(small), 'big': collections.deque(big)}
    nsmall = max(1, a.workers - (1 if big else 0))
    slots = [('small', a.mem_gb)] * nsmall + ([('big', a.big_mem_gb)] if big else [])
    live = {}                                # conn -> [proc, lane, mem, task, t_start]
    stats = collections.Counter()
    lg = open(a.log, 'a', encoding='utf-8')

    def spawn(lane, mem):
        pc, cc = ctx.Pipe()
        p = ctx.Process(target=worker, args=(cc, a.input, a.out, mem, a.max_bytes), daemon=True)
        p.start()
        cc.close()
        live[pc] = [p, lane, mem, None, 0.0]
        feed(pc)

    def feed(pc):
        p, lane, mem, _, _ = live[pc]
        q = queues[lane]
        if q:
            rel = q.popleft()
            live[pc][3], live[pc][4] = rel, time.time()
            pc.send((rel, plan[rel]))
        else:
            pc.send(None)
            live[pc][3] = None

    def record(rec):
        stats[rec['status']] += 1
        stats['n'] += rec.get('n', 0)
        lg.write(json.dumps(rec, ensure_ascii=False) + '\n')
        if sum(stats[k] for k in stats if k != 'n') % 2000 == 0:
            lg.flush()
            print(f'[v2] {sum(v for k, v in stats.items() if k != "n")}/{len(todo)} {time.time() - t0:.0f}s '
                  f'{dict(stats)}', flush=True)

    def fail(pc, why):
        p, lane, mem, rel, _ = live.pop(pc)
        if rel is not None:
            src, dst = os.path.join(a.input, rel), os.path.join(a.out, plan[rel])
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            record({'rel': rel, 'new': plan[rel], 'size': sizes.get(rel), 'status': why, 'n': 0,
                    'notes': ['KEPT %s: original emitted' % why]})
        if queues[lane]:
            spawn(lane, mem)

    for lane, mem in slots:
        spawn(lane, mem)
    while live:
        conns = list(live)
        ready = mpc.wait(conns + [live[c][0].sentinel for c in conns], timeout=30)
        for c in conns:
            if c not in live:
                continue
            p = live[c][0]
            if c in ready:
                try:
                    rec = c.recv()
                except (EOFError, OSError):
                    p.join(1)
                    fail(c, 'worker-died')
                    continue
                record(rec)
                feed(c)
                if live[c][3] is None:
                    p.join(5)
                    live.pop(c)
            elif p.sentinel in ready and not p.is_alive():
                fail(c, 'worker-died')
            elif live[c][3] is not None and time.time() - live[c][4] > a.timeout:
                p.kill()                     # our own child, exact PID
                p.join(5)
                fail(c, 'timeout')
    lg.close()
    summ = {'files': len(files), 'skipped_done': len(done), 'seconds': round(time.time() - t0), **stats}
    json.dump(summ, open(a.log + '.summary.json', 'w'), indent=1)
    print(f'[v2] done {json.dumps(summ)}', flush=True)


if __name__ == '__main__':
    main()
