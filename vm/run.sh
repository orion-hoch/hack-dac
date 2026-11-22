#!/usr/bin/env bash
# Full run on the VM. Refuses to start unless every input has its manifest size and parity_check.sh passed for this
# exact code, table and key. WORKERS (default cores - 2) counts all workers, one of them for files of 200 MB or more.
#   ATTEMPT=attempt-3 WORKERS=28 bash vm/run.sh        (resumable)
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd); . ${DEID_HOME:-$HOME/deid}/env.sh
A=${ATTEMPT:?set ATTEMPT}; W=${WORKERS:-$(( $(nproc) - 2 ))}; O=$DEID_HOME/out/$A; L=$DEID_HOME/logs/$A; mkdir -p $O $L
PV=$DEID_HOME/private
$DEID_HOME/venv/bin/python -I $R/vm/verify_sizes.py $DEID_HOME/manifest.tsv $DEID_HOME/input
S=$($DEID_HOME/venv/bin/python -I $R/vm/parity.py sig $DEID_HOME/input $PV)
[ "$(cat $PV/PARITY_OK 2>/dev/null)" = "$S" ] || { echo "parity not verified for this code/table/key: run vm/parity_check.sh"; exit 1; }
nice -n 5 /usr/bin/time -v $DEID_HOME/venv/bin/python -I $R/code/run_v2.py --input $DEID_HOME/input --out $O/corpus \
  --log $L/run.jsonl --identities $PV/identities_v3.json --scoped $PV/identities_v3_paths.json --workers $W >> $L/run.out 2>&1 &
RUN=$!; bash $R/memwatch.sh $RUN >> $L/memwatch.log 2>&1 & wait $RUN
grep -c "OCR fresh reads" $L/run.jsonl > $L/ocr_fresh_files.txt || true
$DEID_HOME/venv/bin/python -I $R/submit.py make $O $L/run.jsonl
$DEID_HOME/venv/bin/python -I $R/submit.py preflight $O $L/run.jsonl $PV/identities_v3.json $DEID_HOME/input | tee $L/preflight.json
touch $O/PREFLIGHT_OK; echo "ready to upload: ATTEMPT=$A bash vm/upload.sh"
