#!/usr/bin/env bash
# Full run on the VM. Refuses to start unless: every input file has its manifest size, and parity_check.sh passed for
# exactly this code, table and key. WORKERS (default: cores - 2) counts all workers, one of them for files >= 200 MB.
#   ATTEMPT=attempt-2 WORKERS=28 bash run.sh        (resumable: run it again after any stop)
set -euo pipefail
B=$(cd "$(dirname "$0")" && pwd); . ${DEID_HOME:-$HOME/deid}/env.sh
A=${ATTEMPT:?set ATTEMPT}; W=${WORKERS:-$(( $(nproc) - 2 ))}; O=$DEID_HOME/out/$A; L=$DEID_HOME/logs/$A; mkdir -p $O $L
$DEID_HOME/venv/bin/python -I $B/verify_sizes.py $B/manifest.tsv $DEID_HOME/input
S=$(cd $B && $DEID_HOME/venv/bin/python -I -c "import sys; sys.argv=['parity.py','sig','.','$DEID_HOME/private']; exec(open('parity.py').read().split('pub = subprocess')[0]); print(sig())")
[ "$(cat $DEID_HOME/private/PARITY_OK 2>/dev/null)" = "$S" ] || { echo "parity not verified for this code/table/key: run parity_check.sh"; exit 1; }
nice -n 5 /usr/bin/time -v $DEID_HOME/venv/bin/python -I $B/code/run_v2.py --input $DEID_HOME/input --out $O/corpus \
  --log $L/run.jsonl --identities $DEID_HOME/private/identities_v3.json --workers $W >> $L/run.out 2>&1 &
RUN=$!; bash $B/memwatch.sh $RUN >> $L/memwatch.log 2>&1 & wait $RUN
$DEID_HOME/venv/bin/python -I $B/submit.py make $O $L/run.jsonl
$DEID_HOME/venv/bin/python -I $B/submit.py preflight $O $L/run.jsonl $DEID_HOME/private/identities_v3.json $DEID_HOME/input | tee $L/preflight.json
touch $O/PREFLIGHT_OK; echo "ready to upload: ATTEMPT=$A bash upload.sh"
