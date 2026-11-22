#!/usr/bin/env bash
# On the VM after setup, private files and fetch: stand-ins and the 50 reference outputs must equal the Spark's.
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd); . ${DEID_HOME:-$HOME/deid}/env.sh; PV=$DEID_HOME/private
$DEID_HOME/venv/bin/python -I $R/code/tools/filter_ids.py $PV/identities_final.json $PV/identities_v3.json $DEID_HOME/logs/identities_dropped.tsv $PV/identities_final_scoped.json $PV/identities_v3_paths.json
$DEID_HOME/venv/bin/python -I $R/vm/parity.py check $DEID_HOME/input $PV
