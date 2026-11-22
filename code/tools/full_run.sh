#!/usr/bin/env bash
# Full dry run on the Spark: finish download -> truth -> checks -> pipeline -> attempt folder -> local score.
# Usage: bash tools/full_run.sh <attempt-name>      (run from the repo/code dir; ~/machina layout assumed)
set -uo pipefail
NAME="${1:-attempt-1}"
M=~/machina; P=$M/.venv/bin/python; T=$M/private/truth; A=$M/attempts/$NAME; L=$M/private/$NAME
mkdir -p "$T" "$L" "$M/attempts" "$M/tmp"
export DEID_TMP=$M/tmp   # private temp dir for OLE rewrites (identity-bearing data never goes to /tmp)
log() { echo "[full_run $(date +%H:%M:%S)] $*"; }

# 1. wait for the download, then one resume pass to fix any failed files
while pgrep -u "$USER" -f "get-input.sh --dest $M/input" >/dev/null; do sleep 60; done
log "download finished; verifying"
(cd $M && bash get-input.sh --dest $M/input --jobs 16 > $M/get-input-verify.log 2>&1) || log "verify reported problems (see get-input-verify.log)"
tail -3 $M/get-input-verify.log

# 2. truth: re-parse kinds whose parser changed since the early batch, pass 1 for new files, pass 2 over everything
$P -I tools/drop_from_shards.py $T ole
$P -I tools/complete_files.py $M/input $M/input/.get-input/manifest.tsv $T/all.txt
$P -I deid.py truth --input $M/input --out $T --files $T/all.txt --append --workers 14 > $T/truth_full.log 2>&1
log "truth: $(grep -E 'identities|done in' $T/truth_full.log | tail -2 | tr '\n' ' ')"

# 3. checks + copy baseline sanity
$P -I deid.py checks --truth $T --out $M/private/checks.jsonl > $M/private/checks.log 2>&1
log "checks: $(head -1 $M/private/checks.log)"

# 4. pipeline (incremental: outputs are deterministic, so files from an earlier interim run are simply overwritten;
#    anything no longer produced -- e.g. a path renamed differently by the final dictionary -- is removed)
mkdir -p "$A"
$P -I run_pipeline.py --input $M/input --identities $T/identities.json --out $A/corpus --log $L/run.jsonl --workers 14 > $L/pipeline.log 2>&1
log "pipeline: $(tail -1 $L/pipeline.log)"
$P -I tools/prune_stale.py $A/corpus $L/run.jsonl > $L/prune.log 2>&1
log "prune: $(tail -1 $L/prune.log)"

# 5. code/ + README.txt, preflight
$P -I tools/make_attempt.py --repo . --attempt $A --log $L/run.jsonl --truth $T --label "$NAME" > $L/make_attempt.log 2>&1
$P -I deid.py preflight --attempt $A --input $M/input > $L/preflight.log 2>&1
log "preflight: $(tr '\n' ' ' < $L/preflight.log | cut -c1-300)"

# 6. local scores
$P -I deid.py score --checks $M/private/checks.jsonl --corpus $M/input > $L/score_copy.log 2>&1
$P -I deid.py score --checks $M/private/checks.jsonl --corpus $A/corpus --report $L/report.json > $L/score.log 2>&1
log "copy baseline: $(grep LOCAL $L/score_copy.log)"
log "attempt: $(grep LOCAL $L/score.log)"
touch "$A/.final"     # tells tools/uploader.py --loop that the attempt folder is complete"
cat $L/score.log
log "DONE"
