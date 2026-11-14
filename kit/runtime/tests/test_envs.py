from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from adw_modules import envs
from adw_modules.ir import load
from adw_modules.state import ADWState


def _state(prompt: str = "Fix the important issue today") -> ADWState:
    return ADWState("1234abcd", "test", prompt=prompt, klass="bug")


def test_worktree_copies_files_exports_ports_and_removes(make_layer: object, app_repo: Path, tmp_path: Path) -> None:
    (app_repo / ".env").write_text("from app\n", encoding="utf-8")
    (app_repo / ".git" / "info" / "exclude").write_text(".env\n", encoding="utf-8")  # an untracked, ignored secret like a real .env
    layer = load(make_layer({"environments": [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [".env"], "portBase": 22000, "portsPerRun": 2, "keep": "never", "status": "stable"}]}))
    state = _state()
    stack = envs.build(layer, state, ["wt"])
    stack.setup()
    assert state.branch.startswith("bug/1234abcd-fix-the-important-issue-today")
    assert (Path(state.worktree) / ".env").read_text() == "from app\n"
    assert len(state.ports) == 2 and stack.env["ADW_PORT_0"] == str(state.ports[0])
    assert subprocess.run(["git", "-C", str(app_repo), "status", "--porcelain"], capture_output=True, text=True, timeout=30).stdout == ""
    stack.teardown(success=True)
    assert not Path(state.worktree).exists()


def test_resume_reuses_existing_worktree(make_layer: object) -> None:
    layer = load(make_layer({"environments": [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 22500, "portsPerRun": 1, "keep": "always", "status": "stable"}]}))
    state = _state()
    envs.build(layer, state, ["wt"]).setup()
    branch, worktree = state.branch, state.worktree
    envs.build(layer, state, ["wt"]).setup()  # would fail with "already exists" if it re-added the worktree
    assert (state.branch, state.worktree) == (branch, worktree)


def test_host_stack_is_read_only_and_runs_in_app(make_layer: object, app_repo: Path) -> None:
    layer = load(make_layer({"environments": [{"id": "host", "kind": "host", "readOnly": True, "status": "stable"}]}))
    stack = envs.build(layer, _state(), ["host"])
    stack.setup()
    assert stack.read_only and stack.cwd == app_repo.resolve()
    assert stack.wrap("ls") == ["bash", "-c", "ls"]


def test_worktree_keep_failure_and_success_semantics(make_layer: object) -> None:
    layer = load(make_layer({"environments": [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 23000, "portsPerRun": 0, "keep": "on-failure", "status": "stable"}]}))
    failed = _state()
    failed_stack = envs.build(layer, failed, ["wt"])
    failed_stack.setup()
    failed_path = Path(failed.worktree)
    failed_stack.teardown(success=False)
    assert failed_path.exists()
    successful = ADWState("1234abce", "test", prompt="done", klass="chore")
    success_stack = envs.build(layer, successful, ["wt"])
    success_stack.setup()
    success_path = Path(successful.worktree)
    success_stack.teardown(success=True)
    assert not success_path.exists()


def test_nix_and_podman_argv_shapes(make_layer: object, monkeypatch: object) -> None:
    layer = load(make_layer({"environments": [
        {"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 24000, "portsPerRun": 1, "keep": "always", "status": "stable"},
        {"id": "nix", "kind": "nix-devshell", "nixpkgs": "x", "packages": [], "pure": True, "shellHook": "", "status": "stable"},
        {"id": "box", "kind": "podman", "agentInside": False, "baseImage": "debian", "cpus": "2", "id": "box", "memory": "4g", "network": "slirp", "packages": [], "setup": [], "status": "stable"},
    ]}))
    calls = []
    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=1 if argv[1:3] == ["image", "exists"] else 0, stderr="", stdout="")
    monkeypatch.setattr(envs.subprocess, "run", fake_run)
    state = _state()
    stack = envs.build(layer, state, ["wt", "nix", "box"])
    stack.setup()
    assert not any(call[0][:2] == ["podman", "build"] for call in calls)  # image is built lazily
    argv = stack.wrap("pytest -q")
    stack.wrap("true")
    builds = [call for call in calls if call[0][:2] == ["podman", "build"]]
    assert len(builds) == 1
    assert builds[0][0] == ["podman", "build", "-t", "al-test-layer:latest", "-f", "env/Containerfile", "env/"]
    assert builds[0][1]["cwd"] == layer.layer_dir
    # scope defaults to "run": one container per run, commands exec into it
    starts = [call[0] for call in calls if call[0][:3] == ["podman", "run", "-d"]]
    assert len(starts) == 1
    start = starts[0]
    assert "--userns=keep-id" in start and "--network" not in start and "--memory" in start and "--cpus" in start
    assert f"{state.worktree}:{state.worktree}" in start                    # worktree at its host path
    assert f"{layer.app_root / '.git'}:{layer.app_root / '.git'}" in start  # git metadata, not the app tree
    assert not any(arg.startswith(f"{layer.app_root}:") for arg in start)   # the app working tree is never mounted
    assert start[-3:] == ["al-test-layer:latest", "sleep", "infinity"]
    assert argv[:2] == ["podman", "exec"] and f"ADW_PORT_0={state.ports[0]}" in argv
    assert argv[-4:] == ["al-test-layer-1234abcd", "bash", "-c", "pytest -q"]
    assert stack.wrap_agent(["agent", "--version"])[0:3] == ["nix", "develop", f"path:{layer.layer_dir / 'env'}"]
    assert envs.EnvStack(layer, state, [layer.option("environments", "nix")]).wrap("true")[:3] == ["nix", "develop", f"path:{layer.layer_dir / 'env'}"]
    stack.teardown(success=True)
    assert ["podman", "rm", "-f", "al-test-layer-1234abcd"] in [call[0] for call in calls]


def _podman_layer(make_layer, **podman):
    option = {"id": "box", "kind": "podman", "agentInside": False, "baseImage": "debian", "cpus": "2", "memory": "4g",
              "network": "none", "packages": [], "setup": [], "status": "stable", "scope": "run", "caches": [],
              "credentials": [], **podman}
    return load(make_layer({"environments": [
        {"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 24100, "portsPerRun": 0,
         "keep": "always", "status": "stable"}, option]}))


def _fake_podman(monkeypatch):
    calls = []
    real_run = envs.subprocess.run
    def fake_run(argv, **kwargs):
        if argv[0] != "podman":
            return real_run(argv, **kwargs)
        calls.append(argv)
        return SimpleNamespace(returncode=0, stderr="", stdout="")
    monkeypatch.setattr(envs.subprocess, "run", fake_run)
    return calls


def test_agent_inside_runs_the_agent_in_the_container(make_layer: object, monkeypatch: object) -> None:
    layer = _podman_layer(make_layer, agentInside=True, caches=["uv", "ccache"], credentials=["claude"])
    calls = _fake_podman(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    state = _state()
    stack = envs.build(layer, state, ["wt", "box"])
    stack.setup()
    argv = stack.wrap_agent(["claude", "-p", "hi"])
    assert argv[:3] == ["podman", "exec", "-i"] and argv[-3:] == ["claude", "-p", "hi"]
    assert "ANTHROPIC_API_KEY=sk-test" in argv and "UV_CACHE_DIR=/caches/uv" in argv and "AL_CACHE_CCACHE=/caches/ccache" in argv
    start = next(call for call in calls if call[:3] == ["podman", "run", "-d"])
    assert f"{layer.layer_dir}:{layer.layer_dir}:ro" in start               # layer read-only …
    assert f"{layer.layer_dir / 'specs'}:{layer.layer_dir / 'specs'}" in start  # … except where a run writes
    assert "al-cache-uv:/caches/uv:U" in start and "--network" in start and start[start.index("--network") + 1] == "none"
    stack.teardown(success=True)


def test_agent_outside_container_gets_no_credentials(make_layer: object, monkeypatch: object) -> None:
    layer = _podman_layer(make_layer, agentInside=False, credentials=["claude"])
    _fake_podman(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    stack = envs.build(layer, _state(), ["wt", "box"])
    stack.setup()
    assert stack.wrap_agent(["claude", "-p", "hi"]) == ["claude", "-p", "hi"]   # host, unwrapped
    assert not any("ANTHROPIC_API_KEY" in arg for arg in stack.wrap("true"))
    stack.teardown(success=True)


def test_scope_command_uses_a_fresh_container_per_command(make_layer: object, monkeypatch: object) -> None:
    layer = _podman_layer(make_layer, scope="command")
    calls = _fake_podman(monkeypatch)
    stack = envs.build(layer, _state(), ["wt", "box"])
    stack.setup()
    argv = stack.wrap("make test")
    assert argv[:3] == ["podman", "run", "--rm"] and argv[-3:] == ["bash", "-c", "make test"]
    assert not any(call[:3] == ["podman", "run", "-d"] for call in calls)
    stack.teardown(success=True)


def test_port_leases_never_collide(tmp_path: Path) -> None:
    for run in ("aaaa0001", "aaaa0002"):
        (tmp_path / "agents" / run).mkdir(parents=True)
        (tmp_path / "agents" / run / "state.json").write_text("{}")
    first = envs.lease_ports(tmp_path, "aaaa0001", 24200, 2)
    second = envs.lease_ports(tmp_path, "aaaa0002", 24200, 2)
    assert first and second and not set(first) & set(second)
    assert envs.lease_ports(tmp_path, "aaaa0001", 24200, 2) == first       # stable on --resume
    envs.release_ports(tmp_path, "aaaa0001")
    (tmp_path / "agents" / "aaaa0003").mkdir()
    (tmp_path / "agents" / "aaaa0003" / "state.json").write_text("{}")
    assert envs.lease_ports(tmp_path, "aaaa0003", 24200, 2) == first       # released block is reused


def test_slots_limit_concurrency(tmp_path: Path) -> None:
    import threading
    import time

    from adw_modules import slots
    layer = SimpleNamespace(layer_dir=tmp_path, ir={"concurrency": {"maxRuns": 4, "maxAgents": 2, "maxChecks": 1, "waitSeconds": 30}})
    active, peak, lock = 0, 0, threading.Lock()
    def work() -> None:
        nonlocal active, peak
        with slots.slot(layer, "agents", poll=0.01):
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
    threads = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak == 2


def test_slot_wait_times_out(tmp_path: Path) -> None:
    import pytest

    from adw_modules import slots
    from adw_modules.errors import StepFailed
    layer = SimpleNamespace(layer_dir=tmp_path, ir={"concurrency": {"maxRuns": 1, "maxAgents": 1, "maxChecks": 1, "waitSeconds": 0}})
    with slots.slot(layer, "runs"):
        with pytest.raises(StepFailed, match="no free runs slot"):
            with slots.slot(layer, "runs", poll=0.01):
                pass


def test_merged_branch_is_deleted_and_unmerged_kept(make_layer: object) -> None:
    layer = load(make_layer())
    app = str(layer.app_root)
    def git(*args: str) -> None:
        envs.subprocess.run(["git", "-C", app, "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True)
    git("branch", "chore/empty")                                        # nothing beyond the default branch
    git("branch", "chore/work")
    git("worktree", "add", "-q", str(layer.layer_dir / "trees" / "w"), "chore/work")
    (layer.layer_dir / "trees" / "w" / "new.txt").write_text("x")
    envs.subprocess.run(["git", "-C", str(layer.layer_dir / "trees" / "w"), "add", "-A"], check=True)
    envs.subprocess.run(["git", "-C", str(layer.layer_dir / "trees" / "w"), "-c", "user.name=t", "-c", "user.email=t@t",
                         "commit", "-qm", "work"], check=True)
    git("worktree", "remove", "--force", str(layer.layer_dir / "trees" / "w"))
    assert envs.delete_branch_if_merged(layer, "chore/empty") is True
    assert envs.delete_branch_if_merged(layer, "chore/work") is False   # unshipped work is never deleted
    branches = envs.subprocess.run(["git", "-C", app, "branch", "--list"], capture_output=True, text=True).stdout
    assert "chore/work" in branches and "chore/empty" not in branches


def test_port_block_skips_busy_slot(tmp_path: Path) -> None:
    import socket

    import pytest

    busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        busy.bind(("127.0.0.1", 0))
    except PermissionError:
        pytest.skip("socket bind() is not permitted in this sandbox")
    busy.listen()
    base = busy.getsockname()[1]
    try:
        ports = envs.lease_ports(tmp_path, "busy0001", base, 2)
    finally:
        busy.close()
    assert ports[0] >= base + 10 and (ports[0] - base) % 10 == 0 and ports[1] == ports[0] + 1
