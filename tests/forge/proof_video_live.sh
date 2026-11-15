#!/usr/bin/env bash
# Live check: the Proof comment on a pull request embeds the run's recording as a PLAYABLE video
# (for the human reviewer), ahead of screenshots and of the frames cut for reviewer agents.
# Throwaway local Forgejo; no agent, no LLM: it posts real saved artefacts through the runtime's
# own post_proof(). Writes docs/demo/webdemo/proof-video-pr.png unless AL_NO_OUTPUT=1.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PORT="${AL_FORGEJO_PORT:-3910}"; NAME="al-forgejo-proofvideo"; IMAGE="codeberg.org/forgejo/forgejo:11"
TMP="$(mktemp -d /tmp/al-proofvideo.XXXXXX)"; BASE="http://127.0.0.1:$PORT"
pass=0; fail=0
ok()  { pass=$((pass+1)); printf '  ok    %s\n' "$1"; }
bad() { fail=$((fail+1)); printf '  FAIL  %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; }
trap 'podman rm -f "$NAME" >/dev/null 2>&1; rm -rf "$TMP"' EXIT
api() { local m="$1" p="$2"; shift 2; curl -sS -X "$m" -H "Authorization: token $TOKEN" -H 'Content-Type: application/json' "$BASE/api/v1$p" "$@"; }

echo "SETUP"
podman rm -f "$NAME" >/dev/null 2>&1
podman image exists "$IMAGE" || { echo "image $IMAGE not present"; exit 2; }
podman run -d --name "$NAME" -p "127.0.0.1:$PORT:3000" -e FORGEJO__security__INSTALL_LOCK=true \
  -e FORGEJO__database__DB_TYPE=sqlite3 -e FORGEJO__server__ROOT_URL="$BASE/" -e FORGEJO__log__LEVEL=Warn "$IMAGE" >/dev/null || exit 2
for _ in $(seq 1 60); do curl -fsS "$BASE/api/v1/version" >/dev/null 2>&1 && break; sleep 1; done
podman exec -u git "$NAME" forgejo admin user create --username adw --password 'adw-Passw0rd!' --email adw@example.org --admin --must-change-password=false >/dev/null
TOKEN="$(podman exec -u git "$NAME" forgejo admin user generate-access-token --username adw --token-name al --scopes all --raw | tail -1)"
api POST /user/repos -d '{"name":"notes","default_branch":"main","private":false}' >/dev/null
mkdir -p "$TMP/notes" && cp -r "$ROOT/tests/webdemo/app/." "$TMP/notes/" && cd "$TMP/notes" && git init -q -b main \
  && git add -A && git -c user.name=adw -c user.email=adw@example.org commit -qm initial \
  && git remote add origin "http://adw:$TOKEN@127.0.0.1:$PORT/adw/notes.git" && git push -q origin main \
  && git checkout -qb feature/search && echo "/* search box */" >> static/style.css \
  && git -c user.name=adw -c user.email=adw@example.org commit -qam "feature: search box" && git push -q origin feature/search
PR=null; for _ in $(seq 1 15); do   # the forge needs a moment to see the pushed branch
  RESP="$(api POST /repos/adw/notes/pulls -d '{"head":"feature/search","base":"main","title":"feature: Search box that filters the notes","body":"Add a search box above the notes list."}')"
  PR="$(echo "$RESP" | jq -r .number)"; [ "$PR" != null ] && break; sleep 1; done
[ "$PR" != null ] || { echo "PR not created: $RESP" | cut -c1-300; exit 2; }
L="$TMP/layer"; A="$L/artefacts/demo0001"; mkdir -p "$A/recording/.keyframes" "$A/screenshots"
cp "$ROOT/docs/demo/webdemo/b-feature.webm" "$A/recording/session.webm"
cp "$ROOT"/docs/demo/webdemo/b-keyframes/session-0[2468].png "$A/recording/.keyframes/"
cp "$ROOT/docs/demo/webdemo/a-after.png" "$A/screenshots/01-home.png"
cd "$ROOT"

echo "TEST  the runtime posts the proof"
AL_T="$TOKEN" PYTHONPATH="$ROOT/kit/runtime" python3 - "$L" "$BASE" "$PR" <<'PY' && ok "post_proof ran" || bad "post_proof failed"
import os, sys
from pathlib import Path
from types import SimpleNamespace
from adw_modules import forge, ship
layer_dir, base, pr = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
ir = {"name": "notes-demo", "forge": {"id": "forgejo", "kind": "forgejo", "status": "stable", "host": base, "repo": "adw/notes",
                                      "tokenEnv": "AL_T", "botMarker": "[ADW-BOT]"}}
layer = SimpleNamespace(ir=ir, layer_dir=layer_dir, app_root=layer_dir)
frames = sorted(p.relative_to(layer_dir).as_posix() for p in (layer_dir / "artefacts/demo0001/recording/.keyframes").glob("*.png"))
state = SimpleNamespace(adw_id="demo0001", ship={}, artefacts=[
    {"id": "screenshots", "type": "image", "path": "artefacts/demo0001/screenshots/01-home.png"},
    {"id": "recording", "type": "video", "path": "artefacts/demo0001/recording/session.webm",
     "keyframes": [{"path": f} for f in frames]}])
result = ship.post_proof(layer, state, forge.get_client(layer), {"number": pr})
print("   ", {k: result[k] for k in ("posted", "attached", "warnings")})
sys.exit(0 if result["posted"] and not result["warnings"] else 1)
PY
body="$(api GET "/repos/adw/notes/issues/$PR/comments" | jq -r '.[-1].body')"
echo "$body" | grep -q '<video src="http' && ok "the comment embeds the recording as a video element" || bad "no video element in the comment" "$(echo "$body" | head -5)"
echo "$body" | grep -q 'as given to the reviewer agents' && ok "frames are labelled as the agents' version" || bad "frames label missing"
html="$(curl -sS "$BASE/adw/notes/pulls/$PR")"
echo "$html" | grep -q '<video[^>]*src="[^"]*attachments' && ok "Forgejo renders a video player on the pull request page" || bad "Forgejo did not render a <video> (sanitised away?)" "$(echo "$html" | grep -o '<video[^>]*>' | head -2)"
url="$(echo "$body" | grep -o 'src="http[^"]*"' | head -1 | cut -d'"' -f2)"
ctype="$(curl -sSI "$url" | tr -d '\r' | awk 'tolower($1)=="content-type:"{print $2}')"
size="$(curl -sS "$url" | wc -c)"
[ "$size" -gt 100000 ] && ok "the attached recording is downloadable without signing in ($size bytes, $ctype)" || bad "recording not served ($size bytes)"
if [ "${AL_NO_OUTPUT:-0}" != 1 ]; then
  google-chrome --headless=new --hide-scrollbars --window-size=1300,1500 --virtual-time-budget=6000 \
    --screenshot="$ROOT/docs/demo/webdemo/proof-video-pr.png" "$BASE/adw/notes/pulls/$PR" >/dev/null 2>&1 \
    && ok "saved docs/demo/webdemo/proof-video-pr.png" || bad "screenshot failed"
fi
echo; echo "proof video live: $pass passed, $fail failed"; [ "$fail" -eq 0 ]
