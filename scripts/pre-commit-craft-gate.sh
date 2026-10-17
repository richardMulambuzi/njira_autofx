#!/bin/sh
# Craft gate: hard-blocks commits whose staged additions carry AI slop.
# Encodes the mechanical checks from the `unslop` skill. Comment discipline
# (`no-comments` / `comment-sicko`) cannot be judged by regex, so it is only reminded here.
# Bypass for a legitimate exception: SKIP_CRAFT_GATE=1 git commit ...
# shortcut: slop detection is a fixed vocabulary, upgrade by moving it to a shared config file.

[ "${SKIP_CRAFT_GATE:-}" = "1" ] && exit 0

# Added lines only (drop the +++ header lines).
# This file is excluded: its vocabulary regex would otherwise block its own commit.
added=$(git diff --cached --unified=0 --no-color -- ':!scripts/pre-commit-craft-gate.sh' | grep -E '^\+' | grep -vE '^\+\+\+')
[ -z "$added" ] && exit 0

# unslop rule 7 (AI vocabulary) + rule 3 (superficial -ing) + rule 9 ("not just X, but Y").
slop='delve|pivotal|tapestry|testament|showcas(e|ing)|fostering|crucial|intricate|vibrant|garner|underscor(e|ing)|interplay|it'"'"'s not just|not only .* but also'
hits=$(printf '%s\n' "$added" | grep -inE "$slop")
dash=$(printf '%s\n' "$added" | grep -n '—')

if [ -n "$hits" ] || [ -n "$dash" ]; then
  echo "craft-gate: BLOCKED — staged additions contain AI slop patterns."
  [ -n "$hits" ] && printf '%s\n' "$hits" | sed 's/^/  slop: /'
  [ -n "$dash" ]  && printf '%s\n' "$dash" | sed 's/^/  em dash (unslop rule 13): /'
  echo ""
  echo "Fix: run the unslop skill on these lines, or reword. Then re-stage."
  echo "Also run the no-comments skill over the staged diff before committing."
  echo "Escape hatch (documented): SKIP_CRAFT_GATE=1 git commit ..."
  exit 1
fi

exit 0
