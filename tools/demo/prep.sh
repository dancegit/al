#!/usr/bin/env bash
# Build throwaway demo layers under /tmp/al-demo from the committed LLM-authored sources.
#   prep.sh layer NAME [mock]   -> /tmp/al-demo/NAME.al   (mock: the mock agent is declared in the DSL)
#   prep.sh scale               -> /tmp/al-demo/scale.al  (custom agent inside a container, max 2 runs)
#   prep.sh clean               -> remove demo layers, their worktrees, branches and containers
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; D=/tmp/al-demo; mkdir -p "$D"
clean_bed() { local bed="$ROOT/testbeds/$1"; [ -d "$bed" ] || return 0
  git -C "$bed" worktree list --porcelain | awk '/^worktree /{print $2}' | grep "^$D/" | xargs -r -n1 git -C "$bed" worktree remove --force 2>/dev/null || true
  git -C "$bed" worktree prune; }
case "${1:-}" in
  layer)
    name="$2"; L="$D/$name.al"; clean_bed "$name"; rm -rf "$L"; mkdir -p "$L"
    cp "$ROOT/tests/authored/$name/LLM.txt" "$L/"
    sed -e "s|#:root \"[^\"]*\"|#:root \"$ROOT/testbeds/$name\"|" "$ROOT/tests/authored/$name/agentic.rkt" > "$L/agentic.rkt"
    if [ "${3:-}" = mock ]; then
      sed -i -e 's/#:agent claude-code/#:agent mock/' -e '0,/^  (prompt-input /s//  (agent mock)\n  (prompt-input /' "$L/agentic.rkt"
    fi ;;
  scale)
    L="$D/scale.al"; clean_bed flask; rm -rf "$L"; mkdir -p "$L/commands"
    printf '# Touch\n\nCreate the file proof.txt. $1\n\n{{output_contract}}\n' > "$L/commands/touch.src.md"
    cat > "$L/agentic.rkt" <<RKT
#lang al
(agentic-layer #:name "scale-demo"
  (codebase #:root "$ROOT/testbeds/flask" #:languages (python)
    (cmd proof "test -f proof.txt && sleep 4 && echo check ran in \$(cat /etc/hostname)"))
  (concurrency slots #:max-runs 2)
  (agent custom #:command "cat >/dev/null; test -f /run/.containerenv && echo agent-inside-container || echo agent-on-host; touch proof.txt")
  (environment worktree)
  (environment podman #:base-image "docker.io/library/python:3.12-slim" #:network none #:agent-inside #t)
  (command touch #:template custom #:file "commands/touch.src.md")
  (adw sandboxed #:environment (worktree podman)
    (steps (run agent #:command touch) (check proof #:cmd proof))))
RKT
    ;;
  clean)
    for b in flask ripgrep gin express jq; do clean_bed "$b"; done
    podman ps -a --format '{{.Names}}' 2>/dev/null | grep -E '^al-(scale-demo|flask|gin|ripgrep|express|jq)-' | xargs -r podman rm -f >/dev/null 2>&1 || true
    rm -rf "$D" ;;
  *) echo "usage: prep.sh layer NAME [mock] | scale | clean" >&2; exit 2 ;;
esac
