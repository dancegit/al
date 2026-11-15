"""Per-run isolation against REAL rootless podman: own network namespace per run (no port
collisions), loopback-only publishing at leased ports, per-run sidecar pods, hardened flags,
cross-run invisibility, stale-resource GC and complete teardown.

Skipped cleanly when podman or the cached test image is unavailable; nothing is pulled. The image
is the git-enabled `al-sandbox-test:v2` built (once, from a local base) by test_sandbox_podman.py;
it doubles as the sidecar image (`python3 -m http.server`)."""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from adw_modules import envs, gitsafe, steps
from adw_modules.ir import load
from adw_modules.state import ADWState
from test_sandbox_podman import sandbox_image  # noqa: F401 — the shared, cached image fixture

pytestmark = pytest.mark.podman

READY = "python3 -c \"import socket; socket.create_connection(('127.0.0.1', 8000), timeout=1)\""


def _podman(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["podman", *args], capture_output=True, text=True, timeout=timeout, check=False)


@pytest.fixture
def isolated(make_layer, sandbox_image, monkeypatch):  # noqa: F811
    """Factory for run stacks of one uniquely named layer; tears everything down and proves that
    nothing named al-<layer>-* survives."""
    monkeypatch.setattr(envs.EnvStack, "image", property(lambda self: sandbox_image))
    name = f"iso{secrets.token_hex(3)}"
    stacks: list[envs.EnvStack] = []
    made: dict[str, object] = {}

    def factory(adw_id: str, *, publish=(), services=(), read_only=False, agent_inside=False, caches=(),
                ports_per_run=1, network="slirp") -> envs.EnvStack:
        if "layer" not in made:
            made["layer"] = load(make_layer({
                "name": name,
                "environments": [
                    {"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 27800,
                     "portsPerRun": ports_per_run, "keep": "never", "status": "stable"},
                    {"id": "box", "kind": "podman", "agentInside": agent_inside, "baseImage": "debian", "cpus": "1",
                     "memory": "512m", "network": network, "packages": [], "setup": [], "status": "stable",
                     "scope": "run", "caches": list(caches), "credentials": [], "publish": list(publish),
                     "services": list(services), "pidsLimit": 512, "readOnlyRoot": read_only}],
                "services": [{"id": "web", "kind": "container", "image": sandbox_image, "env": ["SIDE=car"],
                              "command": "python3 -m http.server 8000 --directory /tmp", "readyCmd": READY,
                              "readyTimeout": 30, "status": "stable"}],
            }))
        state = ADWState(adw_id, "test", prompt="isolation", klass="chore")
        stack = envs.build(made["layer"], state, ["wt", "box"])
        stack.setup()
        state.save(made["layer"].layer_dir)
        stacks.append(stack)
        return stack

    yield factory
    for stack in stacks:
        stack.teardown(success=True)
    leftovers = (_podman("ps", "-a", "--format", "{{.Names}}").stdout.split()
                 + _podman("pod", "ps", "--format", "{{.Name}}").stdout.split())
    for leftover in [item for item in leftovers if item.startswith(f"al-{name}-")]:
        _podman("pod", "rm", "-f", "-t", "0", leftover)
        _podman("rm", "-f", "-t", "0", leftover)
    assert [item for item in leftovers if item.startswith(f"al-{name}-")] == []


def _inside(stack: envs.EnvStack, cmd: str, timeout: int = 120) -> tuple[int, str]:
    return steps.run_command(stack.wrap(cmd), stack.cwd, stack.process_env(), timeout)


def _get(url: str, timeout: float = 2.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def _wait_get(url: str, seconds: float = 20.0) -> tuple[int, str]:
    deadline = time.monotonic() + seconds
    while True:
        try:
            return _get(url)
        except (OSError, urllib.error.URLError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.25)


def _serve(stack: envs.EnvStack) -> None:
    code, out = _inside(stack, "nohup python3 -m http.server 8000 >/dev/null 2>&1 & sleep 0.5; "
                               f"{READY.replace('timeout=1', 'timeout=5')}")
    assert code == 0, out


def _non_loopback_ip() -> str | None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return None if address.startswith("127.") else address


def test_concurrent_runs_bind_the_same_port_without_collision(isolated) -> None:
    run_a = isolated("1a0a0001", publish=[8000])
    run_b = isolated("1a0a0002", publish=[8000])
    for stack, who in ((run_a, "run A"), (run_b, "run B")):
        (Path(stack.state.worktree) / "who.txt").write_text(who)
        _serve(stack)                                     # both bind container port 8000, concurrently
    port_a, port_b = run_a.state.ports[0], run_b.state.ports[0]
    assert port_a != port_b
    assert run_a.env["ADW_PUBLISHED_8000"] == str(port_a) and run_b.env["ADW_PUBLISHED_8000"] == str(port_b)
    assert _wait_get(f"http://127.0.0.1:{port_a}/who.txt") == (200, "run A")
    assert _wait_get(f"http://127.0.0.1:{port_b}/who.txt") == (200, "run B")
    code, out = _inside(run_a, 'test "$ADW_PUBLISHED_8000" = "$ADW_PORT_0"')
    assert code == 0, out
    try:                                                   # container port 8000 itself is not on the host
        status, body = _get("http://127.0.0.1:8000/who.txt", timeout=1)
        assert body not in ("run A", "run B")
    except (OSError, urllib.error.URLError):
        pass
    address = _non_loopback_ip()
    if address:                                            # published on loopback only
        with pytest.raises((OSError, urllib.error.URLError)):
            _get(f"http://{address}:{port_a}/who.txt", timeout=1)


def test_unpublished_run_is_not_reachable_from_the_host(isolated) -> None:
    stack = isolated("1a0a0003")                           # portsPerRun 1 is leased, but nothing is published
    (Path(stack.state.worktree) / "who.txt").write_text("hidden")
    _serve(stack)
    with pytest.raises((OSError, urllib.error.URLError)):
        _get(f"http://127.0.0.1:{stack.state.ports[0]}/who.txt", timeout=1)


def test_runs_cannot_see_each_others_worktree_quarantine_agents_tmp_or_home(isolated) -> None:
    run_a = isolated("1a0a0004", agent_inside=True, read_only=True)
    run_b = isolated("1a0a0005", agent_inside=True, read_only=True)
    layer_dir = Path(run_a.layer.layer_dir)
    code, out = _inside(run_a, "echo secret-a > /tmp/a.txt && mkdir -p $HOME && echo secret-a > $HOME/a.txt")
    assert code == 0, out
    code, out = _inside(run_b, "test ! -e /tmp/a.txt && test ! -e $HOME/a.txt && test -w $HOME && test -w /tmp")
    assert code == 0, f"run B sees run A's /tmp or HOME: {out}"
    b_paths = [run_b.state.worktree, run_b.sandbox.quarantine, layer_dir / "agents" / "1a0a0005",
               layer_dir / "artefacts" / "1a0a0005"]
    for path in b_paths:
        code, out = _inside(run_a, f"test ! -e '{path}'")
        assert code == 0, f"run A can see {path}: {out}"
        _inside(run_a, f"mkdir -p '{path}' 2>/dev/null; echo pwned > '{path}/pwned.txt'")
        assert not (Path(path) / "pwned.txt").exists(), f"run A wrote into run B's {path}"
    code, out = _inside(run_a, f"test -r '{layer_dir / 'agentic-layer.json'}' && "  # the layer itself is readable …
                               f"test ! -w '{layer_dir / 'agentic-layer.json'}' && "  # … read-only …
                               f"test -d '{layer_dir / 'agents' / '1a0a0004'}' && test ! -e '{layer_dir / 'agents' / '.ports.json'}'")
    assert code == 0, out                                   # … and its agents/ holds only run A's own dir


@pytest.mark.parametrize("read_only", [False, True])
def test_hardened_container_still_commits_runs_python_and_writes_caches(isolated, read_only) -> None:
    cache = f"altest{secrets.token_hex(3)}"
    stack = isolated("1a0a0006" if read_only else "1a0a0007", read_only=read_only, caches=[cache])
    try:
        code, out = _inside(stack, "set -e; echo change >> app.py; git add -A; "
                                   "git -c user.name=t -c user.email=t@t commit -qm hardened; git rev-parse HEAD; "
                                   "python3 -c 'import sys; print(sys.version_info[0])'; "
                                   f"mkdir -p \"$AL_CACHE_{cache.upper()}/wheels\" && echo w > \"$AL_CACHE_{cache.upper()}/wheels/x\"; "
                                   "mkdir -p $HOME/.cache && echo h > $HOME/.cache/x; "
                                   "grep -E '^(CapEff|NoNewPrivs)' /proc/self/status; "
                                   # the container user is not root, so probe the mount flag, not a write
                                   "awk '$2 == \"/\" {split($4, o, \",\"); print \"ROOT-\" (o[1] == \"ro\" ? \"READONLY\" : \"WRITABLE\")}' "
                                   "/proc/self/mounts")
        assert code == 0, out
        lines = out.split()
        commit = next(line for line in lines if len(line) == 40)
        assert (stack.sandbox.quarantine / commit[:2] / commit[2:]).is_file()   # the commit landed in the quarantine
        assert "0000000000000000" in out and "NoNewPrivs:\t1" in out.replace("NoNewPrivs: ", "NoNewPrivs:\t")
        assert ("ROOT-READONLY" in out) is read_only and ("ROOT-WRITABLE" in out) is (not read_only)
        inspect = json.loads(_podman("inspect", stack.run_name).stdout)[0]["HostConfig"]
        assert inspect["PidsLimit"] == 512 and inspect["ReadonlyRootfs"] is read_only
        assert gitsafe.run(stack.sandbox, ["log", "--format=%s", "-1"], "test").stdout.strip() == "hardened"
    finally:
        stack.teardown(success=True)
        _podman("volume", "rm", "-f", f"al-cache-{cache}")


def test_sidecars_are_per_run_and_reachable_on_localhost(isolated) -> None:
    run_a = isolated("1a0a0008", services=["web"], publish=[8001], read_only=True)
    run_b = isolated("1a0a0009", services=["web"], publish=[8001], read_only=True)
    fetch = ("python3 -c \"import urllib.request, urllib.error, sys\n"
             "try: print(urllib.request.urlopen('http://127.0.0.1:8000/mark', timeout=5).read().decode())\n"
             "except urllib.error.HTTPError as e: print('HTTP', e.code)\"")
    code, out = _inside(run_a, 'test "$ADW_SERVICE_WEB" = 1 && ' + fetch)  # starts pod A: sidecar, ready, container
    assert code == 0 and "HTTP 404" in out, out
    code, out = _inside(run_b, fetch)                                        # pod B, its own sidecar on the same port
    assert code == 0 and "HTTP 404" in out, out
    sidecar_a, sidecar_b = f"{run_a.run_name}-web", f"{run_b.run_name}-web"
    assert _podman("exec", sidecar_a, "sh", "-c", "echo from-sidecar-a > /tmp/mark").returncode == 0
    assert _inside(run_a, fetch)[1].strip() == "from-sidecar-a"
    assert "HTTP 404" in _inside(run_b, fetch)[1]                            # B's sidecar is a different instance
    for sidecar in (sidecar_a, sidecar_b):
        info = json.loads(_podman("inspect", sidecar).stdout)[0]
        assert [mount for mount in info["Mounts"] if mount.get("Type") == "bind"] == [], info["Mounts"]
        assert info["HostConfig"]["ReadonlyRootfs"] is True and info["HostConfig"]["PidsLimit"] == 512
        assert "SIDE=car" in info["Config"]["Env"] and not any(env.startswith(("GIT_", "ADW_")) for env in info["Config"]["Env"])
        out = _podman("exec", sidecar, "sh", "-c", f"grep CapEff /proc/self/status; test ! -e '{run_a.state.worktree}' "
                                                   f"&& test ! -e '{run_a.layer.app_root / '.git'}' "
                                                   f"&& test ! -e '{run_a.layer.layer_dir}' && echo NOTHING-MOUNTED").stdout
        assert "0000000000000000" in out and "NOTHING-MOUNTED" in out, out
    # freezing for host git pauses the run container only; the sidecar keeps serving; unpause follows
    with gitsafe.session(run_a.sandbox):
        assert _podman("inspect", "-f", "{{.State.Status}}", run_a.run_name).stdout.strip() == "paused"
        assert _podman("inspect", "-f", "{{.State.Status}}", sidecar_a).stdout.strip() == "running"
    assert _podman("inspect", "-f", "{{.State.Status}}", run_a.run_name).stdout.strip() == "running"
    # publishing moved to the pod: a server in the run container is reachable at A's leased port
    code, out = _inside(run_a, "nohup python3 -m http.server 8001 >/dev/null 2>&1 & sleep 0.5; "
                               "python3 -c \"import socket; socket.create_connection(('127.0.0.1', 8001), timeout=5)\"")
    assert code == 0, out
    assert _wait_get(f"http://127.0.0.1:{run_a.state.ports[0]}/")[0] == 200
    run_a.teardown(success=False)
    assert _podman("pod", "exists", run_a.run_name).returncode != 0 and _podman("container", "exists", sidecar_a).returncode != 0
    assert _podman("container", "exists", sidecar_b).returncode == 0          # B is untouched


def test_gc_removes_a_crashed_runs_pod_and_keeps_a_live_one(isolated) -> None:
    live = isolated("1a0a000a", services=["web"])
    crashed = isolated("1a0a000b", services=["web"])
    for stack in (live, crashed):
        assert _inside(stack, "true")[0] == 0
    layer = live.layer
    # the crashed run's process died: its lease now names a dead pid, its status is still "running"
    dead = subprocess.Popen(["true"])
    dead.wait()
    table = Path(layer.layer_dir) / "agents" / ".ports.json"
    leases = json.loads(table.read_text())
    leases["1a0a000b"]["pid"] = dead.pid
    table.write_text(json.dumps(leases))
    removed = envs.collect_stale_containers(layer)
    assert crashed.run_name in removed and not any(name.startswith(live.run_name) for name in removed)
    assert _podman("pod", "exists", crashed.run_name).returncode != 0
    assert _podman("container", "exists", crashed.run_name).returncode != 0
    assert _podman("pod", "exists", live.run_name).returncode == 0
    assert _podman("container", "exists", live.run_name).returncode == 0
    assert _inside(live, "true")[0] == 0
    crashed._container = crashed._pod = None                                  # it is gone; teardown has nothing to do
    assert os.getpid() == json.loads(table.read_text())["1a0a000a"]["pid"]
