#!/usr/bin/env bash
# Web application demo, end to end, with a REAL agent (Claude Opus 5.5 inside the run's container)
# against a throwaway local Forgejo. Model: tests/forge/forgejo_live.sh.
#   SETUP     Forgejo on a free loopback port, user + token + PUBLIC repo, push tests/webdemo/app,
#             generate the layer, build al-notes-demo:latest if missing, BEFORE screenshot
#   SCENARIO A  issue -> plan, build, unit tests, e2e capture, commit -> human review: PR with the
#             screenshots as proof -> screenshot of the PR page -> HUMAN STEP merges it
#   SCENARIO B  issue -> plan-feature, build, unit tests, e2e capture, commit -> two agent reviewers
#             with screenshots + video keyframes -> gate -> merged through the Forgejo API
#   OUTPUTS   docs/demo/webdemo/ (only when everything passed)
#   TEARDOWN  Forgejo container, al-notes-demo-* containers/pods, temp dir (unless AL_KEEP=1)
# Needs: podman, uv, racket, curl, jq, git, ffmpeg, google-chrome, ~/.claude/.credentials.json,
#        docker.io/library/python:3.12-slim and codeberg.org/forgejo/forgejo:11 (pulled if missing).
# Costs real money: roughly $1-4 of agent usage per full run (see tests/webdemo/README.md).
# AL_WEBDEMO_ONLY=a|b runs one scenario (debugging; nothing is copied to docs/demo/webdemo then).
set -uo pipefail
export LC_ALL=C.UTF-8
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HERE="$ROOT/tests/webdemo"
OUT_FINAL="${AL_WEBDEMO_OUT:-$ROOT/docs/demo/webdemo}"
ONLY="${AL_WEBDEMO_ONLY:-ab}"
NAME="al-webdemo-forgejo"; IMAGE="codeberg.org/forgejo/forgejo:11"
LAYER_NAME="notes-demo"; BOX_IMAGE="al-$LAYER_NAME:latest"; BASE_IMAGE="docker.io/library/python:3.12-slim"
PORT="${AL_WEBDEMO_PORT:-$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')}"
TMP="$(mktemp -d /tmp/al-webdemo.XXXXXX)"; BASE="http://127.0.0.1:$PORT"
APP="$TMP/notes"; LAYER="$TMP/notes.al"; OUT="$TMP/out"; mkdir -p "$OUT"
PROMPT_A_TITLE="Dark theme: dark slate page background"
PROMPT_A_BODY="Change the page background colour to dark slate (#1e293b) and make the text light so it stays readable."
PROMPT_B_TITLE="Search box that filters the notes"
PROMPT_B_BODY="Add a search box above the notes list that filters the notes as you type; show 'No notes match' when nothing matches."
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
fingerprint() { git -C "$APP" rev-parse HEAD; git -C "$APP" branch --show-current; git -C "$APP" status --porcelain; }
# Real Chrome on the host, as a person opening the PR would see it (the repository is public: no login).
page_shot() { google-chrome --headless=new --disable-gpu --no-first-run --user-data-dir="$TMP/chrome" --hide-scrollbars \
  --window-size="${3:-1300,2200}" --virtual-time-budget=10000 --screenshot="$2" "$1" >/dev/null 2>&1; [ -s "$2" ]; }
# Sum of total_cost_usd over the `result` events of every agent step of a run, and the number of agent steps.
run_cost() { find "$LAYER/agents/$1" -name raw.jsonl -print0 | xargs -0 -r cat | jq -R -s \
  '[split("\n")[] | fromjson? | select(type == "object" and .type == "result") | (.total_cost_usd // 0)] | add // 0'; }
agent_steps() { find "$LAYER/agents/$1" -name raw.jsonl | wc -l | tr -d ' '; }
# Run an ADW for an issue; streams concise progress; sets RUN_ID, RUN_RC, RUN_SECONDS.
run_adw() { local letter="$1" adw="$2" klass="$3" issue="$4" started
  started=$(date +%s)
  (cd "$LAYER" && PYTHONUNBUFFERED=1 uv run -q adws/dispatch.py --adw "$adw" --source "issue:$issue" --class "$klass") 2>&1 \
    | tee "$TMP/$letter-run.log" \
    | grep --line-buffered -E '\] (step .* (start|passed|failed|skipped)|gate:|ship:|review verdict|warning:)' \
    | sed -u -E 's/^/        /; s/(passed|failed): (.{0,110}).*/\1: \2/'
  RUN_RC=${PIPESTATUS[0]}; RUN_SECONDS=$(( $(date +%s) - started ))
  RUN_ID="$(grep -o -m1 '\[adw [0-9a-f]\{8\}\]' "$TMP/$letter-run.log" | tr -d '[]' | cut -d' ' -f2)"
  scrub < "$TMP/$letter-run.log" > "$OUT/$letter-run.log"
}
pr_has_proof() {  # comments carry the Proof comment with embedded attachment images
  api GET "/repos/adw/notes/issues/$1/comments" | jq -e '[.[] | select(.body | contains("Proof from run")
    and test("!\\[[^]]*\\]\\(http[^)]*/attachments/"))] | length == 1' >/dev/null; }
first_attachment() { api GET "/repos/adw/notes/issues/$1/comments" \
  | jq -r '[.[] | .body | select(contains("Proof from run")) | match("!\\[[^]]*\\]\\((http[^)]*/attachments/[^)]*)\\)").captures[0].string][0] // ""'; }

echo "SETUP"
for tool in podman uv racket curl jq git ffmpeg google-chrome; do command -v "$tool" >/dev/null || { echo "missing tool: $tool"; exit 2; }; done
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
api POST /user/repos -d '{"name":"notes","default_branch":"main","private":false,"description":"Notes: the al web demo app"}' | jq -e '.private == false' >/dev/null \
  && ok "Forgejo up on $BASE with a public repository adw/notes" || { bad "repository creation"; exit 1; }
cp -r "$HERE/app" "$APP" && rm -rf "$APP/e2e-results" "$APP"/**/__pycache__ 2>/dev/null
(cd "$APP" && git init -q -b main && git add -A && git -c user.name=adw -c user.email=adw@example.org commit -qm "Notes app" \
  && git remote add origin "http://adw:$TOKEN@127.0.0.1:$PORT/adw/notes.git" && git push -q origin main) \
  && ok "app pushed to the forge ($(git -C "$APP" ls-files | wc -l | tr -d ' ') files)" || { bad "push"; exit 1; }
mkdir -p "$LAYER" && sed "s#@FORGE_HOST@#$BASE#" "$HERE/layer.rkt.in" > "$LAYER/agentic.rkt"
"$ROOT/tools/al" gen "$LAYER" >/dev/null && ok "layer generated (tools/al gen)" || { bad "layer generation"; exit 1; }
if ! podman image exists "$BOX_IMAGE"; then
  info "building $BOX_IMAGE once (Chromium + Claude CLI download: several minutes)"
  (cd "$LAYER" && podman build -t "$BOX_IMAGE" -f env/Containerfile env/ > "$TMP/image-build.log" 2>&1) \
    || { bad "image build" "$(tail -5 "$TMP/image-build.log")"; exit 1; }
fi
ok "container image $BOX_IMAGE present (reused between runs)"
export AL_WEBDEMO_FORGEJO_TOKEN="$TOKEN"

echo "BEFORE  the untouched app, captured by its own e2e cmd"
"$ROOT/tools/al" verify "$LAYER" --cmd e2e > "$TMP/verify.log" 2>&1 \
  && ok "al verify --cmd e2e passes on main in the layer's container" || bad "al verify --cmd e2e" "$(tail -8 "$TMP/verify.log")"
# al verify discards its throwaway worktree (and e2e-results/ with it), so the BEFORE images come from
# the same capture run once more, in the same image, on an export of main, with the run's hardening flags.
mkdir -p "$TMP/before" && git -C "$APP" archive main | tar -x -C "$TMP/before"
podman run --rm --userns=keep-id --cap-drop=ALL --security-opt=no-new-privileges --security-opt label=disable \
  -e HOME=/tmp/home -v "$TMP/before:$TMP/before" -w "$TMP/before" "$BOX_IMAGE" python e2e/capture.py > "$TMP/before.log" 2>&1 \
  && cp "$TMP/before/e2e-results/01-home.png" "$OUT/before.png" && ok "before.png captured (light theme, $(grep -c '^ok' "$TMP/before.log") steps)" \
  || bad "before capture" "$(tail -6 "$TMP/before.log")"

declare -A SECONDS_OF COST_OF STEPS_OF PR_OF COMMIT_OF MAIN_OF ID_OF

if [[ "$ONLY" == *a* ]]; then
echo "SCENARIO A  a visual change, reviewed by a human"
before="$(fingerprint)"
N="$(api POST /repos/adw/notes/issues -d "$(jq -n --arg t "$PROMPT_A_TITLE" --arg b "$PROMPT_A_BODY" '{title: $t, body: $b}')" | jq -r .number)"
# As a person filing a UI issue would: attach a screenshot of the current page to the issue. The
# issue text becomes the PR's summary, so the PR shows this BEFORE picture next to the run's proof.
BEFORE_URL="$(curl -sS -H "Authorization: token $TOKEN" -F "attachment=@$OUT/before.png" \
  "$BASE/api/v1/repos/adw/notes/issues/$N/assets" | jq -r '.browser_download_url // empty')"
[ -n "$BEFORE_URL" ] && api PATCH "/repos/adw/notes/issues/$N" \
  -d "$(jq -n --arg b "$PROMPT_A_BODY"$'\n\n'"Current look, before the change:"$'\n\n'"![before.png]($BEFORE_URL)" '{body: $b}')" >/dev/null \
  && ok "issue #$N filed with a screenshot of the current page (before.png)" || bad "before.png on issue #$N"
info "running ADW ui-change (class chore) with the real agent"
run_adw a ui-change chore "$N"; A="$RUN_ID"; ID_OF[a]="$A"; SECONDS_OF[a]=$RUN_SECONDS
STATE="$LAYER/agents/$A/state.json"
[ "$RUN_RC" = 0 ] && jq -e '.status == "passed"' "$STATE" >/dev/null 2>&1 \
  && ok "ADW run $A passed in ${RUN_SECONDS}s" || bad "ADW run for issue #$N (exit $RUN_RC)" "$(tail -8 "$OUT/a-run.log")"
PR="$(jq -r '.ship.pr.number // empty' "$STATE" 2>/dev/null)"; PR_OF[a]="$PR"; COMMIT_OF[a]="$(jq -r '.ship.commit // ""' "$STATE" 2>/dev/null)"
api GET "/repos/adw/notes/pulls/${PR:-0}" | jq -e '.state == "open" and .merged == false and .base.ref == "main"' >/dev/null \
  && ok "pull request #$PR is open on the forge, waiting for a human" || bad "open PR #${PR:-?}"
api GET "/repos/adw/notes/pulls/${PR:-0}" | jq -e --arg u "$BEFORE_URL" '.body | contains("![before.png](" + $u + ")")' >/dev/null \
  && ok "the PR description shows the BEFORE screenshot" || bad "no before screenshot in the PR description"
jq -e '[.checks.test.exit, .checks.e2e.exit] == [0, 0]' "$STATE" >/dev/null && ok "unit tests and e2e capture passed in the container" || bad "checks" "$(jq -c .checks "$STATE")"
pr_has_proof "${PR:-0}" && ok "PR #$PR has one Proof comment with embedded image attachments" \
  || bad "Proof comment on PR #${PR:-?}" "$(api GET "/repos/adw/notes/issues/${PR:-0}/comments" | jq -c '[.[].body[0:120]]')"
URL="$(first_attachment "${PR:-0}")"
[ -n "$URL" ] && [ "$(curl -s -o /dev/null -w '%{http_code} %{content_type}' "$URL")" = "200 image/png" ] \
  && ok "the embedded screenshot is served publicly as image/png (no login)" || bad "attachment $URL not publicly served"
SHOT="$(ls "$LAYER/artefacts/$A/screenshots/e2e-results/"*-home.png 2>/dev/null | head -1)"
[ -n "$SHOT" ] && cp "$SHOT" "$OUT/a-after.png" && ok "after-screenshot: $(basename "$SHOT")" || bad "no after-screenshot in artefacts/$A"
# 3000 px tall (not 2200): the before picture in the description pushes the Proof comment down
page_shot "$BASE/adw/notes/pulls/$PR" "$OUT/a-pr-page.png" 1300,3000 && ok "a-pr-page.png: the PR page in real Chrome" || bad "PR page screenshot"
[ "$before" = "$(fingerprint)" ] && ok "local app clone untouched (HEAD, branch, status)" || bad "local app clone changed"
echo "  HUMAN STEP: approve and merge in Forgejo"
api POST "/repos/adw/notes/pulls/$PR/merge" -d '{"Do":"merge","MergeMessageField":"Approved after looking at the screenshots"}' >/dev/null
api GET "/repos/adw/notes/pulls/$PR" | jq -e '.merged == true' >/dev/null && ok "PR #$PR merged by the human" || bad "human merge of PR #$PR"
api GET "/repos/adw/notes/raw/static/style.css?ref=main" | grep -qi '#1e293b' \
  && ok "main on the forge: static/style.css has the dark slate #1e293b" || bad "main has no #1e293b in static/style.css"
MAIN_OF[a]="$(api GET /repos/adw/notes/branches/main | jq -r .commit.id)"
COST_OF[a]="$(run_cost "$A")"; STEPS_OF[a]="$(agent_steps "$A")"
info "scenario A: ${SECONDS_OF[a]}s, \$${COST_OF[a]}, ${STEPS_OF[a]} agent steps"
echo "  MAINTAINER: pulls main into the local checkout (the only change to it, made by the script, not a run)"
git -C "$APP" pull -q --ff-only origin main && ok "local checkout at $(git -C "$APP" rev-parse --short HEAD)" || bad "pull"
fi

if [[ "$ONLY" == *b* ]]; then
echo "SCENARIO B  a new feature, two agent reviewers, shipped without a human"
before="$(fingerprint)"
N="$(api POST /repos/adw/notes/issues -d "$(jq -n --arg t "$PROMPT_B_TITLE" --arg b "$PROMPT_B_BODY" '{title: $t, body: $b}')" | jq -r .number)"
info "issue #$N filed; running ADW feature (class feature, zero-touch) with the real agent"
run_adw b feature feature "$N"; B="$RUN_ID"; ID_OF[b]="$B"; SECONDS_OF[b]=$RUN_SECONDS
STATE="$LAYER/agents/$B/state.json"
[ "$RUN_RC" = 0 ] && jq -e '.status == "passed"' "$STATE" >/dev/null 2>&1 \
  && ok "ADW run $B passed in ${RUN_SECONDS}s" || bad "ADW run for issue #$N (exit $RUN_RC)" "$(tail -8 "$OUT/b-run.log")"
jq -e '.review.rounds[-1].reviewers | length == 2 and all(.[]; .success == true and (.artefacts_reviewed | length > 0))' "$STATE" >/dev/null 2>&1 \
  && ok "two independent reviewers, both success, both list artefacts_reviewed" || bad "reviewer verdicts" "$(jq -c '[.review.rounds[]?.reviewers[]? | {success, summary: .summary[0:160]}]' "$STATE" 2>/dev/null)"
jq -e '.review.rounds[-1].reviewers | all(.[]; any(.artefacts_reviewed[]; test("\\.keyframes/")) and any(.artefacts_reviewed[]; test("screenshots/")))' "$STATE" >/dev/null 2>&1 \
  && ok "each reviewer opened video keyframes AND screenshots" || bad "reviewers did not list keyframes and screenshots" "$(jq -c '[.review.rounds[-1].reviewers[]?.artefacts_reviewed]' "$STATE" 2>/dev/null)"
jq -e '.ship.gate.action == "merge" and .ship.shipped == true and .ship.action == "merge"' "$STATE" >/dev/null 2>&1 \
  && ok "gate decided merge: $(jq -r '.ship.gate.diff_lines' "$STATE") diff lines, checks re-run green" || bad "gate" "$(jq -c '.ship | {action, shipped, gate, reasons}' "$STATE" 2>/dev/null)"
PR="$(jq -r '.ship.pr.number // empty' "$STATE" 2>/dev/null)"; PR_OF[b]="$PR"; COMMIT_OF[b]="$(jq -r '.ship.commit // ""' "$STATE" 2>/dev/null)"
api GET "/repos/adw/notes/pulls/${PR:-0}" | jq -e '.merged == true' >/dev/null && ok "PR #$PR merged on the forge without a human" || bad "PR #${PR:-?} not merged"
pr_has_proof "${PR:-0}" && ok "PR #$PR carries the Proof comment (screenshots + video keyframes)" || bad "Proof comment on PR #${PR:-?}"
rm -rf "$TMP/main-b" && git clone -q "http://127.0.0.1:$PORT/adw/notes.git" "$TMP/main-b" 2>/dev/null
grep -rqs --include='*.html' --include='*.js' --include='*.py' "No notes match" "$TMP/main-b/templates" "$TMP/main-b/static" "$TMP/main-b/app.py" \
  && ok "main on the forge contains the feature ('No notes match')" || bad "feature not on main"
grep -qs "No notes match" "$TMP/main-b/e2e/scenario.py" && grep -Eqs '"type":' "$TMP/main-b/e2e/scenario.py" \
  && [ "$(grep -c '"screenshot"' "$TMP/main-b/e2e/scenario.py")" -gt 2 ] \
  && ok "e2e/scenario.py on main was extended to demonstrate the search" || bad "scenario not extended"
grep -qi '#1e293b' "$TMP/main-b/static/style.css" && ok "scenario A's dark theme is still on main" || bad "dark theme lost on main"
FRAMES=("$LAYER/artefacts/$B/recording/e2e-results/session.keyframes/"*.png)
[ "${#FRAMES[@]}" = 8 ] && [ -s "${FRAMES[0]}" ] && ok "8 keyframes extracted from the session video" || bad "keyframes: ${#FRAMES[@]}"
VIDEO="$LAYER/artefacts/$B/recording/e2e-results/session.webm"
if [ -s "$VIDEO" ]; then
  cp "$VIDEO" "$OUT/b-feature.webm"
  ffmpeg -loglevel error -y -i "$VIDEO" -c:v libx264 -pix_fmt yuv420p -movflags +faststart -crf 26 "$OUT/b-feature.mp4"
  for fps in 8 6 4; do
    ffmpeg -loglevel error -y -i "$VIDEO" -vf "fps=$fps,scale=800:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=bayer" "$OUT/b-feature.gif"
    [ "$(stat -c %s "$OUT/b-feature.gif")" -le 1572864 ] && break
  done
  DUR="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT/b-feature.mp4")"
  ok "video: $(printf '%.1f' "$DUR")s; mp4 $(du -k "$OUT/b-feature.mp4" | cut -f1) KB, gif $(du -k "$OUT/b-feature.gif" | cut -f1) KB"
else bad "no session video in artefacts/$B"; fi
mkdir -p "$OUT/b-keyframes" && cp "${FRAMES[@]}" "$OUT/b-keyframes/" 2>/dev/null
jq '{gate: .ship.gate, shipped: .ship.shipped, target: .ship.target, combined_verdict: .review.verdict,
     rounds: (.review.rounds | length), reviewers: .review.rounds[-1].reviewers}' "$STATE" 2>/dev/null | scrub > "$OUT/b-verdicts.json"
page_shot "$BASE/adw/notes/pulls/$PR" "$OUT/b-pr-page.png" && ok "b-pr-page.png: the merged PR page in real Chrome" || bad "PR page screenshot"
[ "$before" = "$(fingerprint)" ] && ok "local app clone untouched (HEAD, branch, status)" || bad "local app clone changed"
MAIN_OF[b]="$(api GET /repos/adw/notes/branches/main | jq -r .commit.id)"
COST_OF[b]="$(run_cost "$B")"; STEPS_OF[b]="$(agent_steps "$B")"
info "scenario B: ${SECONDS_OF[b]}s, \$${COST_OF[b]}, ${STEPS_OF[b]} agent steps"
fi

echo "OUTPUTS"
scenario_json() { local l="$1" title="$2" body="$3" adw="$4" klass="$5" review="$6"
  jq -n --arg title "$title" --arg body "$body" --arg adw "$adw" --arg klass "$klass" --arg review "$review" \
    --arg id "${ID_OF[$l]:-}" --argjson seconds "${SECONDS_OF[$l]:-0}" --argjson cost "${COST_OF[$l]:-0}" \
    --argjson steps "${STEPS_OF[$l]:-0}" --arg pr "${PR_OF[$l]:-}" --arg commit "${COMMIT_OF[$l]:-}" --arg main "${MAIN_OF[$l]:-}" \
    '{prompt: {issue_title: $title, issue_body: $body}, adw: $adw, class: $klass, review: $review, run_id: $id,
      wall_clock_seconds: $seconds, agent_cost_usd: ($cost * 10000 | round / 10000), agent_steps: $steps,
      pull_request: ($pr | tonumber? // null), shipped_commit: $commit, main_after: $main}'; }
jq -n --arg model "claude-opus-5-5" --argjson a "$(scenario_json a "$PROMPT_A_TITLE" "$PROMPT_A_BODY" ui-change chore human)" \
  --argjson b "$(scenario_json b "$PROMPT_B_TITLE" "$PROMPT_B_BODY" feature feature "auto, 2 reviewers, ship merge")" \
  --arg date "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg forgejo "$(api GET /version | jq -r .version)" \
  '{date: $date, model: $model, forgejo: $forgejo, scenario_a: $a, scenario_b: $b,
    total_agent_cost_usd: (($a.agent_cost_usd + $b.agent_cost_usd) * 10000 | round / 10000)}
   | .scenario_a.prompt.issue_attachment = "before.png, embedded at the end of the issue body as \"Current look, before the change\""' > "$OUT/summary.json"
for png in "$OUT"/*-pr-page.png; do   # keep the committed outputs small: PR pages at 1000 px wide
  [ -f "$png" ] && [ "$(stat -c %s "$png")" -gt 700000 ] && ffmpeg -loglevel error -y -i "$png" -vf scale=1000:-1 "$png.small.png" && mv "$png.small.png" "$png"
done
if [ "$fail" -eq 0 ] && [ "$ONLY" = ab ]; then
  rm -rf "$OUT_FINAL" && mkdir -p "$OUT_FINAL" && cp -r "$OUT"/. "$OUT_FINAL"/ \
    && ok "outputs in ${OUT_FINAL#$ROOT/} ($(du -sh "$OUT_FINAL" | cut -f1))"
else info "outputs NOT copied to ${OUT_FINAL#$ROOT/} (failures or a single scenario); they are in $OUT (AL_KEEP=1 keeps them)"; fi
echo; echo "webdemo live: $pass passed, $fail failed"; [ "$fail" -eq 0 ]
