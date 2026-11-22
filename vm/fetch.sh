#!/usr/bin/env bash
# Fetch the whole corpus with the bundled manifest. SOURCE=mirror (default) | s3 | <any base URL>. JOBS parallel
# streams (default 96). Resumable: complete files are skipped, partial ones resumed. Ends with a size check of
# every file against the manifest and exits non-zero on any missing or wrong-sized file.
set -euo pipefail
B=$(cd "$(dirname "$0")" && pwd); . ${DEID_HOME:-$HOME/deid}/env.sh
case "${SOURCE:-mirror}" in
  mirror) BASE=https://machina-input.joinbuilders.org ;;
  s3) BASE=https://machina-deid-v1-challenge.s3.us-west-2.amazonaws.com/releases/v1 ;;
  *) BASE=$SOURCE ;;
esac
D=$DEID_HOME/input; mkdir -p $D; JOBS=${JOBS:-96}
echo "source $BASE, $JOBS streams, into $D"
$DEID_HOME/venv/bin/python -I $B/verify_sizes.py $B/manifest.tsv $D --todo > $DEID_HOME/logs/fetch_todo.tsv || true
echo "to fetch: $(wc -l < $DEID_HOME/logs/fetch_todo.tsv) files"
awk -F'\t' -v b="$BASE" -v d="$D" '{printf "%s/%s%c%s/%s%c", b, $4, 0, d, $1, 0}' $DEID_HOME/logs/fetch_todo.tsv |
  xargs -0 -n 2 -P "$JOBS" sh -c 'mkdir -p "$(dirname "$2")" && curl -fsS --retry 10 --retry-all-errors --retry-delay 2 -C - -o "$2" "$1" || echo "FAIL $1" >&2' _ \
  2> $DEID_HOME/logs/fetch_errors.log || true
$DEID_HOME/venv/bin/python -I $B/verify_sizes.py $B/manifest.tsv $D
