#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SCORE_FILE="${SCORE_FILE:-$SCRIPT_DIR/score.json}"

echo "{}" > "$SCORE_FILE"
echo "Reset $SCORE_FILE"
