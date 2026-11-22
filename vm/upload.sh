#!/usr/bin/env bash
# Upload one attempt with the organisers' upload.py (copied to $DEID_HOME/upload.py, unchanged). A person refreshes the
# 1-hour token in $DEID_HOME/private/token.txt; upload.py waits for a new one there when the old one runs out.
#   ATTEMPT=attempt-3 bash vm/upload.sh
set -euo pipefail
. ${DEID_HOME:-$HOME/deid}/env.sh
A=${ATTEMPT:?set ATTEMPT}; T=$(cat $DEID_HOME/private/team_id.txt); D=$DEID_HOME/out/$A
[ -f $D/PREFLIGHT_OK ] || { echo "preflight has not passed for $D"; exit 1; }
python3 $DEID_HOME/upload.py --dest gs://builders-machina-submissions-1120/teams/$T/$A/ --corpus $D/corpus --code $D/code \
  --readme $D/README.txt --token "$(cat $DEID_HOME/private/token.txt)" --token-file $DEID_HOME/private/token.txt --workers ${WORKERS:-32}
