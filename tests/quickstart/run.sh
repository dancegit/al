#!/usr/bin/env bash
# The README's quick start, rehearsed on a clean machine: a container with only racket, uv and git
# (tests/quickstart/Containerfile), a tiny application made on the spot, and the commands of the
# README's "Try it on your own repository" section, read from the README itself and run in its order.
#
#   tests/quickstart/run.sh              # builds the image with podman (else docker) and runs the steps in it
#   tests/quickstart/run.sh --inside     # the steps themselves, on this machine (what the container runs)
#
# Covered: tools/al init, gen, verify, doctor, explain, check and the --dry-run workflow (the stand-in
# agent). The two prompts that need an agent login are replaced by tests/quickstart/layer.rkt. Not
# covered, and listed at the end of every run: what needs a real agent login, and what needs podman
# inside the container. The whole output goes to $AL_QUICKSTART_LOG (default tests/quickstart/run.log,
# ignored by git). Exit: 0 passed, 1 a step failed, 3 no container engine.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
README="$ROOT/README.md"
SECTION="## Try it on your own repository"

NOT_COVERED_AGENT=(
  "meta/commands/scout-codebase.md (the prompt that completes LLM.txt)"
  "meta/commands/author-layer.md (the prompt that writes agentic.rkt)"
  "tools/al author"
  "tools/al create"
  "tools/al adopt-all"
  "uv run adws/adw_sdlc.py (the real agent)"
  "uv run adws/triggers/trigger_cron.py (the poller, with a real agent)"
)
NOT_COVERED_SETUP=(
  "tools/al adopt"
)
NOT_COVERED_PODMAN=(
  "tools/al sandbox LAYER doctor"
  "tools/al verify LAYER --workflow sdlc (when that workflow uses a container)"
)
NOT_COVERED_VOICE=(
  "tools/al voice LAYER setup"
)

# The command lines of the quick start: the lines of the section's shell blocks, comments removed.
readme_commands() {
  awk -v section="$SECTION" '
    $0 == section { inside = 1; next }
    inside && /^## / { exit }
    inside && /^```(bash|sh)[ \t]*$/ { code = 1; next }
    inside && /^```[ \t]*$/ { code = 0; next }
    inside && code { sub(/[ \t]+#.*$/, ""); if ($0 != "") print }
  ' "$README"
}

inside() {
  local n=0 line status
  step() { n=$((n + 1)); echo "STEP $n: $*"; }
  failed() { echo "FAILED at STEP $n"; exit 1; }

  step "make a tiny git repository at ~/code/myapp (one source file, a test.sh, one commit)"
  { mkdir -p ~/code/myapp && cd ~/code/myapp && git init -q -b main \
      && printf '#!/bin/sh\necho hello\n' > app.sh \
      && printf '#!/bin/sh\n[ "$(sh ./app.sh)" = hello ]\n' > test.sh && chmod +x app.sh test.sh \
      && git add -A && git -c user.name=reader -c user.email=reader@example.org commit -qm initial; } || failed
  cd "$ROOT"

  while IFS= read -r line; do
    case "$line" in
      "tools/al init "*)
        step "$line"; eval "$line" || failed
        echo "STEP: stands in for scout-codebase and author-layer, which need an agent login: tests/quickstart/layer.rkt becomes ~/code/myapp.al/agentic.rkt"
        cp "$ROOT/tests/quickstart/layer.rkt" ~/code/myapp.al/agentic.rkt || failed ;;
      "tools/al verify "*" --workflow "*|"tools/al sandbox "*)
        echo "SKIP (needs podman inside the container): $line" ;;
      "tools/al voice "*)
        echo "SKIP (the tiny layer declares no (voice kokoro), and setup downloads speech models): $line" ;;
      "tools/al doctor "*)
        step "$line"
        status=0; out="$(eval "$line" 2>&1)" || status=$?
        echo "$out"
        if [ "$status" = 4 ] && [ "$(echo "$out" | grep -c '  MISSING$')" = 1 ] && echo "$out" | grep -Eq '^(claude|codex)[[:space:]]+MISSING$'; then
          echo "      exit 4 accepted: the one missing tool is the agent CLI, which a clean machine without a login does not have"
        elif [ "$status" != 0 ]; then failed; fi ;;
      "tools/al gen "*|"tools/al verify "*|"tools/al explain "*|"tools/al check "*)
        step "$line"; eval "$line" || failed ;;
      "cd "*)
        step "$line"; eval "$line" || failed ;;
      "uv run adws/"*" --dry-run")
        step "$line"; eval "$line" || failed ;;
      "uv run adws/"*)
        echo "SKIP (needs a real agent login): $line" ;;
      *)
        n=$((n + 1)); echo "STEP $n: $line"
        echo "the README's quick start shows a command this rehearsal does not know; add it to tests/quickstart/run.sh"
        failed ;;
    esac
  done < <(readme_commands)

  echo
  echo "quickstart: passed ($n steps)"
  for item in "${NOT_COVERED_AGENT[@]}"; do echo "not covered: needs a real agent login: $item"; done
  for item in "${NOT_COVERED_SETUP[@]}"; do echo "not covered: the tiny app has no agent setup to adopt: $item"; done
  for item in "${NOT_COVERED_PODMAN[@]}"; do echo "not covered: needs podman inside the container: $item"; done
  for item in "${NOT_COVERED_VOICE[@]}"; do echo "not covered: the tiny layer declares no (voice kokoro): $item"; done
}

if [ "${1:-}" = "--inside" ]; then
  inside
  exit 0
fi
[ $# -eq 0 ] || { echo "usage: tests/quickstart/run.sh [--inside]" >&2; exit 2; }

LOG="${AL_QUICKSTART_LOG:-$ROOT/tests/quickstart/run.log}"
mkdir -p "$(dirname "$LOG")"
{
  if command -v podman >/dev/null 2>&1; then ENGINE=podman
  elif command -v docker >/dev/null 2>&1; then ENGINE=docker
  else
    echo "quickstart: no container engine (podman or docker) found; not run"
    exit 3
  fi
  IMAGE="al-quickstart:rehearsal"
  echo "quickstart: building $IMAGE with $ENGINE from $ROOT"
  "$ENGINE" build -q -t "$IMAGE" -f "$ROOT/tests/quickstart/Containerfile" "$ROOT" || { echo "FAILED: the image did not build"; exit 1; }
  "$ENGINE" run --rm "$IMAGE" tests/quickstart/run.sh --inside
} 2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
