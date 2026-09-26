#!/usr/bin/env bash
# Pull the latest code for the current branch and install any new Python packages.
# Run by the deploy webhook (the service restarts itself afterwards), or by hand:
#   ./deploy/update.sh && sudo systemctl restart assistant
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

branch="$(git rev-parse --abbrev-ref HEAD)"
before="$(git rev-parse --short HEAD)"
echo "Updating branch $branch (at $before)"
git pull --ff-only origin "$branch"
after="$(git rev-parse --short HEAD)"

if [[ "$before" == "$after" ]]; then
  echo "Already up to date."
else
  git log --oneline "$before..$after"
fi
.venv/bin/pip install -q -r requirements.txt
echo "Updated to $after"
