"""Sandboxed (podman) runs: mounts, environment, detached worktree and host-only ref creation.

podman itself is faked here (see test_sandbox_podman.py for the real thing); git is real."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from adw_modules import envs, gitsafe, sandbox
from adw_modules.errors import LayerError, StepFailed
from adw_modules.ir import load
from adw_modules.runner import main
from adw_modules.state import ADWState

ADW_ID = "5a5a0001"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    if check:
        assert result.returncode == 0, result.stderr
    return result


def _podman_option(**extra) -> dict:
    return {"id": "box", "kind": "podman", "agentInside": False, "baseImage": "debian", "cpus": "2", "memory": "4g",
            "network": "none", "packages": [], "setup": [], "status": "stable", "scope": "run", "caches": [],
            "credentials": [], **extra}


def _worktree_option(**extra) -> dict:
    return {"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 24600, "portsPerRun": 0,
            "keep": "never", "status": "stable", **extra}


def _fake_podman(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real_run = envs.subprocess.run

    def fake_run(argv, **kwargs):
        if argv[0] != "podman":
            return real_run(argv, **kwargs)
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stderr="", stdout="")
    monkeypatch.setattr(envs.subprocess, "run", fake_run)
    monkeypatch.setattr(sandbox, "doctor", lambda *a, **k: [])  # the preflight finds everything it needs
    return calls


def _stack(make_layer, monkeypatch, *, podman=None, worktree=None):
    layer = load(make_layer({"environments": [_worktree_option(**(worktree or {})), _podman_option(**(podman or {}))]}))
    calls = _fake_podman(monkeypatch)
    state = ADWState(ADW_ID, "test", prompt="Sandbox me please", klass="chore")
    stack = envs.build(layer, state, ["wt", "box"])
    stack.setup()
    return layer, state, stack, calls


def test_sandboxed_mounts_and_object_environment(make_layer, monkeypatch) -> None:
    layer, state, stack, calls = _stack(make_layer, monkeypatch)
    app_git = layer.app_root / ".git"
    admin = app_git / "worktrees" / ADW_ID
    quarantine = (layer.layer_dir / "agents" / ADW_ID / "objects").resolve()
    argv = stack.wrap("true")
    start = next(call for call in calls if call[:3] == ["podman", "run", "-d"])
    assert f"{app_git}:{app_git}:ro" in start                      # app .git read-only …
    assert f"{app_git}:{app_git}" not in start                     # … and never read-write
    assert f"{admin}:{admin}" in start                             # the run's own admin dir
    assert f"{quarantine}:{quarantine}" in start                   # the per-run object quarantine
    assert f"{state.worktree}:{state.worktree}" in start
    assert not any(arg.split(":")[0] == str(layer.app_root) for arg in start)   # app working tree never mounted
    rw_under_git = [arg for arg in start if arg.startswith(str(app_git)) and arg.count(":") == 1]
    assert sorted(rw_under_git) == sorted([f"{admin}:{admin}"])    # nothing else under .git is writable on the host
    assert f"GIT_OBJECT_DIRECTORY={quarantine}" in argv
    assert f"GIT_ALTERNATE_OBJECT_DIRECTORIES={app_git / 'objects'}" in argv
    assert quarantine.is_dir()
    stack.teardown(success=True)


def test_sandboxed_worktree_is_detached_and_branch_ref_is_not_created(make_layer, monkeypatch, app_repo) -> None:
    layer, state, stack, _ = _stack(make_layer, monkeypatch, worktree={"keep": "always"})
    assert state.branch.startswith(f"chore/{ADW_ID}-sandbox-me-please")
    assert _git(Path(state.worktree), "symbolic-ref", "-q", "HEAD", check=False).returncode != 0   # detached
    assert _git(app_repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{state.branch}", check=False).returncode != 0
    assert (Path(state.worktree) / ".git").read_text() == f"gitdir: {layer.app_root / '.git' / 'worktrees' / ADW_ID}\n"
    stack.teardown(success=True)


def test_non_sandboxed_worktree_keeps_branch_behaviour(make_layer, app_repo) -> None:
    layer = load(make_layer({"environments": [_worktree_option()]}))
    state = ADWState(ADW_ID, "test", prompt="plain run", klass="chore")
    stack = envs.build(layer, state, ["wt"])
    stack.setup()
    assert stack.sandbox is None
    assert _git(Path(state.worktree), "symbolic-ref", "HEAD").stdout.strip() == f"refs/heads/{state.branch}"
    stack.teardown(success=True)


def test_sparse_worktree_is_refused_in_a_sandbox(make_layer, monkeypatch) -> None:
    with pytest.raises(LayerError, match="sparse"):
        _stack(make_layer, monkeypatch, worktree={"sparse": ["src"]})


def test_host_commit_goes_to_quarantine_and_publish_migrates(make_layer, monkeypatch, app_repo) -> None:
    layer, state, stack, _ = _stack(make_layer, monkeypatch, worktree={"keep": "always"})
    sandbox = stack.sandbox
    (Path(state.worktree) / "new.txt").write_text("new\n")
    gitsafe.run(sandbox, ["add", "-A"], "test")
    gitsafe.run(sandbox, ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "new"], "test")
    commit = gitsafe.run(sandbox, ["rev-parse", "HEAD"], "test").stdout.strip()
    assert (sandbox.quarantine / commit[:2] / commit[2:]).is_file()
    assert _git(app_repo, "cat-file", "-e", commit, check=False).returncode != 0      # not in the app store yet
    gitsafe.import_objects(sandbox, commit, "test")
    assert _git(app_repo, "cat-file", "-e", f"{commit}^{{tree}}").returncode == 0
    gitsafe.update_ref(sandbox, f"refs/heads/{state.branch}", commit, "test")
    assert _git(app_repo, "rev-parse", f"refs/heads/{state.branch}").stdout.strip() == commit
    assert _git(app_repo, "fsck", "--connectivity-only").returncode == 0
    stack.teardown(success=True)


def test_update_ref_is_fast_forward_only(make_layer, monkeypatch, app_repo) -> None:
    layer, state, stack, _ = _stack(make_layer, monkeypatch, worktree={"keep": "always"})
    sandbox = stack.sandbox
    _git(app_repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-qm", "moved on")
    stale = _git(app_repo, "rev-parse", "HEAD~1").stdout.strip()
    with pytest.raises(StepFailed, match="fast-forward"):
        gitsafe.update_ref(sandbox, "refs/heads/main", stale, "test")
    stack.teardown(success=True)


def test_publish_refuses_foreign_branch_names(make_layer, monkeypatch) -> None:
    layer, state, stack, _ = _stack(make_layer, monkeypatch, worktree={"keep": "always"})
    for bad in ("main", "al/ship/main", f"chore/{ADW_ID}-x/../../main", "chore/ffffffff-other-run"):
        with pytest.raises(StepFailed, match="branch"):
            envs.check_run_branch(bad, ADW_ID)
    envs.check_run_branch(state.branch, ADW_ID)
    stack.teardown(success=True)


def _sandbox_layer(make_layer, app_repo, *, keep="never", ship="merge") -> Path:
    layer = make_layer({
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "commands": [{"name": "plan", "template": "plan", "modelSet": "base", "output": "path"},
                     {"name": "build", "template": "build", "modelSet": "base", "output": "text"},
                     {"name": "review", "template": "review", "modelSet": "base", "output": "json"}],
        "codebase": {"commands": []},
        "reviews": [{"id": "policy", "kind": "auto", "status": "stable", "command": "review", "reviewers": 1,
                     "maxRetries": 0, "artefacts": [], "allowedClasses": ["chore"], "blockProtectedPaths": True,
                     "maxDiffLines": 100, "requireChecks": [], "ship": ship}],
        "environments": [_worktree_option(keep=keep), _podman_option()],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "adws": [{"name": "demo", "agent": "mock", "review": "policy", "environment": ["wt", "box"],
                  "classes": ["chore"], "steps": [
                      {"name": "plan", "kind": "run", "command": "plan"},
                      {"name": "build", "kind": "run", "command": "build"},
                      {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}]}],
    })
    (layer / "env").mkdir()
    (layer / "env" / "Containerfile").write_text("FROM debian\n")   # what `al gen` writes; the preflight hashes it
    return layer


def _script(layer: Path, name: str, data: dict) -> None:
    path = layer / "mock" / "demo" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_sandboxed_run_ships_through_the_host_only(make_layer, app_repo, monkeypatch) -> None:
    layer = _sandbox_layer(make_layer, app_repo)
    _fake_podman(monkeypatch)
    _script(layer, "build", {"files": {"feature.py": "FEATURE = True\n"}})
    head = _git(app_repo, "rev-parse", "HEAD").stdout.strip()
    assert main("demo", layer, ["Add a feature", "--adw-id", ADW_ID]) == 0
    state = json.loads((layer / f"agents/{ADW_ID}/state.json").read_text())
    assert state["status"] == "passed", (layer / f"agents/{ADW_ID}/run.log").read_text()
    assert _git(app_repo, "show", "al/ship/main:feature.py").stdout == "FEATURE = True\n"
    assert state["ship"]["commit"] == _git(app_repo, "rev-parse", "al/ship/main").stdout.strip()
    assert _git(app_repo, "fsck", "--connectivity-only").returncode == 0
    assert not (layer / "agents" / ADW_ID / "objects").exists()          # quarantine removed with the worktree
    assert _git(app_repo, "status", "--porcelain").stdout == ""
    assert _git(app_repo, "rev-parse", "HEAD").stdout.strip() == head


def test_failed_sandboxed_run_keeps_unshipped_work_on_a_branch(make_layer, app_repo, monkeypatch) -> None:
    layer = _sandbox_layer(make_layer, app_repo, ship="none")
    _fake_podman(monkeypatch)
    _script(layer, "build", {"files": {"feature.py": "FEATURE = True\n"}})
    _script(layer, "commit", {"commit": True})       # the commit step commits; the build step never does
    data = json.loads((layer / "agentic-layer.json").read_text())
    data["commands"].append({"name": "commit", "template": "commit", "modelSet": "base", "output": "text"})
    (layer / "commands" / "commit.md").write_text("# commit\n$ARGUMENTS\n", encoding="utf-8")
    # the check "runs" in a container that the fake podman never started, so it fails
    data["codebase"]["commands"] = [{"name": "test", "run": "./test.sh", "timeout": 20}]
    data["adws"][0]["steps"] = data["adws"][0]["steps"][:2] + [{"name": "commit", "kind": "run", "command": "commit"},
                                                               {"name": "test", "kind": "check", "cmd": "test"}]
    (layer / "agentic-layer.json").write_text(json.dumps(data))
    assert main("demo", layer, ["Doomed work", "--adw-id", ADW_ID]) == 1
    state = json.loads((layer / f"agents/{ADW_ID}/state.json").read_text())
    assert not Path(state["worktree"]).exists()
    assert _git(app_repo, "show", f"{state['branch']}:feature.py").stdout == "FEATURE = True\n"
    assert _git(app_repo, "fsck", "--connectivity-only").returncode == 0
    assert not (layer / "agents" / ADW_ID / "objects").exists()


def _with_origin(app_repo: Path) -> Path:
    bare = app_repo.parent / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    _git(app_repo, "remote", "add", "origin", str(bare))
    _git(app_repo, "push", "-q", "origin", "main")
    return bare


def test_sandboxed_merge_via_remote_pushes_from_the_app(make_layer, app_repo, monkeypatch) -> None:
    bare = _with_origin(app_repo)
    layer = _sandbox_layer(make_layer, app_repo)
    data = json.loads((layer / "agentic-layer.json").read_text())
    data["forge"]["remote"] = "origin"
    (layer / "agentic-layer.json").write_text(json.dumps(data))
    _fake_podman(monkeypatch)
    _script(layer, "build", {"files": {"feature.py": "FEATURE = True\n"}})
    assert main("demo", layer, ["Remote feature", "--adw-id", ADW_ID]) == 0, \
        (layer / f"agents/{ADW_ID}/run.log").read_text()
    state = json.loads((layer / f"agents/{ADW_ID}/state.json").read_text())
    assert state["ship"]["shipped"] is True and state["ship"]["target"] == "origin/main"
    assert _git(bare, "rev-parse", "refs/heads/main").stdout.strip() == state["ship"]["commit"]
    assert _git(app_repo, "fsck", "--connectivity-only").returncode == 0      # remote-tracking refs never dangle
    assert not (layer / "agents" / ADW_ID / "objects").exists()


def test_sandboxed_forge_pr_pushes_the_published_branch(make_layer, app_repo, monkeypatch) -> None:
    import sys

    import adw_modules
    bare = _with_origin(app_repo)
    layer = _sandbox_layer(make_layer, app_repo)
    data = json.loads((layer / "agentic-layer.json").read_text())
    data["forge"] = {"id": "github", "kind": "github", "status": "stable", "remote": ""}
    data["reviews"][0] = {"id": "policy", "kind": "human", "status": "stable", "reviewers": ["alice"], "draft": True}
    (layer / "agentic-layer.json").write_text(json.dumps(data))
    calls = []

    class FakeForge:
        kind = "github"

        def create_pr(self, branch, base, title, body, draft=False, reviewers=()):
            calls.append((branch, base))
            return {"number": 7, "url": "https://forge.invalid/pr/7"}
    fake_module = SimpleNamespace(get_client=lambda _layer: FakeForge())
    monkeypatch.setitem(sys.modules, "adw_modules.forge", fake_module)
    monkeypatch.setattr(adw_modules, "forge", fake_module, raising=False)
    _fake_podman(monkeypatch)
    _script(layer, "build", {"files": {"feature.py": "FEATURE = True\n"}})
    assert main("demo", layer, ["Open a PR", "--adw-id", ADW_ID]) == 0, \
        (layer / f"agents/{ADW_ID}/run.log").read_text()
    state = json.loads((layer / f"agents/{ADW_ID}/state.json").read_text())
    assert calls == [(state["branch"], "main")]
    assert _git(bare, "rev-parse", f"refs/heads/{state['branch']}").stdout.strip() == state["ship"]["commit"]
    assert _git(app_repo, "rev-parse", f"refs/heads/{state['branch']}").stdout.strip() == state["ship"]["commit"]
    assert _git(app_repo, "fsck", "--connectivity-only").returncode == 0
