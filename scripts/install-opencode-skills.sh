#!/usr/bin/env bash
# Install the ported pstack craft skills into the global OpenCode skills dir.
# Source of truth: tooling/opencode-skills/. Idempotent — re-run to refresh.
# Override the target with OPENCODE_SKILLS_DIR.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO/tooling/opencode-skills"
DEST="${OPENCODE_SKILLS_DIR:-$HOME/.config/opencode/skills}"

mkdir -p "$DEST"
count=0
for d in "$SRC"/*/; do
  name="$(basename "$d")"
  [ -f "$d/SKILL.md" ] || continue
  mkdir -p "$DEST/$name"
  cp "$d/SKILL.md" "$DEST/$name/SKILL.md"
  count=$((count + 1))
done
echo "Installed $count skills into $DEST"

# Install the local pre-commit craft gate so a fresh clone is protected too.
if [ -d "$REPO/.git" ] && [ -f "$REPO/scripts/pre-commit-craft-gate.sh" ]; then
  chmod +x "$REPO/scripts/pre-commit-craft-gate.sh"
  ln -sf ../../scripts/pre-commit-craft-gate.sh "$REPO/.git/hooks/pre-commit"
  echo "Installed pre-commit craft gate into $REPO/.git/hooks/pre-commit"
fi
