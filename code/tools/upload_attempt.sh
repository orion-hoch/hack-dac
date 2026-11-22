#!/usr/bin/env bash
# Upload an attempt folder with the organisers' upload.py, picking up fresh 1-hour tokens from a file.
# Usage: bash tools/upload_attempt.sh <attempt-name> <team-id>
#   Put the token from the track page ("Get upload token") in ~/machina/private/token.txt (one line).
#   When it expires, overwrite that file with a new token; upload.py waits up to 2 h for it.
#   Re-running skips files already uploaded (same size + MD5), so it is safe to restart.
set -euo pipefail
NAME="$1"; TEAM="$2"
M=~/machina; A=$M/attempts/$NAME; TOK=$M/private/token.txt
test -s "$TOK" || { echo "missing $TOK"; exit 1; }
chmod 600 "$TOK"
export MACHINA_UPLOAD_TOKEN="$(tr -d '[:space:]' < "$TOK")"
python3 $M/upload.py --dest "gs://builders-machina-submissions-1120/teams/$TEAM/$NAME/" \
  --corpus "$A/corpus" --code "$A/code" --readme "$A/README.txt" \
  --token-file "$TOK" --workers 32
