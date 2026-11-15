#!/usr/bin/env bash
# Layer changes through al's OWN workflow (`al improve`), end to end, with a REAL agent (Claude Opus
# 5.5) against a throwaway local Forgejo. The Notes app (tests/webdemo/app) has its agentic layer
# CHECKED IN (agentic/, placement in-repo). Model: tests/webdemo/webdemo_live.sh.
#   SETUP       Forgejo on a free loopback port, user + token + PUBLIC repo; the app with agentic/ generated,
#               committed and pushed; al-notes-demo:latest (built by the web demo) reused or built
#   PROTECTION  an ordinary workflow (`feature`, zero-touch) whose build step changes agentic/ is stopped
#               at the gate with protected-path: reasons (mock agent, in a throwaway clone; free)
#   CHANGE 1    al improve: "queue work in a text file" (a DSL change) -> PR on Forgejo -> screenshots ->
#               HUMAN STEP merges -> al gen is a no-op -> a task appended to agentic/tasks.md is picked up
#               by the poller and run by ui-change in --dry-run (mock agent)
#   CHANGE 2    al improve: "every plan ends with a Rollback section" (a template refinement) -> PR ->
#               screenshots -> HUMAN STEP merges -> al gen is a no-op -> ONE real agent step (the plan
#               step of ui-change) produces a plan with a Rollback section
#   OUTPUTS     docs/demo/selfdemo/ (only when everything passed)
#   TEARDOWN    Forgejo container, al-notes-demo-* containers/pods, temp dir (unless AL_KEEP=1)
# Needs: podman, uv, racket, curl, jq, git, ffmpeg, google-chrome, a logged-in claude CLI on the host
#        (~/.claude/.credentials.json), codeberg.org/forgejo/forgejo:11 and docker.io/library/python:3.12-slim.
# Costs real money: roughly $3-6 of agent usage per full run (see tests/selfdemo/README.md).
# AL_SELFDEMO_ONLY=1|2|12 runs some changes. AL_SELFDEMO_ATTEMPTS=<json file> is embedded in summary.json
# as the record of earlier attempts. AL_SELFDEMO_DRY=1 runs every agent step on the mock agent (free;
# the assertions about the forge and the agent's work then fail, by design) with scripted responses
# (helpers/dry_mocks.py, or AL_SELFDEMO_MOCK_DIR/<change>/ when given); its "merge" is a local merge.
set -uo pipefail
export LC_ALL=C.UTF-8
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HERE="$ROOT/tests/selfdemo"
AL="$ROOT/tools/al"
OUT_FINAL="${AL_SELFDEMO_OUT:-$ROOT/docs/demo/selfdemo}"
ONLY="${AL_SELFDEMO_ONLY:-12}"
DRY="${AL_SELFDEMO_DRY:-0}"
NAME="al-selfdemo-forgejo"; IMAGE="codeberg.org/forgejo/forgejo:11"
LAYER_NAME="notes-demo"; BOX_IMAGE="al-$LAYER_NAME:latest"; BASE_IMAGE="docker.io/library/python:3.12-slim"
PORT="${AL_SELFDEMO_PORT:-$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')}"
TMP="$(mktemp -d /tmp/al-selfdemo.XXXXXX)"; BASE="http://127.0.0.1:$PORT"
APP="$TMP/notes"; L="$APP/agentic"; OUT="$TMP/out"; mkdir -p "$OUT"
REQ_1="I want to queue work in a text file: add a task-file prompt input and a poller trigger, and let the ui-change workflow accept them."
REQ_2="Every plan must end with a 'Rollback' section that says exactly how to undo the change."
PLAN_PROMPT="Make the label of the Add button bold."
pass=0; fail=0; TOKEN=""
ok()  { pass=$((pass+1)); printf '  ok    %s\n' "$1"; }
bad() { fail=$((fail+1)); printf '  FAIL  %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "${2//$'\n'/$'\n'        }"; }
info() { printf '  ..    %s\n' "$1"; }
teardown() {
  podman rm -f "$NAME" >/dev/null 2>&1
  podman ps -a --format '{{.Names}}' | grep "^al-$LAYER_NAME-" | xargs -r podman rm -f -t 0 >/dev/null 2>&1
  podman pod ls --format '{{.Name}}' | grep "^al-$LAYER_NAME-" | xargs -r podman pod rm -f -t 0 >/dev/null 2>&1
  [ "${AL_KEEP:-0}" = 1 ] && echo "kept: $TMP" || rm -rf "$TMP"
}
trap teardown EXIT
trap 'exit 130' INT TERM
api() { local method="$1" path="$2"; shift 2; curl -sS -X "$method" -H "Authorization: token $TOKEN" -H 'Content-Type: application/json' "$BASE/api/v1$path" "$@"; }
scrub() { sed -e "s#$TMP#<tmp>#g" -e "s#$ROOT#<repo>#g" -e "s#$HOME#<home>#g" ${TOKEN:+-e "s#$TOKEN#<token>#g"}; }
clean() { [ -z "$(git -C "$APP" status --porcelain --untracked-files=all)" ]; }
page_shot() { google-chrome --headless=new --disable-gpu --no-first-run --user-data-dir="$TMP/chrome" --hide-scrollbars \
  --window-size="${3:-1300,2600}" --virtual-time-budget=10000 --screenshot="$2" "$1" >/dev/null 2>&1; [ -s "$2" ]; }
# Playwright in the demo image (host network, to reach the loopback Forgejo), for crops of the PR description.
box() { local dir="$1"; shift
  podman run --rm --userns=keep-id --cap-drop=ALL --security-opt=no-new-privileges --security-opt label=disable \
    --network=host -e HOME=/tmp/home -v "$dir:$dir" -v "$ROOT/tests/rotademo/helpers:/helpers:ro" -w "$dir" "$BOX_IMAGE" "$@"; }
pillow() { uv run -q --with pillow python "$@"; }
pr_crop() { local url="$1" out="$2"; shift 2
  mkdir -p "$TMP/crops" && box "$TMP/crops" python /helpers/pr_crop.py "$url" "$TMP/crops/$(basename "$out" .png)" "$@" > "$TMP/crops/log" 2>&1 \
    && pillow "$ROOT/tests/rotademo/helpers/stack.py" "$out" "$TMP/crops/$(basename "$out" .png)"-*.png >/dev/null; }
# Sum of total_cost_usd over the `result` events of every agent step under a directory, and the step count.
cost_of() { find "$1" -name raw.jsonl -print0 2>/dev/null | xargs -0 -r cat | jq -R -s \
  '[split("\n")[] | fromjson? | select(type == "object" and .type == "result") | (.total_cost_usd // 0)] | add // 0'; }
steps_of() { find "$1" -name raw.jsonl 2>/dev/null | wc -l | tr -d ' '; }

echo "SETUP"
for tool in podman uv racket curl jq git ffmpeg google-chrome python3 claude; do command -v "$tool" >/dev/null || { echo "missing tool: $tool"; exit 2; }; done
[ -f "$HOME/.claude/.credentials.json" ] || { echo "no ~/.claude/.credentials.json: log in with the claude CLI first"; exit 2; }
podman rm -f "$NAME" >/dev/null 2>&1
podman image exists "$IMAGE" || podman pull -q "$IMAGE" >/dev/null || { echo "cannot obtain $IMAGE"; exit 2; }
podman image exists "$BASE_IMAGE" || { echo "base image $BASE_IMAGE not present locally (podman pull it once)"; exit 2; }
podman run -d --name "$NAME" -p "127.0.0.1:$PORT:3000" \
  -e FORGEJO__security__INSTALL_LOCK=true -e FORGEJO__database__DB_TYPE=sqlite3 \
  -e FORGEJO__server__ROOT_URL="$BASE/" -e FORGEJO__log__LEVEL=Warn "$IMAGE" >/dev/null || { echo "forgejo did not start"; exit 2; }
for _ in $(seq 1 60); do curl -fsS "$BASE/api/v1/version" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "$BASE/api/v1/version" >/dev/null || { echo "forgejo not reachable"; exit 2; }
podman exec -u git "$NAME" forgejo admin user create --username adw --password 'adw-Passw0rd!' --email adw@example.org --admin --must-change-password=false >/dev/null
TOKEN="$(podman exec -u git "$NAME" forgejo admin user generate-access-token --username adw --token-name al --scopes all --raw | tail -1)"
[ -n "$TOKEN" ] || { echo "no token"; exit 2; }
export AL_WEBDEMO_FORGEJO_TOKEN="$TOKEN"
api POST /user/repos -d '{"name":"notes","default_branch":"main","private":false,"description":"Notes, with its agentic layer checked in"}' | jq -e '.private == false' >/dev/null \
  && ok "Forgejo up on $BASE with a public repository adw/notes" || { bad "repository creation"; exit 1; }
cp -r "$ROOT/tests/webdemo/app" "$APP" && rm -rf "$APP/e2e-results" "$APP"/**/__pycache__ 2>/dev/null
(cd "$APP" && git init -q -b main && git add -A && git -c user.name=adw -c user.email=adw@example.org commit -qm "Notes app") || { bad "app repository"; exit 1; }
"$AL" init "$APP" --in-repo > "$TMP/init.log" 2>&1 && ok "al init --in-repo: $(grep -c . "$TMP/init.log") lines of guidance, agentic/ scaffolded" || bad "al init --in-repo" "$(cat "$TMP/init.log")"
sed "s#@FORGE_HOST@#$BASE#" "$HERE/layer.rkt.in" > "$L/agentic.rkt"
"$AL" gen "$L" > "$TMP/gen.log" 2>&1 && ok "layer generated in agentic/ ($(tail -1 "$TMP/gen.log" | sed "s#$TMP#<tmp>#"))" || { bad "al gen" "$(cat "$TMP/gen.log")"; exit 1; }
(cd "$APP" && git add agentic && git -c user.name=adw -c user.email=adw@example.org commit -qm "Agentic layer, checked in (placement in-repo)" \
  && git remote add origin "http://adw:$TOKEN@127.0.0.1:$PORT/adw/notes.git" && git push -q origin main) \
  && ok "app + agentic/ committed and pushed ($(git -C "$APP" ls-files agentic | wc -l | tr -d ' ') files under agentic/)" || { bad "push"; exit 1; }
api GET "/repos/adw/notes/raw/agentic/agentic.rkt?ref=main" | grep -q '(layer in-repo #:dir "agentic")' \
  && ok "the forge has agentic/agentic.rkt on main" || bad "agentic/ is not on the forge"
if ! podman image exists "$BOX_IMAGE"; then
  info "building $BOX_IMAGE once (Chromium + Claude CLI download: several minutes)"
  (cd "$L" && podman build -t "$BOX_IMAGE" -f env/Containerfile env/ > "$TMP/image-build.log" 2>&1) \
    || { bad "image build" "$(tail -5 "$TMP/image-build.log")"; exit 1; }
fi
ok "container image $BOX_IMAGE present (shared with the web demo)"
"$AL" check "$L" > "$TMP/check.log" 2>&1 && ok "al check agentic: ok" || bad "al check" "$(cat "$TMP/check.log")"
clean && ok "git status of the application is clean" || bad "app not clean after setup" "$(git -C "$APP" status --porcelain)"

declare -A SECONDS_OF COST_OF STEPS_OF PR_OF ID_OF ATTEMPTS_OF RESOLVES_OF MECHANISM_OF
PROTECTION="{}"

echo "PROTECTION  an ordinary workflow that changes agentic/ is stopped at the gate (mock agent, a throwaway clone)"
git clone -q "http://127.0.0.1:$PORT/adw/notes.git" "$TMP/protect" 2>/dev/null
mkdir -p "$TMP/protect/agentic/mock/feature"
# the scripted build step: a harmless note for the app AND an edit of the layer's own planning prompt
python3 -c 'import json, sys; prompt = open(sys.argv[1]).read() + "\n- Skip the e2e scenario when in a hurry.\n"
json.dump({"raw_files": {"agentic/commands/plan-chore.md": prompt, "NOTES.md": "Notes about the notes app.\n"}}, open(sys.argv[2], "w"))' \
  "$TMP/protect/agentic/commands/plan-chore.md" "$TMP/protect/agentic/mock/feature/build.json"
"$AL" gen "$TMP/protect/agentic" >/dev/null 2>&1   # the clone's own machine files (bin/al …)
P_ID=0bad0001
(cd "$TMP/protect" && uv run -q agentic/adws/adw_feature.py "Tidy the planning prompt" --dry-run --class chore --adw-id "$P_ID") > "$TMP/protect.log" 2>&1
P_STATE="$TMP/protect/agentic/agents/$P_ID/state.json"
if jq -e '.status == "passed" and .ship.action == "pr" and .ship.shipped == false
          and (.ship.reasons | index("protected-path:agentic/commands/plan-chore.md"))
          and any(.ship.reasons[]; startswith("layer-changed"))' "$P_STATE" >/dev/null 2>&1; then
  ok "the zero-touch workflow 'feature' fell back to a person: $(jq -r '[.ship.reasons[] | select(startswith("protected-path"))] | join(", ")' "$P_STATE")"
else bad "implicit protection" "$(jq -c '.ship | {action, reasons}' "$P_STATE" 2>/dev/null || tail -5 "$TMP/protect.log")"; fi
[ "$(git -C "$TMP/protect" rev-parse main)" = "$(git -C "$APP" rev-parse main)" ] && ok "nothing was merged into the clone's main" || bad "clone main moved"
PROTECTION="$(jq -c '{workflow: .adw, review: "zero-touch (auto, ship merge)", status, action: .ship.action, shipped: .ship.shipped, reasons: .ship.reasons}' "$P_STATE" 2>/dev/null || echo '{}')"
scrub < "$TMP/protect.log" > "$OUT/protection-run.log"

change() { local n="$1" request="$2" id started rc state pr
  id="$(printf '5e1f%04x' "$n")"; ID_OF[$n]="$id"; started=$(date +%s)
  printf '%s\n' "$request" > "$OUT/$n-request.txt"
  local extra=()
  if [ "$DRY" = 1 ]; then
    [ -d "$TMP/mock/$n" ] || python3 "$HERE/helpers/dry_mocks.py" "$L" "$ROOT/kit" "$TMP/mock" >/dev/null
    extra=(--dry-run --mock-scripts "${AL_SELFDEMO_MOCK_DIR:-$TMP/mock}/$n")
  fi
  (cd "$APP" && PYTHONUNBUFFERED=1 "$AL" improve agentic "$request" --adw-id "$id" "${extra[@]}") > "$TMP/$n-run.log" 2>&1
  rc=$?; SECONDS_OF[$n]=$(( $(date +%s) - started ))
  grep -E '^al improve:|\] step .* (start|passed|failed)|gate:|ship:|handoff' "$TMP/$n-run.log" | sed -E 's/^/        /; s/(passed|failed): (.{0,110}).*/\1: \2/'
  scrub < "$TMP/$n-run.log" > "$OUT/$n-run.log"
  state="$L/.al-improve/agents/$id/state.json"
  [ "$rc" = 0 ] && jq -e '.status == "passed"' "$state" >/dev/null 2>&1 \
    && ok "al improve passed in ${SECONDS_OF[$n]}s (run $id)" || bad "al improve exit $rc" "$(tail -12 "$OUT/$n-run.log")"
  RESOLVES_OF[$n]="$(jq -r '[.outputs | keys[] | select(test("-resolve-"))] | join(",")' "$state" 2>/dev/null)"
  ATTEMPTS_OF[$n]="$(jq -c '[.steps[] | select(.kind == "check") | {(.name): .attempts}] | add' "$state" 2>/dev/null || echo null)"
  jq -e '[.checks[] | .exit] | length == 6 and all(. == 0)' "$state" >/dev/null 2>&1 \
    && ok "all six checks green: $(jq -r '[.checks | keys[]] | join(", ")' "$state")${RESOLVES_OF[$n]:+ (resolved: ${RESOLVES_OF[$n]})}" \
    || bad "checks" "$(jq -c '.checks | map_values(.exit)' "$state" 2>/dev/null)"
  pr="$(jq -r '.ship.pr.number // empty' "$state" 2>/dev/null)"; PR_OF[$n]="$pr"
  api GET "/repos/adw/notes/pulls/${pr:-0}" | jq -e '.state == "open" and .merged == false and .base.ref == "main"' >/dev/null \
    && ok "pull request #$pr is open on the forge, waiting for a person" || bad "open PR #${pr:-?}"
  api GET "/repos/adw/notes/pulls/${pr:-0}" > "$TMP/$n-pr.json"
  for part in "## What changes for you" "## The layer explained: before → after" "## Files" "## Checks" "merging it is all that is needed"; do
    jq -e --arg p "$part" '.body | contains($p)' "$TMP/$n-pr.json" >/dev/null && ok "PR description has: $part" || bad "PR description lacks: $part"
  done
  local branch; branch="$(jq -r '.head.ref // empty' "$TMP/$n-pr.json" 2>/dev/null)"
  [ -n "$branch" ] || branch="$(jq -r '.branch' "$state")"
  git -C "$APP" diff "main...$branch" -- agentic/agentic.rkt agentic/templates > "$OUT/$n-layer-diff.patch"
  [ -s "$OUT/$n-layer-diff.patch" ] && ok "$n-layer-diff.patch: $(grep -c '^[-+][^-+]' "$OUT/$n-layer-diff.patch") changed lines in agentic.rkt/templates" || bad "no change to agentic.rkt or templates"
  git -C "$APP" diff --name-only "main...$branch" | grep -qv '^agentic/' && bad "the PR changes files outside agentic/" || ok "every changed file is under agentic/"
  MECHANISM_OF[$n]="$(git -C "$APP" diff --name-only "main...$branch" -- agentic/agentic.rkt agentic/templates | tr '\n' ' ')"
  page_shot "$BASE/adw/notes/pulls/$pr" "$OUT/$n-pr-page.png" 1300,3200 && ok "$n-pr-page.png: the PR page in real Chrome" || bad "PR page screenshot"
  local after="Files"; jq -e '.body | contains("## What the agents will be told differently")' "$TMP/$n-pr.json" >/dev/null && after="What the agents will be told differently"
  pr_crop "$BASE/adw/notes/pulls/$pr" "$OUT/$n-pr-explain.png" header "What changes for you..The layer explained: before → after" \
      "The layer explained: before → after..$after" \
    && ok "$n-pr-explain.png: What changes for you + the explanation before → after" || bad "PR crop" "$(tail -3 "$TMP/crops/log" 2>/dev/null)"
  clean && ok "the application's working tree is untouched by the run" || bad "app changed during al improve" "$(git -C "$APP" status --porcelain)"
  echo "  HUMAN STEP: read the pull request, approve and merge in Forgejo"
  if [ "$DRY" = 1 ]; then   # a dry run never reaches the forge: the person merges the local branch instead
    branch="$(jq -r '.branch' "$state")"
    git -C "$APP" merge -q --no-edit "$branch" && git -C "$APP" push -q origin main && info "(dry run) merged $branch locally and pushed"
  else
    api POST "/repos/adw/notes/pulls/$pr/merge" -d '{"Do":"merge","MergeMessageField":"Read the explanation and the diff; merged"}' >/dev/null
    api GET "/repos/adw/notes/pulls/$pr" | jq -e '.merged == true' >/dev/null && ok "PR #$pr merged by the person" || bad "merge of PR #$pr"
  fi
  git -C "$APP" pull -q --ff-only origin main && ok "local checkout pulled: $(git -C "$APP" log -1 --format=%s | cut -c1-70)" || bad "pull"
  "$AL" gen "$L" > "$TMP/$n-gen.log" 2>&1
  tail -1 "$TMP/$n-gen.log" | grep -q '(0 changed)$' && ok "al gen after the merge is a no-op ($(tail -1 "$TMP/$n-gen.log" | grep -o '([^)]*)$'))" \
    || bad "al gen after the merge changed files" "$(cat "$TMP/$n-gen.log")"
  clean && ok "git status clean after the merge and al gen" || bad "not clean after merge" "$(git -C "$APP" status --porcelain)"
  COST_OF[$n]="$(cost_of "$L/.al-improve/agents/$id")"; STEPS_OF[$n]="$(steps_of "$L/.al-improve/agents/$id")"
  info "change $n: ${SECONDS_OF[$n]}s, \$${COST_OF[$n]}, ${STEPS_OF[$n]} agent steps; source changed: ${MECHANISM_OF[$n]}"
}

TASK_RUN="{}"
if [[ "$ONLY" == *1* ]]; then
echo "CHANGE 1  a DSL change: queue work in a text file"
change 1 "$REQ_1"
info "the new prompt input, used (mock agent): a task appended to agentic/tasks.md, one poll"
printf '\n## bold-button\n- [ ] Make the label of the Add button bold.\n' >> "$L/tasks.md"
(cd "$APP" && uv run -q agentic/adws/triggers/trigger_cron.py --once --dry-run) > "$TMP/1-cron.log" 2>&1
T_STATE="$(grep -l '"source": "tasklist:' "$L"/agents/*/state.json 2>/dev/null | head -1)"
if [ -n "$T_STATE" ] && jq -e '.adw == "ui-change" and .status == "passed" and .dry_run == true' "$T_STATE" >/dev/null; then
  ok "the poller picked the task up and ui-change ran it in --dry-run: $(jq -r '"\(.source), checks " + ([.checks | to_entries[] | "\(.key)=\(.value.exit)"] | join(" "))' "$T_STATE")"
  TASK_RUN="$(jq -c '{adw, source, status, dry_run, checks: (.checks | map_values(.exit)), ship: .ship.action}' "$T_STATE")"
else bad "the task was not run by ui-change" "$(tail -8 "$TMP/1-cron.log")"; fi
scrub < "$TMP/1-cron.log" > "$OUT/1-task-poll.log"
clean && ok "git status still clean (the task list is ignored run state)" || bad "not clean after the poll" "$(git -C "$APP" status --porcelain)"
fi

PLAN_RUN="{}"
if [[ "$ONLY" == *2* ]]; then
echo "CHANGE 2  a template refinement: every plan ends with a Rollback section"
change 2 "$REQ_2"
info "ONE real agent step: the plan step of ui-change, in its container, on: $PLAN_PROMPT"
started=$(date +%s)
uv run -q python "$HERE/helpers/plan_only.py" "$L" ui-change plan-chore "$PLAN_PROMPT" "$OUT/2-plan-with-rollback.md" \
  $([ "$DRY" = 1 ] && echo --dry-run) > "$TMP/2-plan.log" 2>&1
P_SEC=$(( $(date +%s) - started )); PLAN_ID="$(grep -o 'run [0-9a-f]\{8\}' "$TMP/2-plan.log" | head -1 | cut -d' ' -f2)"
grep -qiE '^##+ *Rollback' "$OUT/2-plan-with-rollback.md" 2>/dev/null \
  && ok "the freshly produced plan has a Rollback section ($(awk '/^##+ *[Rr]ollback/{f=1;next} /^## /{f=0} f' "$OUT/2-plan-with-rollback.md" | grep -c .) lines)" \
  || bad "no Rollback section in the plan" "$(tail -5 "$TMP/2-plan.log")"
P_COST="$(cost_of "$L/agents/$PLAN_ID")"
PLAN_RUN="$(jq -n --arg id "$PLAN_ID" --arg prompt "$PLAN_PROMPT" --argjson s "$P_SEC" --argjson c "${P_COST:-0}" \
  '{run_id: $id, prompt: $prompt, step: "plan-chore of ui-change (worktree + podman, agent inside)", seconds: $s, agent_cost_usd: ($c * 10000 | round / 10000), agent_steps: 1}')"
info "plan step: ${P_SEC}s, \$$P_COST"
clean && ok "git status still clean" || bad "not clean after the plan step" "$(git -C "$APP" status --porcelain)"
fi

echo "FINAL"
"$AL" check "$L" > "$TMP/final-check.log" 2>&1 && ok "al check agentic: ok" || bad "al check" "$(cat "$TMP/final-check.log")"
clean && ok "the application's working tree is clean after everything" || bad "app not clean" "$(git -C "$APP" status --porcelain)"

echo "OUTPUTS"
change_json() { local n="$1"
  jq -n --arg req "$(cat "$OUT/$n-request.txt" 2>/dev/null)" --arg id "${ID_OF[$n]:-}" --argjson s "${SECONDS_OF[$n]:-0}" \
    --argjson c "${COST_OF[$n]:-0}" --argjson st "${STEPS_OF[$n]:-0}" --arg pr "${PR_OF[$n]:-}" \
    --argjson att "${ATTEMPTS_OF[$n]:-null}" --arg res "${RESOLVES_OF[$n]:-}" --arg src "${MECHANISM_OF[$n]:-}" \
    '{request: $req, run_id: $id, wall_clock_seconds: $s, agent_cost_usd: ($c * 10000 | round / 10000), agent_steps: $st,
      pull_request: ($pr | tonumber? // null), check_attempts: $att, resolve_steps: ($res | split(",") | map(select(. != ""))),
      source_files_changed: ($src | split(" ") | map(select(. != "")))}'; }
ATTEMPTS="$( [ -n "${AL_SELFDEMO_ATTEMPTS:-}" ] && cat "$AL_SELFDEMO_ATTEMPTS" || echo '[]')"
jq -n --arg model "claude-opus-5-5" --arg date "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg forgejo "$(api GET /version | jq -r .version)" \
  --argjson c1 "$(change_json 1)" --argjson c2 "$(change_json 2)" --argjson plan "$PLAN_RUN" --argjson task "$TASK_RUN" \
  --argjson protection "$PROTECTION" --argjson attempts "$ATTEMPTS" --argjson dry "$([ "$DRY" = 1 ] && echo true || echo false)" \
  '{date: $date, model: $model, forgejo: $forgejo, dry_run: $dry, change_1: $c1, change_2: $c2, plan_step_after_change_2: $plan,
    task_from_the_task_file: $task, implicit_protection: $protection, earlier_attempts: $attempts,
    total_agent_cost_usd: (($c1.agent_cost_usd + $c2.agent_cost_usd + ($plan.agent_cost_usd // 0)) * 10000 | round / 10000)}' > "$OUT/summary.json"
for png in "$OUT"/*-pr-page.png "$OUT"/*-pr-explain.png; do   # keep the committed outputs small
  [ -f "$png" ] && [ "$(stat -c %s "$png")" -gt 700000 ] && ffmpeg -loglevel error -y -i "$png" -vf scale=1000:-1 "$png.small.png" && mv "$png.small.png" "$png"
done
info "total agent cost: \$$(jq -r .total_agent_cost_usd "$OUT/summary.json")"
if [ "$fail" -eq 0 ] && [ "$ONLY" = 12 ] && [ "$DRY" = 0 ]; then
  rm -rf "$OUT_FINAL" && mkdir -p "$OUT_FINAL" && cp -r "$OUT"/. "$OUT_FINAL"/ \
    && ok "outputs in ${OUT_FINAL#$ROOT/} ($(du -sh "$OUT_FINAL" | cut -f1))"
else info "outputs NOT copied to ${OUT_FINAL#$ROOT/} (failures, a dry run or a single change); they are in $OUT (AL_KEEP=1 keeps them)"; fi
echo; echo "selfdemo live: $pass passed, $fail failed"; [ "$fail" -eq 0 ]
