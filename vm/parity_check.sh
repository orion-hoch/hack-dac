#!/usr/bin/env bash
# On the VM, after setup.sh, the private files and fetch.sh: stand-ins and 50 outputs must equal the Spark's.
set -euo pipefail
B=$(cd "$(dirname "$0")" && pwd); . ${DEID_HOME:-$HOME/deid}/env.sh
$DEID_HOME/venv/bin/python -I $B/code/tools/filter_ids.py $DEID_HOME/private/identities_final.json $DEID_HOME/private/identities_v3.json $DEID_HOME/logs/identities_dropped.tsv
$DEID_HOME/venv/bin/python -I $B/parity.py check $DEID_HOME/input $DEID_HOME/private
