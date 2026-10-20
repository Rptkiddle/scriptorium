#!/usr/bin/env bash
# Pre-push check for the public repository. Git runs it as the pre-push hook
# (install: ln -s ../../scripts/check_push.sh .git/hooks/pre-push) and passes
# one line per ref on stdin: <local ref> <local sha> <remote ref> <remote sha>.
# It refuses the push if the outgoing commits contain a secret (gitleaks), a
# personal email address, a Co-Authored-By trailer, or an author other than
# the GitHub noreply address; it warns about added lines with /Users/ paths.
set -euo pipefail

NOREPLY="14862381+Rptkiddle@users.noreply.github.com"
ZERO=0000000000000000000000000000000000000000
fail=0

while read -r local_ref local_sha remote_ref remote_sha; do
  [ "$local_sha" = "$ZERO" ] && continue  # deleting a remote branch
  if [ "$remote_sha" = "$ZERO" ]; then
    range="$local_sha --not --remotes=origin"
  else
    range="$remote_sha..$local_sha"
  fi
  commits=$(git rev-list $range)
  [ -z "$commits" ] && continue
  echo "check_push: $(echo "$commits" | wc -l | tr -d ' ') commit(s) for $remote_ref"

  if ! gitleaks git --no-banner --redact --log-opts="$range" . >/dev/null 2>&1; then
    echo "  REFUSED: gitleaks found a possible secret (run: gitleaks git --log-opts=\"$range\" -v)"
    fail=1
  fi
  bad_authors=$(git log --format='%ae %ce' $range | tr ' ' '\n' | sort -u | grep -v -x "$NOREPLY" || true)
  if [ -n "$bad_authors" ]; then
    echo "  REFUSED: author or committer email is not the noreply address: $bad_authors"
    fail=1
  fi
  if git log --format='%B' $range | grep -q -i '^Co-Authored-By:'; then
    echo "  REFUSED: a commit message carries a Co-Authored-By trailer"
    fail=1
  fi
  added=$(git log -p --format= $range -- . ':!scripts/check_push.sh' | grep '^+' | grep -v '^+++' || true)
  if echo "$added" | grep -q -i -E '@icloud\.com|@me\.com|@gmail\.com'; then
    echo "  REFUSED: an added line holds a personal email address"
    fail=1
  fi
  if echo "$added" | grep -q '/Users/'; then
    echo "  WARNING: an added line holds a /Users/ path:"
    echo "$added" | grep '/Users/' | head -5 | sed 's/^/    /'
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "check_push: push refused"
  exit 1
fi
echo "check_push: ok"
