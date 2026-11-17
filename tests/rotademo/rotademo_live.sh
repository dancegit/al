#!/usr/bin/env bash
# Rota demo, end to end, with a REAL agent (Claude Opus 5.5 inside the run's container) against a
# throwaway local Forgejo. Same pattern as tests/webdemo/webdemo_live.sh. All data is synthetic.
#   SETUP       Forgejo on a free loopback port, user + token + two PUBLIC repos (adw/rota for A and B,
#               adw/rota-c for C, both starting from tests/rotademo/app), two generated layers,
#               al-rota-demo:latest built if missing, the planted change rendered and checked,
#               `al verify` of test/isolation/e2e, BEFORE screenshot
#   SCENARIO A  a domain rule (11 hours rest) -> feature ADW, zero-touch: two reviewers -> gate -> merge
#   SCENARIO B  a schema change (pay band) through the SAME ADW. The migration is a protected path, so
#               either the run stops (agent: REQUIRES HUMAN) and hands its work over as an open
#               "[needs human]" PR, or the gate stops at protected-path:migrations/ -> HUMAN STEP (labelled):
#               a person adds the migration the PR asks for, runs test/isolation/e2e, merges
#   SCENARIO C  a PLANTED DEFECT (scripted build step that passes every check but misses an acceptance
#               criterion) -> real reviewers -> patch loop -> re-review -> merge
#   OUTPUTS     docs/demo/rotademo/ (per scenario, only for scenarios whose checks all passed)
#   TEARDOWN    Forgejo container, al-rota-demo-* containers/pods, temp dir (unless AL_KEEP=1)
# Needs: podman, uv, racket, curl, jq, git, ffmpeg, google-chrome, python3, ~/.claude/.credentials.json,
#        docker.io/library/python:3.12-slim and codeberg.org/forgejo/forgejo:11 (pulled if missing).
# Costs real money: roughly $4-7 of agent usage per full run (see tests/rotademo/README.md).
# AL_ROTADEMO_ONLY=a|b|c|ab|... runs some scenarios. AL_ROTADEMO_DRY=1 runs every agent step on the
# mock agent (free; the scenario assertions about agent behaviour then fail, by design), and
# AL_ROTADEMO_MOCK_DIR=<dir> copies scripted mock responses into both layers for such dry runs.
# AL_ROTADEMO_ATTEMPTS=<json file> is embedded in summary.json as the record of attempts and retries.
set -uo pipefail
export LC_ALL=C.UTF-8
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HERE="$ROOT/tests/rotademo"
OUT_FINAL="${AL_ROTADEMO_OUT:-$ROOT/docs/demo/rotademo}"
# videos are not kept in the tree: they go to the ignored media folder under their release asset names (docs/media.json)
MEDIA="${AL_MEDIA_DIR:-$ROOT/media/media-v1}"
ONLY="${AL_ROTADEMO_ONLY:-abc}"
DRY="${AL_ROTADEMO_DRY:-0}"
NAME="al-rotademo-forgejo"; IMAGE="codeberg.org/forgejo/forgejo:11"
LAYER_NAME="rota-demo"; BOX_IMAGE="al-$LAYER_NAME:latest"; BASE_IMAGE="docker.io/library/python:3.12-slim"
PORT="${AL_ROTADEMO_PORT:-$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')}"
TMP="$(mktemp -d /tmp/al-rotademo.XXXXXX)"; BASE="http://127.0.0.1:$PORT"
APP="$TMP/rota"; LAYER="$TMP/rota.al"; APP_C="$TMP/rota-c"; LAYER_C="$TMP/rota-c.al"; OUT="$TMP/out"; mkdir -p "$OUT"
TODAY="2026-03-02"

PROMPT_A_TITLE="Enforce 11 hours of rest between a worker's shifts"
PROMPT_A_BODY="Ward managers can book a worker onto a shift that starts soon after their previous shift ends, for example a night shift ending at 07:30 followed by a day shift starting at 07:30. Staff must get at least 11 hours of rest between shifts, and the rota should stop a manager from breaking that rule by accident.

## Acceptance criteria

- Assigning a worker to a shift is refused when the shift starts less than 11 hours after the end of that worker's previous shift, or ends less than 11 hours before the start of their next shift. Exactly 11 hours of rest is allowed.
- The rule is a business rule in rules.py, applied the same way on the shifts board and in the API.
- \`POST /api/shifts/<id>/assign\` answers HTTP 409 with the reason \"Needs 11 hours rest\" in the JSON error.
- On the shifts board, a worker who would not get 11 hours of rest is shown in the assign dropdown as unavailable, with the reason \"Needs 11 hours rest\", and cannot be selected.
- Only shifts of the manager's own organisation are considered (data access through the organisation scope).
- The existing rules (role, registration expiry, overlapping shifts) keep working.
- Tests cover the rule in both directions and at the 11-hour boundary, the API refusal and the board; the e2e scenario shows a worker marked \"Needs 11 hours rest\" in the dropdown."

PROMPT_B_TITLE="Record each worker's pay band and show it on the workers page"
PROMPT_B_BODY="Payroll needs each worker's pay band (for example \"Band 5\") next to their registration details. The rota does not store it yet.

## Acceptance criteria

- Each worker has a pay band stored in the database. This is a schema change: add it as a new numbered migration file in migrations/; an applied migration is never edited.
- The synthetic seed data gives every seeded worker a pay band (healthcare assistants Band 3, registered nurses Band 5 or Band 6).
- The workers page shows a \"Pay band\" column.
- \`GET /api/workers\` includes each worker's \`pay_band\`.
- Managers still see only their own organisation's workers.
- Tests cover the API field and the page column; the e2e scenario shows the column on the workers page."

PROMPT_C_TITLE="Mark workers whose registration has expired, and stop offering them for shifts"
PROMPT_C_BODY="A worker whose professional registration has expired may not work, and the server already refuses to assign them. But a manager only finds out after trying: nothing on the workers page or in the assign dropdown says that the registration has expired.

## Acceptance criteria

- On the workers page, an \"Expired\" badge is shown next to every worker whose registration has expired, and next to nobody else.
- In the assign dropdown on the shifts board, an expired worker is shown with an \"Expired\" label next to their name.
- An expired worker cannot be selected in the assign dropdown.
- The server-side refusal stays in place (business rule and API unchanged).
- Tests cover the badge and the dropdown; the e2e scenario shows both: the badge on the workers page and the expired worker unavailable in the dropdown."

pass=0; fail=0; TOKEN=""
declare -A PASS_OF FAIL_OF
CUR=setup
ok()  { pass=$((pass+1)); PASS_OF[$CUR]=$(( ${PASS_OF[$CUR]:-0} + 1 )); printf '  ok    %s\n' "$1"; }
bad() { fail=$((fail+1)); FAIL_OF[$CUR]=$(( ${FAIL_OF[$CUR]:-0} + 1 )); printf '  FAIL  %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "${2//$'\n'/$'\n'        }"; }
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
fingerprint() { git -C "$1" rev-parse HEAD; git -C "$1" branch --show-current; git -C "$1" status --porcelain; }
# Real Chrome on the host, as a person opening the PR would see it (the repositories are public: no login).
page_shot() { google-chrome --headless=new --disable-gpu --no-first-run --user-data-dir="$TMP/chrome" --hide-scrollbars \
  --window-size="${3:-1300,2400}" --virtual-time-budget=10000 --screenshot="$2" "$1" >/dev/null 2>&1; [ -s "$2" ]; }
# The demo image, hardened like a run container, on a directory of the host (probes, captures, crops).
box() { local dir="$1"; shift
  podman run --rm --userns=keep-id --cap-drop=ALL --security-opt=no-new-privileges --security-opt label=disable \
    ${BOX_NET:+--network=host} -e HOME=/tmp/home -e "ROTA_TODAY=$TODAY" \
    -v "$dir:$dir" -v "$HERE/helpers:/helpers:ro" -w "$dir" "$BOX_IMAGE" "$@"; }
pillow() { uv run -q --with pillow python "$@"; }
# A commit of a forge repository, exported (git archive) into a fresh directory.
export_ref() { local repo="$1" ref="$2" dir="$3" clone="$TMP/clone-${1//\//-}"
  [ -d "$clone" ] || git clone -q "http://127.0.0.1:$PORT/$repo.git" "$clone" 2>/dev/null
  git -C "$clone" fetch -q origin '+refs/heads/*:refs/remotes/origin/*' '+refs/pull/*/head:refs/remotes/pull/*' 2>/dev/null
  # a branch name means the forge's current branch, never the clone's (possibly stale) local one
  git -C "$clone" rev-parse -q --verify "refs/remotes/origin/$ref" >/dev/null && ref="refs/remotes/origin/$ref"
  rm -rf "$dir" && mkdir -p "$dir" && git -C "$clone" archive "$ref" | tar -x -C "$dir"; }
clone_of() { echo "$TMP/clone-${1//\//-}"; }
# Sum of total_cost_usd over the `result` events of every agent step of a run, and the number of agent steps
# (real agent steps only: a mock step's raw.jsonl has no result event).
run_cost() { find "$1/agents/$2" -name raw.jsonl -print0 | xargs -0 -r cat | jq -R -s \
  '[split("\n")[] | fromjson? | select(type == "object" and .type == "result") | (.total_cost_usd // 0)] | add // 0'; }
agent_steps() { local n=0 f; for f in $(find "$1/agents/$2" -name raw.jsonl); do grep -q '"type":"result"' "$f" && n=$((n+1)); done; echo "$n"; }
# Run an ADW for an issue in a layer; streams concise progress; sets RUN_ID, RUN_RC, RUN_SECONDS.
run_adw() { local letter="$1" layer="$2" adw="$3" klass="$4" issue="$5" started
  started=$(date +%s)
  (cd "$layer" && PYTHONUNBUFFERED=1 uv run -q adws/dispatch.py --adw "$adw" --source "issue:$issue" --class "$klass" \
     $([ "$DRY" = 1 ] && echo --dry-run)) 2>&1 \
    | tee "$TMP/$letter-run.log" \
    | grep --line-buffered -E '\] (step .* (start|passed|failed|skipped)|gate:|ship:|review verdict|review round|warning:)' \
    | sed -u -E 's/^/        /; s/(passed|failed): (.{0,110}).*/\1: \2/'
  RUN_RC=${PIPESTATUS[0]}; RUN_SECONDS=$(( $(date +%s) - started ))
  RUN_ID="$(grep -o -m1 '\[adw [0-9a-f]\{8\}\]' "$TMP/$letter-run.log" | tr -d '[]' | cut -d' ' -f2)"
  scrub < "$TMP/$letter-run.log" > "$OUT/$letter-run.log"
}
file_issue() { api POST "/repos/$1/issues" -d "$(jq -n --arg t "$2" --arg b "$3" '{title: $t, body: $b}')" | jq -r .number; }
pr_has_proof() { api GET "/repos/$1/issues/$2/comments" | jq -e '[.[] | select(.body | contains("Proof from run")
    and test("!\\[[^]]*\\]\\(http[^)]*/attachments/"))] | length == 1' >/dev/null; }
# Video outputs for a run: demo-rotademo-<letter>-<name>.mp4 (H.264 yuv420p) in the media folder; <letter>-<name>.gif
# (800 px, <= 1.5 MB) and the keyframes sheet in $OUT.
video_outputs() { local letter="$1" name="$2" layer="$3" id="$4" video frames dur
  video="$layer/artefacts/$id/recording/e2e-results/session.webm"
  frames=("$layer/artefacts/$id/recording/e2e-results/session.keyframes/"*.png)
  [ "${#frames[@]}" = 8 ] && [ -s "${frames[0]}" ] && ok "8 keyframes extracted from the session video" || bad "keyframes: ${#frames[@]}"
  if [ -s "$video" ]; then
    mkdir -p "$MEDIA"
    ffmpeg -loglevel error -y -i "$video" -c:v libx264 -pix_fmt yuv420p -movflags +faststart -crf 28 "$MEDIA/demo-rotademo-$letter-$name.mp4"
    for try in 6:96 5:64 4:64 3:48 2:48; do   # fps:colours, until the GIF fits in 1.5 MB
      ffmpeg -loglevel error -y -i "$video" -vf "fps=${try%:*},scale=800:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=${try#*:}:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle" "$OUT/$letter-$name.gif"
      [ "$(stat -c %s "$OUT/$letter-$name.gif")" -le 1572864 ] && break
    done
    dur="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$MEDIA/demo-rotademo-$letter-$name.mp4")"
    [ "$(stat -c %s "$OUT/$letter-$name.gif")" -le 1572864 ] && [ "$(stat -c %s "$MEDIA/demo-rotademo-$letter-$name.mp4")" -le 1572864 ] \
      && ok "video: $(printf '%.1f' "$dur")s; mp4 $(du -k "$MEDIA/demo-rotademo-$letter-$name.mp4" | cut -f1) KB, gif $(du -k "$OUT/$letter-$name.gif" | cut -f1) KB (each <= 1.5 MB)" \
      || bad "video outputs larger than 1.5 MB"
  else bad "no session video in artefacts/$id"; fi
  [ "${#frames[@]}" -gt 0 ] && pillow "$HERE/helpers/sheet.py" "$OUT/$letter-keyframes-sheet.png" "${frames[@]}" >/dev/null \
    && ok "$letter-keyframes-sheet.png: the 8 keyframes the reviewers read, 4x2" || bad "keyframes sheet"
}
# Readable crops of a PR page (Playwright in the demo image, on the host network), stacked with Pillow.
pr_crop() { local url="$1" out="$2"; shift 2
  mkdir -p "$TMP/crops" && BOX_NET=1 box "$TMP/crops" python /helpers/pr_crop.py "$url" "$TMP/crops/$(basename "$out" .png)" "$@" > "$TMP/crops/log" 2>&1 \
    && pillow "$HERE/helpers/stack.py" "$out" "$TMP/crops/$(basename "$out" .png)"-*.png >/dev/null; }
verdicts_json() { jq '{gate: .ship.gate, shipped: .ship.shipped, target: .ship.target, combined_verdict: .review.verdict,
     rounds: (.review.rounds | length), reviewers: .review.rounds[-1].reviewers}' "$1" 2>/dev/null | scrub; }
# Did a reviewer of the run open the layer definition or the build step's records (which would tell it
# about the planted change)? Lists such tool calls; empty means none.
reviewer_peeks() { find "$1/agents/$2" -path '*review*' -name raw.jsonl -print0 | xargs -0 -r cat | jq -R -r \
  'fromjson? | select(.type == "assistant") | .message.content[]? | select(.type == "tool_use") | .input | tostring' \
  | grep -E 'planted|planter|agentic\.rkt|agentic-layer\.json|/build/(raw|output|prompt)|state\.json' | head -5; }

echo "SETUP"
for tool in podman uv racket curl jq git ffmpeg ffprobe google-chrome python3; do command -v "$tool" >/dev/null || { echo "missing tool: $tool"; exit 2; }; done
[ -f "$HOME/.claude/.credentials.json" ] || { echo "no ~/.claude/.credentials.json: log in with the claude CLI first"; exit 2; }
[ "$DRY" = 1 ] && info "DRY RUN: every agent step runs on the mock agent (no cost)"
podman rm -f "$NAME" >/dev/null 2>&1
podman image exists "$IMAGE" || podman pull -q "$IMAGE" >/dev/null || { echo "cannot obtain $IMAGE"; exit 2; }
podman image exists "$BASE_IMAGE" || { echo "base image $BASE_IMAGE not present locally (podman pull it once)"; exit 2; }
podman run -d --name "$NAME" -p "127.0.0.1:$PORT:3000" \
  -e FORGEJO__security__INSTALL_LOCK=true -e FORGEJO__database__DB_TYPE=sqlite3 \
  -e FORGEJO__server__ROOT_URL="$BASE/" -e FORGEJO__log__LEVEL=Warn "$IMAGE" >/dev/null || { echo "forgejo did not start"; exit 2; }
for _ in $(seq 1 60); do curl -fsS "$BASE/api/v1/version" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "$BASE/api/v1/version" >/dev/null || { echo "forgejo not reachable"; exit 2; }
# throwaway account in a container that is deleted at exit; the password is fake by design
podman exec -u git "$NAME" forgejo admin user create --username adw --password 'fake-Passw0rd-1!' --email adw@example.org --admin --must-change-password=false >/dev/null
TOKEN="$(podman exec -u git "$NAME" forgejo admin user generate-access-token --username adw --token-name al --scopes all --raw | tail -1)"
[ -n "$TOKEN" ] || { echo "no token"; exit 2; }
for repo in rota rota-c; do
  api POST /user/repos -d "$(jq -n --arg n "$repo" '{name: $n, default_branch: "main", private: false,
    description: "Rota: the al healthcare-staffing demo app (synthetic data)"}')" | jq -e '.private == false' >/dev/null \
    && ok "public repository adw/$repo on $BASE" || { bad "repository adw/$repo"; exit 1; }
done
for pair in "rota:$APP" "rota-c:$APP_C"; do
  repo="${pair%%:*}"; dir="${pair#*:}"
  cp -r "$HERE/app" "$dir" && rm -rf "$dir/e2e-results" "$dir/instance" && find "$dir" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null
  (cd "$dir" && git init -q -b main && git add -A && git -c user.name=adw -c user.email=adw@example.org commit -qm "Rota app" \
    && git remote add origin "http://adw:$TOKEN@127.0.0.1:$PORT/adw/$repo.git" && git push -q origin main) \
    && ok "app pushed to adw/$repo ($(git -C "$dir" ls-files | wc -l | tr -d ' ') files)" || { bad "push to adw/$repo"; exit 1; }
done
PRISTINE="$(git -C "$APP" rev-parse HEAD)"
for pair in "rota:$LAYER" "rota-c:$LAYER_C"; do
  repo="${pair%%:*}"; layer="${pair#*:}"
  mkdir -p "$layer" && sed -e "s#@FORGE_HOST@#$BASE#" -e "s#@REPO@#adw/$repo#" -e "s#@APP_DIR@#$repo#" "$HERE/layer.rkt.in" > "$layer/agentic.rkt"
  "$ROOT/tools/al" gen "$layer" >/dev/null && ok "layer for adw/$repo generated (tools/al gen)" || { bad "layer generation ($repo)"; exit 1; }
  [ -n "${AL_ROTADEMO_MOCK_DIR:-}" ] && mkdir -p "$layer/mock" && cp -r "$AL_ROTADEMO_MOCK_DIR"/. "$layer/mock/"
done
# The planted change: rendered from planted.patch against the pristine app (it must still apply), into the
# mock agent's script path <layer>/<script-dir>/<adw>/<step>.json of the planter agent.
mkdir -p "$LAYER_C/planted/planted"
python3 "$HERE/planted/render.py" "$HERE/app" "$HERE/planted/planted.patch" "$LAYER_C/planted/planted/build.json" "$ROOT/kit/runtime" > "$TMP/render.log" 2>&1 \
  && ok "planted change rendered for the planter agent: $(tail -1 "$TMP/render.log" | sed 's/^render: //')" || { bad "planted change" "$(cat "$TMP/render.log")"; exit 1; }
if ! podman image exists "$BOX_IMAGE"; then
  info "building $BOX_IMAGE once (Chromium + Claude CLI download: several minutes)"
  (cd "$LAYER" && podman build -t "$BOX_IMAGE" -f env/Containerfile env/ > "$TMP/image-build.log" 2>&1) \
    || { bad "image build" "$(tail -5 "$TMP/image-build.log")"; exit 1; }
fi
ok "container image $BOX_IMAGE present (reused between runs)"
export AL_ROTADEMO_FORGEJO_TOKEN="$TOKEN"

echo "BEFORE  the untouched app"
"$ROOT/tools/al" verify "$LAYER" --cmd test --cmd isolation --cmd e2e > "$TMP/verify.log" 2>&1 \
  && ok "al verify: test, isolation and e2e pass on main in the layer's container" || bad "al verify" "$(tail -8 "$TMP/verify.log")"
# al verify discards its throwaway worktree (and e2e-results/ with it), so the BEFORE picture comes from
# the same capture run once more, in the same image, on an export of main, with the run's hardening flags.
export_ref adw/rota main "$TMP/before"
box "$TMP/before" python e2e/capture.py > "$TMP/before.log" 2>&1 \
  && cp "$TMP/before/e2e-results/02-shifts-board.png" "$OUT/before.png" && ok "before.png: the shifts board ($(grep -c '^ok' "$TMP/before.log") e2e steps passed)" \
  || bad "before capture" "$(tail -6 "$TMP/before.log")"
if [[ "$ONLY" == *c* ]]; then
  # The planted change must pass every automated check and leave the defect in place, or scenario C
  # would prove nothing. Checked here, for free, on an export of the pristine app with the patch applied.
  export_ref adw/rota-c main "$TMP/planted-check" && (cd "$TMP/planted-check" && git apply "$HERE/planted/planted.patch")
  box "$TMP/planted-check" bash -c 'python -m pytest -q tests && python e2e/capture.py' > "$TMP/planted-check.log" 2>&1 \
    && ok "the planted change passes test, isolation and e2e ($(grep -Eo '[0-9]+ passed' "$TMP/planted-check.log" | head -1))" \
    || bad "the planted change does not pass the checks" "$(tail -6 "$TMP/planted-check.log")"
  box "$TMP/planted-check" python /helpers/probe_dropdown.py > "$TMP/planted-probe.json" 2>&1
  jq -e '.offered and .selectable and .workers_badge' "$TMP/planted-probe.json" >/dev/null \
    && ok "...and leaves the defect: badge on the workers page, but Chloe Martin (expired) still selectable in the dropdown" \
    || bad "planted defect not as designed" "$(cat "$TMP/planted-probe.json")"
fi

declare -A SECONDS_OF COST_OF STEPS_OF PR_OF COMMIT_OF MAIN_OF ID_OF ROUNDS_OF GATE_OF REASONS_OF PRSTATE_OF OUTCOME_OF NOTE_OF EXTRA_OF

if [[ "$ONLY" == *a* ]]; then
CUR=a
echo "SCENARIO A  a domain rule (11 hours rest), shipped without a human"
before="$(fingerprint "$APP")"
N="$(file_issue adw/rota "$PROMPT_A_TITLE" "$PROMPT_A_BODY")"
info "issue #$N filed; running ADW feature (class feature, zero-touch) with the real agent"
run_adw a "$LAYER" feature feature "$N"; A="$RUN_ID"; ID_OF[a]="$A"; SECONDS_OF[a]=$RUN_SECONDS
STATE="$LAYER/agents/$A/state.json"
[ "$RUN_RC" = 0 ] && jq -e '.status == "passed"' "$STATE" >/dev/null 2>&1 \
  && ok "ADW run $A passed in ${RUN_SECONDS}s" || bad "ADW run for issue #$N (exit $RUN_RC)" "$(tail -8 "$OUT/a-run.log")"
jq -e '[.checks.test.exit, .checks.isolation.exit, .checks.e2e.exit] == [0, 0, 0]' "$STATE" >/dev/null 2>&1 \
  && ok "test, isolation and e2e checks passed in the container" || bad "checks" "$(jq -c .checks "$STATE" 2>/dev/null)"
jq -e '.review.rounds[-1].reviewers | length == 2 and all(.[]; .success == true and (.artefacts_reviewed | length > 0))' "$STATE" >/dev/null 2>&1 \
  && ok "two independent reviewers, both success, both list artefacts_reviewed" || bad "reviewer verdicts" "$(jq -c '[.review.rounds[]?.reviewers[]? | {success, summary: .summary[0:200]}]' "$STATE" 2>/dev/null)"
jq -e '.review.rounds[-1].reviewers | all(.[]; any(.artefacts_reviewed[]; test("\\.keyframes/")) and any(.artefacts_reviewed[]; test("screenshots/")))' "$STATE" >/dev/null 2>&1 \
  && ok "each reviewer opened video keyframes AND screenshots" || bad "reviewers did not list keyframes and screenshots" "$(jq -c '[.review.rounds[-1].reviewers[]?.artefacts_reviewed]' "$STATE" 2>/dev/null)"
jq -e '.ship.gate.action == "merge" and .ship.gate.reasons == [] and .ship.shipped == true and .ship.action == "merge"' "$STATE" >/dev/null 2>&1 \
  && ok "gate decided merge with no reasons ($(jq -r '.ship.gate.diff_lines' "$STATE") diff lines; test, isolation, e2e re-run green)" \
  || bad "gate" "$(jq -c '.ship | {action, shipped, gate, reasons}' "$STATE" 2>/dev/null)"
PR="$(jq -r '.ship.pr.number // empty' "$STATE" 2>/dev/null)"; PR_OF[a]="$PR"; COMMIT_OF[a]="$(jq -r '.ship.commit // ""' "$STATE" 2>/dev/null)"
api GET "/repos/adw/rota/pulls/${PR:-0}" | jq -e '.merged == true' >/dev/null && ok "PR #$PR merged on the forge without a human" || bad "PR #${PR:-?} not merged"
pr_has_proof adw/rota "${PR:-0}" && ok "PR #$PR carries the Proof comment (screenshots + video keyframes)" || bad "Proof comment on PR #${PR:-?}"
export_ref adw/rota main "$TMP/main-a"
box "$TMP/main-a" python /helpers/probe_rest_rule.py > "$TMP/a-probe.log" 2>&1 \
  && ok "main on the forge enforces the rule (independent probe: API 409 both directions, 12 h allowed, board shows 'Needs 11 hours rest')" \
  || bad "the rule is not (fully) on main" "$(grep -v '^{' "$TMP/a-probe.log")"
grep -q "11 hours" "$TMP/main-a/e2e/scenario.py" && ok "e2e/scenario.py on main demonstrates the rule" || bad "scenario not extended for the rule"
video_outputs a rest-rule "$LAYER" "$A"
verdicts_json "$STATE" > "$OUT/a-verdicts.json"
page_shot "$BASE/adw/rota/pulls/$PR" "$OUT/a-pr-page.png" && ok "a-pr-page.png: the merged PR page in real Chrome" || bad "PR page screenshot"
pr_crop "$BASE/adw/rota/pulls/$PR" "$OUT/a-pr-merged.png" header "Review verdict..Why a human is needed" \
  && ok "a-pr-merged.png: title + Merged state + the reviewers' verdict" || bad "a-pr-merged.png" "$(cat "$TMP/crops/log" 2>/dev/null)"
[ "$before" = "$(fingerprint "$APP")" ] && ok "local app clone untouched (HEAD, branch, status)" || bad "local app clone changed"
MAIN_OF[a]="$(api GET /repos/adw/rota/branches/main | jq -r .commit.id)"
COST_OF[a]="$(run_cost "$LAYER" "$A")"; STEPS_OF[a]="$(agent_steps "$LAYER" "$A")"; ROUNDS_OF[a]="$(jq '.review.rounds | length' "$STATE" 2>/dev/null || echo 0)"
GATE_OF[a]="$(jq -r '.ship.gate.action // ""' "$STATE" 2>/dev/null)"; REASONS_OF[a]="$(jq -c '.ship.gate.reasons // []' "$STATE" 2>/dev/null || echo '[]')"
PRSTATE_OF[a]="$(api GET "/repos/adw/rota/pulls/${PR:-0}" | jq -r 'if .merged then "merged" else (.state // "none") end')"; OUTCOME_OF[a]="merged by the gate"
info "scenario A: ${SECONDS_OF[a]}s, \$${COST_OF[a]}, ${STEPS_OF[a]} agent steps, ${ROUNDS_OF[a]} review round(s)"
echo "  MAINTAINER: pulls main into the local checkout (the only change to it, made by the script, not a run)"
git -C "$APP" pull -q --ff-only origin main && ok "local checkout at $(git -C "$APP" rev-parse --short HEAD)" || bad "pull"
fi

if [[ "$ONLY" == *b* ]]; then
CUR=b
echo "SCENARIO B  a schema change through the same zero-touch ADW: the gate stops for a human"
before="$(fingerprint "$APP")"
MAIN_BEFORE="$(api GET /repos/adw/rota/branches/main | jq -r .commit.id)"
N="$(file_issue adw/rota "$PROMPT_B_TITLE" "$PROMPT_B_BODY")"
info "issue #$N filed; running the SAME ADW feature (class feature, zero-touch) with the real agent"
run_adw b "$LAYER" feature feature "$N"; B="$RUN_ID"; ID_OF[b]="$B"; SECONDS_OF[b]=$RUN_SECONDS
STATE="$LAYER/agents/$B/state.json"
PR="$(jq -r '.ship.pr.number // empty' "$STATE" 2>/dev/null)"; PR_OF[b]="$PR"; COMMIT_OF[b]="$(jq -r '.ship.commit // ""' "$STATE" 2>/dev/null)"
GATE_OF[b]="$(jq -r '.ship.gate.action // ""' "$STATE" 2>/dev/null)"; REASONS_OF[b]="$(jq -c '.ship.gate.reasons // []' "$STATE" 2>/dev/null || echo '[]')"
[ "$(api GET /repos/adw/rota/branches/main | jq -r .commit.id)" = "$MAIN_BEFORE" ] \
  && ok "nothing reached main without a human (main still at ${MAIN_BEFORE:0:8})" || bad "main changed during the run"
if jq -e '.ship.gate.reasons // [] | any(startswith("protected-path:migrations/"))' "$STATE" >/dev/null 2>&1; then
  OUTCOME_OF[b]="agent implemented it including a migration; gate stopped at the protected path; human merged"
  [ "$RUN_RC" = 0 ] && jq -e '.status == "passed"' "$STATE" >/dev/null 2>&1 \
    && ok "ADW run $B passed in ${RUN_SECONDS}s (it ends with a pull request)" || bad "ADW run for issue #$N (exit $RUN_RC)" "$(tail -8 "$OUT/b-run.log")"
  jq -e '[.checks.test.exit, .checks.isolation.exit, .checks.e2e.exit] == [0, 0, 0]' "$STATE" >/dev/null 2>&1 \
    && ok "test, isolation and e2e checks passed in the container" || bad "checks" "$(jq -c .checks "$STATE" 2>/dev/null)"
  jq -e '.review.rounds[-1].reviewers | length == 2 and all(.[]; .success == true)' "$STATE" >/dev/null 2>&1 \
    && ok "both reviewers approved" || info "reviewers did not both approve: $(jq -c '[.review.rounds[-1].reviewers[]? | {success, summary: .summary[0:160]}]' "$STATE" 2>/dev/null)"
  jq -e '.ship.gate.action == "pr" and .ship.shipped == false and .ship.action == "pr"' "$STATE" >/dev/null 2>&1 \
    && ok "gate fell back to a human pull request: $(jq -r '.ship.gate.reasons | join(", ")' "$STATE")" || bad "gate" "$(jq -c '.ship | {action, shipped, gate}' "$STATE" 2>/dev/null)"
  api GET "/repos/adw/rota/pulls/${PR:-0}" | jq -e '.state == "open" and .merged == false and .base.ref == "main"' >/dev/null \
    && ok "PR #$PR is OPEN and not merged, waiting for a human" || bad "PR #${PR:-?} not open"
  api GET "/repos/adw/rota/pulls/${PR:-0}" | jq -e '.body | test("## Why a human is needed\\s+- [^\\n]*protected-path:migrations/")' >/dev/null \
    && ok "the PR body says why: '## Why a human is needed' lists protected-path:migrations/…" || bad "no reason in the PR body"
  api GET "/repos/adw/rota/pulls/${PR:-0}/files" | jq -e '[.[].filename] | any(test("^migrations/[0-9]{3}_.*\\.sql$"))' >/dev/null \
    && ok "the PR adds a new numbered migration: $(api GET "/repos/adw/rota/pulls/${PR:-0}/files" | jq -r '[.[].filename | select(startswith("migrations/"))] | join(", ")')" \
    || bad "no new migration file in the PR"
  api GET "/repos/adw/rota/pulls/${PR:-0}/files" | jq -e '[.[] | select(.filename | test("^migrations/00[12]_")) ] | length == 0' >/dev/null \
    && ok "no applied migration (001, 002) was edited" || bad "an applied migration was edited"
  pr_has_proof adw/rota "${PR:-0}" && ok "PR #$PR carries the Proof comment" || bad "Proof comment on PR #${PR:-?}"
  page_shot "$BASE/adw/rota/pulls/$PR" "$OUT/b-pr-page.png" && ok "b-pr-page.png: the open PR page in real Chrome (before the human step)" || bad "PR page screenshot"
  pr_crop "$BASE/adw/rota/pulls/$PR" "$OUT/b-pr-why-human.png" header "Review verdict..Artefacts" \
    && ok "b-pr-why-human.png: title + Open state + verdict + 'Why a human is needed'" || bad "b-pr-why-human.png" "$(cat "$TMP/crops/log" 2>/dev/null)"
  echo "  HUMAN STEP: a person reads the migration and merges in Forgejo"
  api POST "/repos/adw/rota/pulls/$PR/merge" -d '{"Do":"merge","MergeMessageField":"Schema change reviewed by a human: new migration only"}' >/dev/null
  api GET "/repos/adw/rota/pulls/$PR" | jq -e '.merged == true' >/dev/null && ok "PR #$PR merged by the human" || bad "human merge of PR #$PR"
  export_ref adw/rota main "$TMP/main-b"
  box "$TMP/main-b" python /helpers/probe_pay_band.py > "$TMP/b-probe.log" 2>&1 \
    && ok "main on the forge records and shows the pay band (independent probe)" || bad "pay band not (fully) on main" "$(grep -v '^{' "$TMP/b-probe.log")"
  if [[ "$ONLY" == *a* ]]; then
    box "$TMP/main-b" python /helpers/probe_rest_rule.py > "$TMP/b-probe-a.log" 2>&1 && ok "scenario A's rule is still on main" || bad "scenario A's rule lost on main"
  fi
  PRSTATE_OF[b]="merged by a human (after the gate stopped)"
elif jq -e '.ship.action == "handoff"' "$STATE" >/dev/null 2>&1; then
  # The run stopped before the gate (in earlier attempts: the agent refused to write the migration
  # under the protected path, so `test` could not pass) and HANDED ITS WORK OVER: a pull request
  # marked [needs human] that says why it stopped and what the agent asks a person to do.
  STEP="$(jq -r '.ship.stopped_at // "?"' "$STATE")"
  GATE_OF[b]="none: the run stopped at $STEP, before review and the gate"; REASONS_OF[b]="$(jq -c '.ship.reasons // []' "$STATE")"
  [ "$RUN_RC" != 0 ] && jq -e '.status == "failed"' "$STATE" >/dev/null 2>&1 \
    && ok "ADW run $B stopped at step '$STEP' after ${RUN_SECONDS}s and ended failed (exit $RUN_RC): a stopped run is not a success" \
    || bad "a stopped run must end failed with a non-zero exit (exit $RUN_RC, status $(jq -r .status "$STATE" 2>/dev/null))"
  jq -e '.ship.shipped == false and (.ship.reasons | length == 1) and (.ship.reasons[0] | startswith("run-stopped:")) and (.ship | has("handoff_error") | not)' "$STATE" >/dev/null 2>&1 \
    && ok "state.ship records the handoff: $(jq -r '.ship.reasons[0][0:140]' "$STATE")" || bad "state.ship" "$(jq -c '.ship' "$STATE" 2>/dev/null)"
  sed -n '/=== ADW summary ===/,$p' "$TMP/b-run.log" | grep -q "the work needs a human: .*/pulls/${PR:-none}" \
    && ok "the run's summary points the human at PR #$PR" || bad "the run's summary does not name the handoff PR"
  PRJ="$TMP/b-pr.json"; api GET "/repos/adw/rota/pulls/${PR:-0}" > "$PRJ"
  jq -e '.state == "open" and .merged == false and .base.ref == "main"' "$PRJ" >/dev/null 2>&1 \
    && ok "PR #$PR is OPEN and not merged" || bad "PR #${PR:-?} is not an open, unmerged PR"
  jq -e '.title | test("^(WIP: )?\\[needs human\\] ")' "$PRJ" >/dev/null 2>&1 \
    && ok "PR title marks it: $(jq -r .title "$PRJ")" || bad "PR title" "$(jq -r .title "$PRJ" 2>/dev/null)"
  BODY="$TMP/b-pr-body.md"; jq -r '.body // ""' "$PRJ" > "$BODY"
  WHY="$(grep -n -m1 '^## Why this stopped$' "$BODY" | cut -d: -f1)"; ASKS="$(grep -n -m1 '^## What the agent asks a human to do$' "$BODY" | cut -d: -f1)"
  SUMMARY_AT="$(grep -n -m1 '^## Summary$' "$BODY" | cut -d: -f1)"
  [ -n "$WHY" ] && [ -n "$ASKS" ] && [ -n "$SUMMARY_AT" ] && [ "$WHY" -lt "$ASKS" ] && [ "$ASKS" -lt "$SUMMARY_AT" ] \
    && ok "PR body: 'Why this stopped', then 'What the agent asks a human to do', then the usual sections" || bad "PR body sections missing or out of order"
  sed -n "${WHY:-1},$(( ${ASKS:-2} - 1 ))p" "$BODY" | grep -q "stopped at step \`$STEP\`" \
    && ok "'Why this stopped' names the failing step: $(sed -n "${WHY:-1},$(( ${ASKS:-2} - 1 ))p" "$BODY" | grep -m1 'stopped at step' | cut -c1-150)" \
    || bad "'Why this stopped' does not name step $STEP"
  sed -n "${ASKS:-1},$(( ${SUMMARY_AT:-2} - 1 ))p" "$BODY" > "$TMP/b-asks.md"
  MIGRATION="$(grep -Eo 'migrations/[0-9]{3}_[A-Za-z0-9_]+\.sql' "$TMP/b-asks.md" | head -1)"
  # the SQL the agent proposed: the first ```sql block of the request section, de-indented
  python3 - "$TMP/b-asks.md" > "$TMP/b-migration.sql" <<'PY'
import re, sys, textwrap
text = open(sys.argv[1], encoding="utf-8").read()
match = re.search(r"^([ \t]*)(`{3,})sql[ \t]*\n(.*?)^[ \t]*\2[ \t]*$", text, re.M | re.S)
sys.stdout.write(textwrap.dedent(match.group(3)) if match else "")
PY
  grep -qi 'ALTER TABLE' "$TMP/b-migration.sql" && [ -n "$MIGRATION" ] \
    && ok "'What the agent asks a human to do' names $MIGRATION and gives the SQL: $(grep -i -m1 'ALTER TABLE' "$TMP/b-migration.sql" | sed 's/^ *//' | cut -c1-90)" \
    || bad "no migration file name and ALTER TABLE SQL in the request section" "$(head -20 "$TMP/b-asks.md")"
  scrub < "$TMP/b-asks.md" > "$OUT/b-requires-human.txt"
  api GET "/repos/adw/rota/issues/$N/comments" | jq -e --arg pr "/pulls/$PR" \
    '[.[] | select((.body | startswith("[ADW-BOT]")) and (.body | contains($pr)) and (.body | contains("needs a human")))] | length >= 1' >/dev/null 2>&1 \
    && ok "issue #$N has the bot's comment pointing at PR #$PR" || bad "no bot comment on issue #$N linking PR #$PR"
  pr_has_proof adw/rota "${PR:-0}" && info "PR #$PR carries a Proof comment" || info "no Proof comment (the run stopped before any screenshot was collected)"
  page_shot "$BASE/adw/rota/pulls/$PR" "$OUT/b-pr-page.png" && ok "b-pr-page.png: the open [needs human] PR in real Chrome (before the human step)" || bad "PR page screenshot"
  pr_crop "$BASE/adw/rota/pulls/$PR" "$OUT/b-pr-why-human.png" header "Why this stopped..Summary" \
    && ok "b-pr-why-human.png: title + Open state + 'Why this stopped' + 'What the agent asks a human to do'" || bad "b-pr-why-human.png" "$(cat "$TMP/crops/log" 2>/dev/null)"

  echo "  HUMAN STEP (labelled): a person adds the migration the agent described, runs the checks, marks the PR ready, merges"
  HEAD_BRANCH="$(jq -r .head.ref "$PRJ")"; HUMAN="$TMP/human-clone"
  git clone -q "http://adw:$TOKEN@127.0.0.1:$PORT/adw/rota.git" "$HUMAN" 2>/dev/null && git -C "$HUMAN" checkout -q "$HEAD_BRANCH" \
    && [ -n "$MIGRATION" ] && [ ! -e "$HUMAN/$MIGRATION" ] && [ -s "$TMP/b-migration.sql" ] \
    && cp "$TMP/b-migration.sql" "$HUMAN/$MIGRATION" && git -C "$HUMAN" add "$MIGRATION" \
    && git -C "$HUMAN" -c user.name="A. Human (ward systems lead)" -c user.email=human@example.org \
         commit -qm "Add $MIGRATION exactly as the agent proposed in the PR (human contribution)" \
    && git -C "$HUMAN" push -q origin "$HEAD_BRANCH" 2>/dev/null \
    && [ "$(git -C "$HUMAN" show --name-only --format= HEAD)" = "$MIGRATION" ] \
    && ok "HUMAN: committed exactly the SQL from the PR body as $MIGRATION to $HEAD_BRANCH ($(git -C "$HUMAN" rev-parse --short HEAD), one file)" \
    || bad "HUMAN: could not add the migration to $HEAD_BRANCH" "$(cat "$TMP/b-migration.sql" 2>/dev/null)"
  export_ref adw/rota "$HEAD_BRANCH" "$TMP/b-human"
  HUMAN_CHECKS=1
  for pair in "test:python -m pytest -q tests" "isolation:python -m pytest -q tests/test_isolation.py" "e2e:python e2e/capture.py"; do
    name="${pair%%:*}"; cmd="${pair#*:}"
    if box "$TMP/b-human" bash -c "$cmd" > "$TMP/b-human-$name.log" 2>&1; then
      detail="$(grep -Eo '[0-9]+ passed' "$TMP/b-human-$name.log" | tail -1)"; [ -n "$detail" ] || detail="$(grep -c '^ok' "$TMP/b-human-$name.log") e2e steps ok"
      ok "HUMAN: check $name passes on the PR branch with the migration ($detail)"
    else HUMAN_CHECKS=0; bad "HUMAN: check $name fails on the PR branch with the migration" "$(tail -8 "$TMP/b-human-$name.log")"; fi
  done
  if [ "$HUMAN_CHECKS" = 1 ]; then
    TITLE="$(jq -r .title "$PRJ")"
    [ "${TITLE#WIP: }" != "$TITLE" ] && api PATCH "/repos/adw/rota/pulls/$PR" -d "$(jq -n --arg t "${TITLE#WIP: }" '{title: $t}')" >/dev/null \
      && info "HUMAN: marked the PR ready (Forgejo's draft convention: removed the 'WIP: ' prefix)"
    api POST "/repos/adw/rota/pulls/$PR/merge" -d '{"Do":"merge","MergeMessageField":"Pay band: agent change plus the migration a human added and checked"}' >/dev/null
    api GET "/repos/adw/rota/pulls/$PR" | jq -e '.merged == true' >/dev/null && ok "HUMAN: PR #$PR merged after test, isolation and e2e passed" || bad "human merge of PR #$PR"
    export_ref adw/rota main "$TMP/main-b"
    box "$TMP/main-b" python /helpers/probe_pay_band.py > "$TMP/b-probe.log" 2>&1 \
      && ok "main on the forge records and shows the pay band (independent probe)" || bad "pay band not (fully) on main" "$(grep -v '^{' "$TMP/b-probe.log")"
    PRSTATE_OF[b]="merged by a human (after adding the migration the agent asked for)"
  else PRSTATE_OF[b]="open (the human's checks failed)"; fi
  OUTCOME_OF[b]="the run stopped at $STEP and handed its work over: PR #$PR [needs human] with the agent's REQUIRES HUMAN request ($MIGRATION + SQL); a human added exactly that file; test, isolation and e2e $([ "$HUMAN_CHECKS" = 1 ] && echo "passed; the human merged" || echo "did not all pass; not merged")"
  EXTRA_OF[b]="$(jq -n --arg step "$STEP" --arg migration "$MIGRATION" --rawfile sql "$TMP/b-migration.sql" --arg title "$(jq -r .title "$PRJ")" \
    --argjson checks "$HUMAN_CHECKS" '{handoff: {stopped_at: $step, pr_title: $title, requested_file: $migration, requested_sql: $sql,
      human_added_exactly_the_requested_sql: true, checks_on_pr_branch_after_human_step: (if $checks == 1 then "test, isolation, e2e passed" else "failed" end)}}')"
else
  # Neither a gate stop nor a handoff: record exactly what happened, and fail (a stopped run must hand over).
  OUTCOME_OF[b]="neither a gate stop nor a handoff: $(jq -r '"status " + (.status // "?") + ", ship " + (.ship | tostring)' "$STATE" 2>/dev/null | cut -c1-300)"
  bad "unexpected outcome" "${OUTCOME_OF[b]}"
  PRSTATE_OF[b]="${PR:+$(api GET "/repos/adw/rota/pulls/$PR" | jq -r 'if .merged then "merged" else .state end')}"
fi
verdicts_json "$STATE" > "$OUT/b-verdicts.json"
[ "$before" = "$(fingerprint "$APP")" ] && ok "local app clone untouched (HEAD, branch, status)" || bad "local app clone changed"
MAIN_OF[b]="$(api GET /repos/adw/rota/branches/main | jq -r .commit.id)"
COST_OF[b]="$(run_cost "$LAYER" "$B")"; STEPS_OF[b]="$(agent_steps "$LAYER" "$B")"; ROUNDS_OF[b]="$(jq '.review.rounds | length' "$STATE" 2>/dev/null || echo 0)"
info "scenario B: ${SECONDS_OF[b]}s, \$${COST_OF[b]}, ${STEPS_OF[b]} agent steps, ${ROUNDS_OF[b]} review round(s)"
fi

if [[ "$ONLY" == *c* ]]; then
CUR=c
echo "SCENARIO C  a PLANTED DEFECT (scripted build step): do the real reviewers catch it?"
before="$(fingerprint "$APP_C")"
N="$(file_issue adw/rota-c "$PROMPT_C_TITLE" "$PROMPT_C_BODY")"
info "issue #$N filed on adw/rota-c (pristine app); running ADW planted: build = scripted planter, everything else real"
run_adw c "$LAYER_C" planted feature "$N"; C="$RUN_ID"; ID_OF[c]="$C"; SECONDS_OF[c]=$RUN_SECONDS
STATE="$LAYER_C/agents/$C/state.json"
jq -e '.script' "$LAYER_C/agents/$C/build/raw.jsonl" >/dev/null 2>&1 \
  && ok "the build step ran on the scripted planter (mock agent), not on a real agent" || bad "build step was not the planter"
grep -Eq '\] step test \(check\) passed' "$TMP/c-run.log" && grep -Eq '\] step isolation \(check\) passed' "$TMP/c-run.log" && grep -Eq '\] step e2e \(check\) passed' "$TMP/c-run.log" \
  && ok "test, isolation and e2e passed on the planted change (round 1)" || bad "checks on the planted change" "$(grep -E 'step (test|isolation|e2e)' "$TMP/c-run.log" | scrub | head -6)"
ROUNDS="$(jq '.review.rounds | length' "$STATE" 2>/dev/null || echo 0)"; ROUNDS_OF[c]="$ROUNDS"
jq '{round: 1, combined_verdict: .review.rounds[0].verdict, reviewers: .review.rounds[0].reviewers, patch: .review.rounds[0].patch}' "$STATE" 2>/dev/null | scrub > "$OUT/c-round1-verdicts.json"
if jq -e '.review.rounds[0].verdict.success == true' "$STATE" >/dev/null 2>&1; then
  bad "THE REVIEWERS APPROVED THE PLANTED DEFECT IN ROUND 1 (an acceptance criterion is unmet and nobody blocked it)" \
      "$(jq -r '.review.rounds[0].reviewers[] | "- " + .summary' "$STATE" | scrub)"
  NOTE_OF[c]="round 1 approved the planted defect"
else
  ok "round 1 rejected the planted change: $(jq '[.review.rounds[0].reviewers[] | select(.success == false)] | length' "$STATE") of 2 reviewers raised a blocker"
  jq -e '[.review.rounds[0].verdict.issues[] | select(.severity == "blocker") | .description + " " + .resolution]
         | any(test("dropdown|select|<option|disabled"; "i"))' "$STATE" >/dev/null 2>&1 \
    && ok "a blocker names the unmet criterion (the assign dropdown still offers expired workers)" \
    || bad "no blocker about the dropdown" "$(jq -c '[.review.rounds[0].verdict.issues[] | select(.severity == "blocker") | .description[0:200]]' "$STATE" 2>/dev/null)"
  [ "$ROUNDS" -ge 2 ] && ok "patch loop ran: $ROUNDS review rounds (plan-patch -> implement by the real agent -> checks re-run -> re-review)" \
    || bad "no second review round"
  jq -e '.review.rounds[0].patch.checks_rerun == ["test", "isolation", "e2e"]' "$STATE" >/dev/null 2>&1 \
    && ok "after the patch, test, isolation and e2e were re-run" || bad "checks not re-run after the patch" "$(jq -c '.review.rounds[0].patch.checks_rerun' "$STATE" 2>/dev/null)"
fi
peeks="$(reviewer_peeks "$LAYER_C" "$C")"
[ -z "$peeks" ] && ok "no reviewer opened the layer definition or the build step's records (no hint about the planted change)" \
  || bad "a reviewer looked at records that reveal the planted change" "$(echo "$peeks" | scrub)"
PR="$(jq -r '.ship.pr.number // empty' "$STATE" 2>/dev/null)"; PR_OF[c]="$PR"; COMMIT_OF[c]="$(jq -r '.ship.commit // ""' "$STATE" 2>/dev/null)"
GATE_OF[c]="$(jq -r '.ship.gate.action // ""' "$STATE" 2>/dev/null)"; REASONS_OF[c]="$(jq -c '.ship.gate.reasons // []' "$STATE" 2>/dev/null || echo '[]')"
PRSTATE_OF[c]="$(api GET "/repos/adw/rota-c/pulls/${PR:-0}" | jq -r 'if .merged then "merged" else (.state // "none") end')"
if jq -e '.review.verdict.success == true' "$STATE" >/dev/null 2>&1; then
  C_MERGED=1
  jq -e '.status == "passed" and .ship.gate.action == "merge" and .ship.gate.reasons == [] and .ship.shipped == true' "$STATE" >/dev/null 2>&1 \
    && ok "round $ROUNDS approved; gate decided merge with no reasons" || bad "final state" "$(jq -c '{status, ship: (.ship | {action, shipped, gate})}' "$STATE" 2>/dev/null)"
  [ "${PRSTATE_OF[c]}" = merged ] && ok "PR #$PR merged on the forge without a human" || bad "PR #${PR:-?}: ${PRSTATE_OF[c]}"
  FIXREF=main
else
  # The last round still had a blocker: the change must NOT ship by itself. Assert exactly that.
  C_MERGED=0
  NOTE_OF[c]="${NOTE_OF[c]:-round $ROUNDS still had a blocker: $(jq -r '[.review.rounds[-1].verdict.issues[] | select(.severity == "blocker") | .description[0:120]] | join(" / ")' "$STATE" 2>/dev/null)}"
  info "OUTCOME: the last review round ($ROUNDS) still raised a blocker, so the gate handed the change to a human"
  jq -e '.status == "passed" and .ship.gate.action == "pr" and .ship.shipped == false and (.ship.gate.reasons | index("review-failed"))' "$STATE" >/dev/null 2>&1 \
    && ok "gate fell back to a human pull request: $(jq -r '.ship.gate.reasons | join(", ")' "$STATE")" || bad "gate" "$(jq -c '.ship | {action, shipped, gate}' "$STATE" 2>/dev/null)"
  [ "${PRSTATE_OF[c]}" = open ] && ok "PR #$PR is OPEN, not merged" || bad "PR #${PR:-?}: ${PRSTATE_OF[c]}"
  [ "$(api GET /repos/adw/rota-c/branches/main | jq -r .commit.id)" = "$(git -C "$APP_C" rev-parse HEAD)" ] \
    && ok "main on the forge is unchanged" || bad "main changed without a human"
  FIXREF="${COMMIT_OF[c]}"   # the fix is judged on the PR head
fi
# The planted commit and the fix, from the forge's history (Forgejo squash-merges, so the run's
# individual commits are on the PR head, refs/pull/N/head, not on main).
CLONE="$(clone_of adw/rota-c)"; export_ref adw/rota-c "$FIXREF" "$TMP/fix-c"
PLANTED="$(git -C "$CLONE" log --format='%H %s' "refs/remotes/pull/${PR:-0}" 2>/dev/null | awk '$2 == "adw:" && $3 == "build" {print $1}' | tail -1)"
if [ -n "$PLANTED" ]; then
  git -C "$CLONE" diff "$PLANTED^" "$PLANTED" > "$OUT/c-planted-diff.patch"
  [ "$(git -C "$CLONE" diff --name-only "$PLANTED^" "$PLANTED" | sort)" = "$(grep -o '^+++ b/.*' "$HERE/planted/planted.patch" | cut -c7- | sort)" ] \
    && ok "c-planted-diff.patch: the planted commit ${PLANTED:0:8} changes exactly the files of planted.patch" || bad "planted commit differs from planted.patch"
  git -C "$CLONE" diff "$PLANTED" "${COMMIT_OF[c]}" > "$OUT/c-fix-diff.patch"
  ok "c-fix-diff.patch: what the patch loop changed after the planted commit ($(grep -c '^diff --git' "$OUT/c-fix-diff.patch") files, $(grep -Ec '^[+-][^+-]' "$OUT/c-fix-diff.patch") lines)"
  export_ref adw/rota-c "$PLANTED" "$TMP/c-before"
else bad "no planted commit ('adw: build') on PR #${PR:-?}"; fi
where="$([ "$C_MERGED" = 1 ] && echo "main on the forge" || echo "the PR head ${COMMIT_OF[c]:0:8}")"
box "$TMP/fix-c" python /helpers/probe_dropdown.py > "$TMP/c-probe-after.json" 2>&1
jq -e '(.offered | not) or (.selectable | not)' "$TMP/c-probe-after.json" >/dev/null 2>&1 \
  && ok "$where: Chloe Martin (expired) cannot be selected in the dropdown (independent probe: $(jq -c '{offered, selectable, option}' "$TMP/c-probe-after.json"))" \
  || bad "expired worker still selectable on $where" "$(cat "$TMP/c-probe-after.json")"
jq -e '.workers_badge' "$TMP/c-probe-after.json" >/dev/null 2>&1 && ok "$where: the workers page shows the Expired badge" || bad "badge missing on $where"
# Before/after pictures of the dropdown, with the app's own capture tooling at the two commits.
for side in before after; do
  dir="$TMP/c-$side"; [ "$side" = after ] && dir="$TMP/fix-c"
  [ -d "$dir" ] || continue
  cp "$HERE/helpers/dropdown_scenario.py" "$dir/e2e/dropdown_scenario.py"
  box "$dir" env ROTA_E2E_SCENARIO=e2e/dropdown_scenario.py "DROPDOWN_SELECT_CHLOE=$([ "$side" = before ] && echo 1 || echo 0)" python e2e/capture.py > "$TMP/c-$side-capture.log" 2>&1 \
    && cp "$dir/e2e-results/01-assign-dropdown.png" "$OUT/c-$side-fix.png" \
    && ok "c-$side-fix.png: the assign dropdown of shift 8 at the $([ "$side" = before ] && echo "planted commit" || echo "$where"), captured by e2e/capture.py" \
    || bad "c-$side-fix.png" "$(tail -4 "$TMP/c-$side-capture.log")"
done
video_outputs c expired-badge "$LAYER_C" "$C"
verdicts_json "$STATE" > "$OUT/c-verdicts.json"
page_shot "$BASE/adw/rota-c/pulls/$PR" "$OUT/c-pr-page.png" && ok "c-pr-page.png: the PR page in real Chrome (${PRSTATE_OF[c]})" || bad "PR page screenshot"
pr_crop "$BASE/adw/rota-c/pulls/$PR" "$OUT/c-pr-final.png" header "Review verdict..Artefacts" \
  && ok "c-pr-final.png: title + ${PRSTATE_OF[c]} state + the final verdict + 'Why a human is needed'" || bad "c-pr-final.png" "$(cat "$TMP/crops/log" 2>/dev/null)"
[ "$before" = "$(fingerprint "$APP_C")" ] && ok "local app clone untouched (HEAD, branch, status)" || bad "local app clone changed"
MAIN_OF[c]="$(api GET /repos/adw/rota-c/branches/main | jq -r .commit.id)"
COST_OF[c]="$(run_cost "$LAYER_C" "$C")"; STEPS_OF[c]="$(agent_steps "$LAYER_C" "$C")"
OUTCOME_OF[c]="${NOTE_OF[c]:-round 1 rejected the planted defect; the patch loop fixed it; round $ROUNDS approved; merged}"
info "scenario C: ${SECONDS_OF[c]}s, \$${COST_OF[c]}, ${STEPS_OF[c]} agent steps, ${ROUNDS_OF[c]} review round(s)"
fi

echo "OUTPUTS"
CUR=outputs
scenario_json() { local l="$1" title="$2" body="$3" adw="$4" repo="$5" base="$6"
  jq -n --arg title "$title" --arg body "$body" --arg adw "$adw" --arg repo "$repo" --arg base "$base" \
    --arg id "${ID_OF[$l]:-}" --argjson seconds "${SECONDS_OF[$l]:-0}" --argjson cost "${COST_OF[$l]:-0}" \
    --argjson steps "${STEPS_OF[$l]:-0}" --argjson rounds "${ROUNDS_OF[$l]:-0}" --arg gate "${GATE_OF[$l]:-}" \
    --argjson reasons "${REASONS_OF[$l]:-[]}" --arg pr "${PR_OF[$l]:-}" --arg prstate "${PRSTATE_OF[$l]:-}" \
    --arg commit "${COMMIT_OF[$l]:-}" --arg main "${MAIN_OF[$l]:-}" --arg outcome "${OUTCOME_OF[$l]:-}" \
    --argjson passed "${PASS_OF[$l]:-0}" --argjson failed "${FAIL_OF[$l]:-0}" --argjson extra "${EXTRA_OF[$l]:-{\}}" \
    '{prompt: {issue_title: $title, issue_body: $body}, adw: $adw, class: "feature", repository: $repo, started_from: $base,
      run_id: $id, wall_clock_seconds: $seconds, agent_cost_usd: ($cost * 10000 | round / 10000), agent_steps: $steps,
      review_rounds: $rounds, gate: {action: $gate, reasons: $reasons}, pull_request: ($pr | tonumber? // null),
      pull_request_final_state: $prstate, shipped_commit: $commit, main_after: $main, outcome: $outcome,
      assertions: {passed: $passed, failed: $failed}} + $extra'; }
ran=(); for l in a b c; do [[ "$ONLY" == *$l* ]] && ran+=("$l"); done
{
  echo '{}'
  [[ "$ONLY" == *a* ]] && scenario_json a "$PROMPT_A_TITLE" "$PROMPT_A_BODY" feature adw/rota "pristine app" | jq '{scenario_a: .}'
  [[ "$ONLY" == *b* ]] && scenario_json b "$PROMPT_B_TITLE" "$PROMPT_B_BODY" feature adw/rota "$([[ "$ONLY" == *a* ]] && echo "main after scenario A" || echo "pristine app")" | jq '{scenario_b: .}'
  [[ "$ONLY" == *c* ]] && scenario_json c "$PROMPT_C_TITLE" "$PROMPT_C_BODY" "planted (build = scripted planter)" adw/rota-c "pristine app" | jq '{scenario_c: .}'
} | jq -s 'add' > "$TMP/scenarios.json"
jq --arg date "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg forgejo "$(api GET /version | jq -r .version)" \
  --slurpfile attempts <(cat "${AL_ROTADEMO_ATTEMPTS:-/dev/null}" 2>/dev/null; echo 'null') \
  '{date: $date, model: "claude-opus-5-5 (base) / claude-fable-5-2 (heavy)", forgejo: $forgejo, data: "synthetic (tests/rotademo/app/seed.py)"} + .
   | if $attempts[0] != null then .attempts = $attempts[0] else . end' "$TMP/scenarios.json" > "$OUT/summary.json"
for png in "$OUT"/*-pr-page.png; do   # keep the committed outputs small: PR pages at 1000 px wide
  [ -f "$png" ] && [ "$(stat -c %s "$png")" -gt 600000 ] && ffmpeg -loglevel error -y -i "$png" -vf scale=1000:-1 "$png.small.png" && mv "$png.small.png" "$png"
done
echo "  per scenario: $(for l in a b c; do printf '%s: %s ok / %s failed  ' "$l" "${PASS_OF[$l]:-0}" "${FAIL_OF[$l]:-0}"; done)"
if [ "$DRY" = 1 ]; then info "dry run: outputs NOT copied to ${OUT_FINAL#$ROOT/}; they are in $OUT (AL_KEEP=1 keeps them)"
elif [ "${FAIL_OF[setup]:-0}" -eq 0 ]; then
  mkdir -p "$OUT_FINAL"
  cp "$OUT/before.png" "$OUT_FINAL/" 2>/dev/null
  copied=""
  for l in "${ran[@]}"; do
    if [ "${FAIL_OF[$l]:-0}" -eq 0 ]; then
      rm -f "$OUT_FINAL/$l-"* && cp "$OUT/$l-"* "$OUT_FINAL/" && copied="$copied $l"
    else info "scenario $l had failures: its outputs are NOT copied (they are in $OUT with AL_KEEP=1)"; fi
  done
  # summary.json: only entries of scenarios that passed replace committed ones
  jq --slurpfile now "$OUT/summary.json" --arg copied "$copied" \
    '. as $old | $now[0] as $n | reduce ("a", "b", "c") as $l ($n;
       if ($copied | contains($l)) then . else .["scenario_" + $l] = $old["scenario_" + $l] end)
     | .attempts = (.attempts // $old.attempts)
     | with_entries(select(.value != null))
     | .total_agent_cost_usd = ([.scenario_a.agent_cost_usd, .scenario_b.agent_cost_usd, .scenario_c.agent_cost_usd] | map(. // 0) | add * 10000 | round / 10000)
     | .total_wall_clock_seconds = ([.scenario_a.wall_clock_seconds, .scenario_b.wall_clock_seconds, .scenario_c.wall_clock_seconds] | map(. // 0) | add)' \
    <(cat "$OUT_FINAL/summary.json" 2>/dev/null || echo '{}') > "$TMP/summary.merged.json" && cp "$TMP/summary.merged.json" "$OUT_FINAL/summary.json"
  [ -n "$copied" ] && ok "outputs of scenario(s)$copied in ${OUT_FINAL#$ROOT/} ($(du -sh "$OUT_FINAL" | cut -f1))"
fi
echo; echo "rotademo live: $pass passed, $fail failed"; [ "$fail" -eq 0 ]
