#!/usr/bin/env bash
# Run the al work dashboard in its own rootless podman container.
#
#   kit/dashboard/run-in-podman.sh [--port 8765] [--poll SECONDS] [--podman-socket] [--read-only] [--no-start] DIR [DIR …]
#
# Each DIR that contains agentic-layer.json is watched as a layer (--layer); any other DIR is
# scanned for layers (--scan). Every DIR is mounted READ-ONLY at the same path inside the
# container. The one exception: for each layer DIR given directly, its agents/settings directory
# (created if missing) is mounted read-write, so the Models panel can change the layer's local
# model setting (agents/settings/models.json); nothing else is writable. Scanned layers get no
# writable mount, and the panel says why. --read-only drops these mounts and the panel's write.
# Starting work from the page is always off in the container (no uv, no checkout, no writable
# layer); --no-start is accepted and passed on so `al dashboard --podman --no-start` works.
# Only 127.0.0.1:PORT is published; the container sits on an internal podman network
# with no route out, runs with --cap-drop=ALL, no-new-privileges and a read-only root, and as your
# own user (--userns=keep-id) so it can read run state files that are private to you.
#
# Inside a container the dashboard cannot run the host's podman, so container status comes from
# run.log. --podman-socket opts in to mounting your podman API socket so it can also show
# container state, CPU and memory. Read the README first: a socket gives full control of your
# podman, read-only mount or not.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
port=8765
poll=""
socket=0
read_only=0
no_start=0
dirs=()
while [ $# -gt 0 ]; do
  case "$1" in
    --port) port="$2"; shift 2 ;;
    --poll) poll="$2"; shift 2 ;;
    --podman-socket) socket=1; shift ;;
    --read-only) read_only=1; shift ;;
    --no-start) no_start=1; shift ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) dirs+=("$1"); shift ;;
  esac
done
[ ${#dirs[@]} -gt 0 ] || { echo "usage: $0 [--port N] [--poll S] [--podman-socket] [--read-only] [--no-start] DIR [DIR …]" >&2; exit 2; }
command -v podman >/dev/null || { echo "podman is required" >&2; exit 1; }

image=localhost/al-dashboard:latest
network=al-dashboard-internal
podman build --quiet -t "$image" -f "$here/Containerfile" "$here" >/dev/null
podman network exists "$network" || podman network create --internal "$network" >/dev/null

mounts=()
watch=()
for dir in "${dirs[@]}"; do
  real="$(realpath "$dir")"
  [ -d "$real" ] || { echo "not a directory: $dir" >&2; exit 2; }
  mounts+=(-v "$real:$real:ro")
  if [ -f "$real/agentic-layer.json" ]; then
    watch+=(--layer "$real")
    if [ "$read_only" = 0 ]; then   # the layer's local model setting: the one writable directory
      mkdir -p "$real/agents/settings"
      mounts+=(-v "$real/agents/settings:$real/agents/settings:rw")
    fi
  else
    watch+=(--scan "$real")
  fi
done

[ -z "$poll" ] || watch+=(--poll "$poll")
[ "$read_only" = 0 ] || watch+=(--read-only)
[ "$no_start" = 0 ] || watch+=(--no-start)

extra=()
if [ "$socket" = 1 ]; then
  sock="${AL_DASHBOARD_PODMAN_SOCKET:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock}"
  [ -S "$sock" ] || { echo "no podman API socket at $sock (systemctl --user start podman.socket)" >&2; exit 1; }
  extra+=(-v "$sock:/run/podman/podman.sock:ro" --podman-socket /run/podman/podman.sock)
fi

token="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
echo "al dashboard (container): http://127.0.0.1:$port/?token=$token"
exec podman run --rm --name "al-dashboard-$port" \
  --userns=keep-id --user "$(id -u):$(id -g)" \
  --cap-drop=ALL --security-opt=no-new-privileges --read-only \
  --pids-limit 64 --memory 256m --cpus 1 \
  --network "$network" -p "127.0.0.1:$port:8765" \
  -e AL_DASHBOARD_TOKEN="$token" \
  "${mounts[@]}" "${extra[@]:0:2}" "$image" "${watch[@]}" "${extra[@]:2}"
