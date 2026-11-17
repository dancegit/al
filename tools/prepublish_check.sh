#!/usr/bin/env bash
# The scan a person runs before making the repository public: secrets, foreign commit addresses,
# home paths, internal hosts, addresses and files over 5 MB, in the tree and in the whole history.
# It changes nothing. Exit 0: no finding; 1: findings; 2: bad use. See tools/prepublish_check.py.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
if command -v uv >/dev/null 2>&1; then
  exec uv run --no-project "$here/prepublish_check.py" "$@"
fi
exec python3 "$here/prepublish_check.py" "$@"
