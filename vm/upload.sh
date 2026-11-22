#!/usr/bin/env bash
# Upload one attempt with the organisers' upload.py (bundled unchanged). A person refreshes the 1-hour token by writing
# it to $DEID_HOME/private/token.txt; upload.py waits for a new token in that file when the old one runs out.
#   ATTEMPT=attempt-2 bash upload.sh        (team id from $DEID_HOME/private/team_id.txt)
set -euo pipefail
B=$(cd "$(dirname "$0")" && pwd); . ${DEID_HOME:-$HOME/deid}/env.sh
A=${ATTEMPT:?set ATTEMPT, e.g. attempt-2}; T=$(cat $DEID_HOME/private/team_id.txt); D=$DEID_HOME/out/$A
[ -f $DEID_HOME/out/$A/PREFLIGHT_OK ] || { echo "preflight has not passed for $D"; exit 1; }
python3 $B/upload.py --dest gs://builders-machina-submissions-1120/teams/$T/$A/ --corpus $D/corpus --code $D/code \
  --readme $D/README.txt --token "$(cat $DEID_HOME/private/token.txt)" --token-file $DEID_HOME/private/token.txt \
  --workers ${WORKERS:-32}
