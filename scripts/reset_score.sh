#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
SCORE_FILE="${SCORE_FILE:-$REPO_DIR/score.json}"

read -r -p "This will reset scores in $SCORE_FILE. Type 'y' to continue: " confirm
if [[ "$confirm" != "y" ]]; then
    echo "Aborted."
    exit 1
fi

echo "{}" > "$SCORE_FILE"
echo "Reset $SCORE_FILE"
