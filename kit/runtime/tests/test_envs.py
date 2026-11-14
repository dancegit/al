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
    assert argv[argv.index("--userns=keep-id") + 2] == f"{state.worktree}:/work:Z"
    assert f"ADW_PORT_0={state.ports[0]}" in argv and "al-test-layer:latest" in argv
    assert argv[:3] == ["podman", "run", "--rm"]
    assert "--network" not in argv and "--memory" in argv and "--cpus" in argv
    assert argv[-3:] == ["bash", "-c", "pytest -q"]
    assert stack.wrap_agent(["agent", "--version"])[0:3] == ["nix", "develop", f"path:{layer.layer_dir / 'env'}"]
    assert envs.EnvStack(layer, state, [layer.option("environments", "nix")]).wrap("true")[:3] == ["nix", "develop", f"path:{layer.layer_dir / 'env'}"]
    stack.teardown(success=True)


def test_port_block_skips_busy_slot() -> None:
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
        ports = envs._free_port_block(base, 2)
    finally:
        busy.close()
    assert ports[0] >= base + 10 and (ports[0] - base) % 10 == 0 and ports[1] == ports[0] + 1
