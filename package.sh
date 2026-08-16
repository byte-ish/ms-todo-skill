#!/usr/bin/env bash
# Build the uploadable skill bundle for the Claude app.
#
# Claude Code reads this repo in place (clone it into ~/.claude/skills/), but
# the Claude app takes an uploaded .zip, so the two need different artifacts.
# The bundle carries only what the skill uses at runtime — no tests, no CI
# config, no packaging metadata — which keeps it small and keeps repo
# scaffolding out of the model's view.
#
#   ./package.sh [output-dir]        # defaults to ./dist
#
# The archive must contain a single top-level folder with SKILL.md directly
# inside it. Uploading a zip of the *contents* instead will be rejected.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$SRC/dist}"
NAME="ms-todo"
STAGE="$OUT/$NAME"

command -v zip >/dev/null || { echo "error: 'zip' is not installed" >&2; exit 1; }

rm -rf "$STAGE" "$OUT/$NAME.zip"
mkdir -p "$STAGE"

cp "$SRC/SKILL.md" "$SRC/LICENSE" "$STAGE/"
cp -R "$SRC/scripts" "$SRC/references" "$STAGE/"

find "$STAGE" \( -name __pycache__ -o -name '*.pyc' -o -name .DS_Store \) -exec rm -rf {} + 2>/dev/null || true

# Fail loudly rather than shipping a bundle that silently lacks its entry point.
test -f "$STAGE/SKILL.md" || { echo "error: SKILL.md missing from bundle" >&2; exit 1; }
test -f "$STAGE/scripts/todo.py" || { echo "error: scripts/todo.py missing from bundle" >&2; exit 1; }

( cd "$OUT" && zip -qr "$NAME.zip" "$NAME" )
rm -rf "$STAGE"

# Same archive under both extensions. A .skill file *is* a zip, but upload
# dialogs filter by extension, and a picker that only offers .skill will not
# show a .zip at all — which looks like a broken package rather than a filter.
cp "$OUT/$NAME.zip" "$OUT/$NAME.skill"

printf 'built %s (%s)\n' "$OUT/$NAME.zip" "$(du -h "$OUT/$NAME.zip" | cut -f1)"
printf 'built %s (identical archive, for pickers that expect .skill)\n' "$OUT/$NAME.skill"
