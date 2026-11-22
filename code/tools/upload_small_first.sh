#!/usr/bin/env bash
# Upload an attempt in two phases: every file <= LIMIT bytes first (99% of files, ~half the bytes), then the rest.
# Phase 1 uses a hard-linked staging tree with identical relative paths, so object keys are the same as a normal
# upload and phase 2 (full tree) skips everything phase 1 already sent. Safe to re-run at any time.
# Usage: bash tools/upload_small_first.sh <attempt-name> [limit_bytes] [workers]
set -uo pipefail
NAME="$1"; LIMIT="${2:-10000000}"; W="${3:-64}"
M=~/machina; A=$M/attempts/$NAME; S=$M/staging/$NAME; TOK=$M/private/token.txt; TEAM=$(cat $M/private/team_id.txt)
DEST="gs://builders-machina-submissions-1120/teams/$TEAM/$NAME/"
up() { MACHINA_UPLOAD_TOKEN="$(tr -d '[:space:]' < "$TOK")" python3 $M/upload.py --dest "$DEST" --token-file "$TOK" --workers "$W" "$@"; }
rm -rf "$S"; mkdir -p "$S/corpus"
(cd "$A/corpus" && find . -type f -size -"$((LIMIT / 1024))"k -print0 | while IFS= read -r -d '' f; do mkdir -p "$S/corpus/$(dirname "$f")"; ln "$f" "$S/corpus/$f"; done)
echo "[upload] phase 1: $(find "$S/corpus" -type f | wc -l) files <= $LIMIT bytes, $(du -sh "$S/corpus" | cut -f1)"
up --corpus "$S/corpus" --code "$A/code" --readme "$A/README.txt"
echo "[upload] phase 2: everything"
up --corpus "$A/corpus" --code "$A/code" --readme "$A/README.txt"
echo "[upload] done"
