#!/usr/bin/env bash
# Acceptance suite for al. Offline and LLM-free: every agent step uses the mock agent.
#   tests/acceptance.sh            all tiers except the slow real-check run
# Needs: racket, uv, git; testbeds/ cloned and layers/<name>/agentic.rkt authored (see README).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
export PLTCOLLECTS="$ROOT/racket:${PLTCOLLECTS:-}"
BEDS=(flask ripgrep gin express jq)
pass=0; fail=0
ok()  { pass=$((pass+1)); printf '  ok    %s\n' "$1"; }
bad() { fail=$((fail+1)); printf '  FAIL  %s\n' "$1"; }
t()   { local name="$1"; shift; if "$@" >/tmp/al-acc.out 2>&1; then ok "$name"; else bad "$name"; sed 's/^/        /' /tmp/al-acc.out | tail -8; fi; }
app_fingerprint() { git -C "testbeds/$1" rev-parse HEAD; git -C "testbeds/$1" branch --show-current; git -C "testbeds/$1" status --porcelain; }

echo "A. language"
t "racket compiler tests" raco test racket/al/tests/compiler-test.rkt
t "schema is generated from the vocabulary (no drift)" bash -c 'racket racket/al/cli.rkt schema | diff -q - schemas/agentic-layer-v1.schema.json'
t "LLM reference is generated from the vocabulary (no drift)" bash -c 'racket racket/al/cli.rkt reference | diff -q - docs/AL-DSL.llms.txt'
t "PITER breadth: P>=6 T>=6 E>=5 R>=3 implemented kinds" bash -c 'racket racket/al/cli.rkt counts | awk "\$1==\"prompt-input\"{p=\$2} \$1==\"trigger\"{t=\$2} \$1==\"environment\"{e=\$2} \$1==\"review\"{r=\$2} END{exit !(p>=6&&t>=6&&e>=5&&r>=3)}"'

echo "B. runtime unit and integration tests"
t "kit/runtime pytest" bash -c 'cd kit/runtime && uv run -q --with pytest pytest -q -p no:cacheprovider 2>&1 | tail -3 | tee /dev/stderr | grep -q " passed" && ! (uv run -q --with pytest pytest -q -p no:cacheprovider 2>&1 | tail -3 | grep -q failed)'

echo "C. testbeds: LLM.txt -> agentic.rkt -> IR -> layer -> ADW run"
for b in "${BEDS[@]}"; do
  before="$(app_fingerprint "$b")"
  t "$b: agentic.rkt compiles and IR validates" ./tools/al compile "layers/$b"
  t "$b: layer generates" ./tools/al gen "layers/$b"
  t "$b: layer check (files, placeholders, YAML, python, app clean)" ./tools/al check "layers/$b"
  t "$b: layer is outside the application tree" bash -c "! git -C testbeds/$b ls-files --others --exclude-standard | grep -q . && [ ! -e testbeds/$b/agentic.rkt ] && [ ! -d testbeds/$b/adws ]"
  t "$b: plan-build ADW runs end to end (mock agent, isolated worktree)" bash -c "cd layers/$b && uv run -q adws/adw_plan-build.py 'Add a note to the README about running the tests' --dry-run | tee /dev/stderr | grep -q '^status: *passed'"
  t "$b: run state recorded under the layer" bash -c "ls layers/$b/agents/*/state.json >/dev/null && ls layers/$b/specs/plan-*.md >/dev/null"
  t "$b: application checkout untouched (HEAD, branch, status)" bash -c "[ \"$before\" = \"\$(git -C testbeds/$b rev-parse HEAD; git -C testbeds/$b branch --show-current; git -C testbeds/$b status --porcelain)\" ]"
done

echo "D. tasklist prompt input + cron trigger (flask, mock agent declared in the DSL)"
rm -rf layers/flask-mock && mkdir -p layers/flask-mock && cp layers/flask/LLM.txt layers/flask-mock/
sed -e 's/#:agent claude-code/#:agent mock/' -e 's/^  (prompt-input tasklist)/  (agent mock)\n  (prompt-input tasklist)/' -e 's/#:name "flask"/#:name "flask-mock"/' layers/flask/agentic.rkt > layers/flask-mock/agentic.rkt
t "flask-mock: generates" ./tools/al gen layers/flask-mock
printf '\n## readme-note\n- [ ] Add a note to the README about running the tests {plan-build}\n- [>] Mention tox in that note {plan-build}\n' >> layers/flask-mock/tasks.md
t "cron --once picks the open task and runs its ADW" bash -c 'cd layers/flask-mock && uv run -q adws/triggers/trigger_cron.py --once'
t "task marked done with adw id; blocked task still waiting" bash -c "grep -Eq '^- \[x\] Add a note .*adw:[0-9a-f]{8}' layers/flask-mock/tasks.md && grep -q '^- \[>\] Mention tox' layers/flask-mock/tasks.md"
t "second poll runs the task that was blocked" bash -c "cd layers/flask-mock && uv run -q adws/triggers/trigger_cron.py --once && grep -Eq '^- \[x\] Mention tox' tasks.md"
t "flask checkout still untouched" bash -c "[ -z \"\$(git -C testbeds/flask status --porcelain)\" ] && [ \"\$(git -C testbeds/flask branch --show-current)\" = main ]"

echo; echo "acceptance: $pass passed, $fail failed"; [ "$fail" -eq 0 ]
