"""Ship fallback: when the gate says merge/deploy and shipping then fails, the run hands the
change to a human (PR on the forge, or PR.md) instead of failing; nothing is lost, nothing is
deployed. Git is real; the forge is a fake; the gate is the real one, wrapped so the target can
move between the decision and the ship (the race that makes a decided merge fail)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import adw_modules
from adw_modules import envs, gitsafe, review, sandbox, ship
from adw_modules.errors import StepFailed
from adw_modules.runner import main

ADW_ID = "5f5f0001"


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                            capture_output=True, text=True, timeout=30)
    if check:
        assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _layer(make_layer, *, ship_action="merge", forge=None, sandboxed=False) -> Path:
    layer = _layer_ir(make_layer, ship_action=ship_action, forge=forge, sandboxed=sandboxed)
    if sandboxed:
        (layer / "env").mkdir(exist_ok=True)
        (layer / "env" / "Containerfile").write_text("FROM debian\n")   # what `al gen` writes; the preflight hashes it
    return layer


def _layer_ir(make_layer, *, ship_action="merge", forge=None, sandboxed=False) -> Path:
    environments = [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 27600,
                     "portsPerRun": 0, "keep": "never", "status": "stable"}]
    if sandboxed:
        environments.append({"id": "box", "kind": "podman", "agentInside": False, "baseImage": "debian", "cpus": "1",
                             "memory": "1g", "network": "none", "packages": [], "setup": [], "status": "stable",
                             "scope": "run", "caches": [], "credentials": []})
    return make_layer({
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "commands": [{"name": "build", "template": "build", "modelSet": "base", "output": "text"},
                     {"name": "review", "template": "review", "modelSet": "base", "output": "json"}],
        "codebase": {"commands": [{"name": "deploy", "run": "touch \"$DEPLOY_MARKER\"", "timeout": 20}]},
        "reviews": [{"id": "policy", "kind": "auto", "status": "stable", "command": "review", "reviewers": 1,
                     "maxRetries": 0, "artefacts": [], "allowedClasses": ["chore"], "blockProtectedPaths": True,
                     "maxDiffLines": 100, "requireChecks": [], "ship": ship_action, "deployCmd": "deploy"}],
        "environments": environments,
        "forge": forge or {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "adws": [{"name": "demo", "agent": "mock", "review": "policy",
                  "environment": ["wt", "box"] if sandboxed else ["wt"], "classes": ["chore"], "steps": [
                      {"name": "build", "kind": "run", "command": "build"},
                      {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}]}],
    })


@pytest.fixture
def deploy_marker(tmp_path, monkeypatch) -> Path:
    marker = tmp_path / "deployed"
    monkeypatch.setenv("DEPLOY_MARKER", str(marker))
    return marker


def _build(layer: Path) -> None:
    path = layer / "mock" / "demo" / "build.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"files": {"feature.py": "FEATURE = True\n"}}), encoding="utf-8")


def _after_gate(monkeypatch, action) -> None:
    """Run `action` right after the real gate decided, before ship acts on the decision."""
    real_gate = review.gate

    def gate(*args, **kwargs):
        decision = real_gate(*args, **kwargs)
        assert decision["action"] in {"merge", "deploy"}, decision     # the precondition of every test here
        action()
        return decision
    monkeypatch.setattr(review, "gate", gate)


def _conflicting_commit(repo: Path, ref: str = "refs/heads/main") -> str:
    """Move `ref` to a new commit that adds a conflicting feature.py, without touching a working tree."""
    blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input="FEATURE = False\n",
                          capture_output=True, text=True, check=True).stdout.strip()
    parent = _git(repo, "rev-parse", ref)
    index = repo.parent / f"index-{abs(hash(str(repo)))}"
    env = {"GIT_INDEX_FILE": str(index)}
    subprocess.run(["git", "-C", str(repo), "read-tree", parent], env={**os.environ, **env}, check=True)
    subprocess.run(["git", "-C", str(repo), "update-index", "--add", "--cacheinfo", f"100644,{blob},feature.py"],
                   env={**os.environ, **env}, check=True)
    tree = subprocess.run(["git", "-C", str(repo), "write-tree"], env={**os.environ, **env}, capture_output=True,
                          text=True, check=True).stdout.strip()
    commit = _git(repo, "commit-tree", tree, "-p", parent, "-m", "someone else shipped first")
    _git(repo, "update-ref", ref, commit, parent)
    return commit


def _state(layer: Path) -> dict:
    return json.loads((layer / f"agents/{ADW_ID}/state.json").read_text())


def _assert_fell_back(layer: Path, app: Path, deploy_marker: Path, decided: str) -> dict:
    state = _state(layer)
    log = (layer / f"agents/{ADW_ID}/run.log").read_text()
    assert state["status"] == "passed", log
    assert state["ship"]["action"] == "pr" and state["ship"]["shipped"] is False
    assert any(reason.startswith("ship-failed:") for reason in state["ship"]["reasons"]), state["ship"]["reasons"]
    assert state["ship"]["fallback"]["decided"] == decided
    assert not deploy_marker.exists()                                         # never deployed after a fallback
    if decided == "deploy":
        assert "fell back" in state["ship"]["deploy"]["skipped"]
    # the run's work is reachable on its branch in the app, exactly the commit the ship recorded
    assert _git(app, "rev-parse", f"refs/heads/{state['branch']}") == state["ship"]["commit"]
    assert _git(app, "show", f"{state['branch']}:feature.py") == "FEATURE = True"
    assert subprocess.run(["git", "-C", str(app), "fsck", "--connectivity-only"], capture_output=True).returncode == 0
    return state


def _fake_forge(monkeypatch, *, merge_error=None, create_errors=0, comments=None) -> list:
    created: list = []

    class FakeForge:
        kind = "github"

        def create_pr(self, branch, base, title, body, draft=False, reviewers=()):
            if len(created) < create_errors:
                created.append(None)
                raise RuntimeError("forge says: 502 bad gateway")
            created.append((branch, base, body))
            return {"number": 41, "url": "https://forge.invalid/pr/41"}

        def merge_pr(self, number, method="squash"):
            if merge_error:
                raise merge_error

        def comment(self, number, body):
            if comments is None:
                raise RuntimeError("comments unsupported")
            comments.append((number, body))
    fake_module = SimpleNamespace(get_client=lambda _layer: FakeForge())
    monkeypatch.setitem(sys.modules, "adw_modules.forge", fake_module)
    monkeypatch.setattr(adw_modules, "forge", fake_module, raising=False)
    return created


def _with_origin(app: Path) -> Path:
    bare = app.parent / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    _git(app, "remote", "add", "origin", str(bare))
    _git(app, "push", "-q", "origin", "main")
    return bare


# ---------------------------------------------------------------- merge conflicts with the target


@pytest.mark.parametrize("decided", ["merge", "deploy"])
def test_default_branch_moved_after_the_gate_falls_back_to_pr_md(make_layer, app_repo, monkeypatch, deploy_marker,
                                                                   decided) -> None:
    _git(app_repo, "checkout", "-q", "-b", "dev")   # main not checked out: ship fast-forwards it directly
    layer = _layer(make_layer, ship_action=decided)
    _build(layer)
    moved: list[str] = []
    _after_gate(monkeypatch, lambda: moved.append(_conflicting_commit(app_repo)))
    assert main("demo", layer, ["Add a feature", "--adw-id", ADW_ID]) == 0
    state = _assert_fell_back(layer, app_repo, deploy_marker, decided)
    assert _git(app_repo, "rev-parse", "refs/heads/main") == moved[0]          # main kept the other change
    assert state["ship"]["target"] == f"agents/{ADW_ID}/PR.md" and state["ship"]["pr"] is None
    text = (layer / state["ship"]["target"]).read_text()
    assert "shipping failed" in text and state["branch"] in text
    assert not Path(state["worktree"]).exists()                                # worktree gone, branch kept


def test_sandboxed_target_moved_after_the_gate_falls_back(make_layer, app_repo, monkeypatch, deploy_marker) -> None:
    _git(app_repo, "checkout", "-q", "-b", "dev")
    layer = _layer(make_layer, ship_action="deploy", sandboxed=True)
    real_run = envs.subprocess.run
    monkeypatch.setattr(envs.subprocess, "run", lambda argv, **kw: SimpleNamespace(returncode=0, stdout="", stderr="")
                        if argv[0] == "podman" else real_run(argv, **kw))
    monkeypatch.setattr(sandbox, "doctor", lambda *a, **k: [])  # the preflight finds everything it needs
    _build(layer)
    _after_gate(monkeypatch, lambda: _conflicting_commit(app_repo))
    assert main("demo", layer, ["Add a feature", "--adw-id", ADW_ID]) == 0
    state = _assert_fell_back(layer, app_repo, deploy_marker, "deploy")
    assert "not a fast-forward" in state["ship"]["fallback"]["error"]         # gitsafe refused, ff-only
    assert not (layer / "agents" / ADW_ID / "objects").exists()                # quarantine gone; objects in the app


# ---------------------------------------------------------------- rejected remote push


@pytest.mark.parametrize("failure", ["remote-branch-moved", "unreachable-remote"])
def test_rejected_remote_push_falls_back_to_pr_md(make_layer, app_repo, monkeypatch, deploy_marker, failure) -> None:
    bare = _with_origin(app_repo)
    layer = _layer(make_layer, forge={"id": "none", "kind": "none", "status": "stable", "remote": "origin"})
    _build(layer)
    if failure == "remote-branch-moved":
        _after_gate(monkeypatch, lambda: _conflicting_commit(bare))
    else:
        _after_gate(monkeypatch, lambda: _git(app_repo, "remote", "set-url", "origin", str(app_repo.parent / "gone.git")))
    remote_main = _git(bare, "rev-parse", "refs/heads/main")
    assert main("demo", layer, ["Push it", "--adw-id", ADW_ID]) == 0
    state = _assert_fell_back(layer, app_repo, deploy_marker, "merge")
    if failure == "unreachable-remote":
        assert _git(bare, "rev-parse", "refs/heads/main") == remote_main         # nothing landed remotely
    assert _git(bare, "rev-parse", "refs/heads/main") != state["ship"]["commit"]
    assert (layer / f"agents/{ADW_ID}/PR.md").is_file()


# ---------------------------------------------------------------- forge refuses


def test_forge_merge_refused_leaves_the_pr_open(make_layer, app_repo, monkeypatch, deploy_marker) -> None:
    bare = _with_origin(app_repo)
    layer = _layer(make_layer, ship_action="deploy",
                   forge={"id": "github", "kind": "github", "status": "stable", "remote": ""})
    comments: list = []
    created = _fake_forge(monkeypatch, merge_error=RuntimeError("merge blocked: required status check pending"),
                          comments=comments)
    _build(layer)
    assert main("demo", layer, ["Ship via forge", "--adw-id", ADW_ID]) == 0
    state = _assert_fell_back(layer, app_repo, deploy_marker, "deploy")
    assert len(created) == 1                                                   # the existing PR is reused, not re-created
    assert state["ship"]["pr"]["number"] == 41 and state["ship"]["target"] == "https://forge.invalid/pr/41"
    assert "ship-failed:merge blocked: required status check pending" in state["ship"]["reasons"]
    assert comments and comments[0][0] == 41 and "merge blocked" in comments[0][1]
    assert _git(bare, "rev-parse", f"refs/heads/{state['branch']}") == state["ship"]["commit"]


def test_forge_pr_creation_failing_once_is_retried_by_the_fallback(make_layer, app_repo, monkeypatch, deploy_marker) -> None:
    _with_origin(app_repo)
    layer = _layer(make_layer, forge={"id": "github", "kind": "github", "status": "stable", "remote": ""})
    created = _fake_forge(monkeypatch, create_errors=1)
    _build(layer)
    assert main("demo", layer, ["Ship via forge", "--adw-id", ADW_ID]) == 0
    state = _assert_fell_back(layer, app_repo, deploy_marker, "merge")
    assert created[0] is None and created[1][0] == state["branch"]            # fallback opened the PR
    assert "shipping failed" in created[1][2]                                  # and says why a human is needed


# ---------------------------------------------------------------- the fallback itself fails


def test_fallback_failing_too_is_a_real_failure_with_a_forge(make_layer, app_repo, monkeypatch, deploy_marker) -> None:
    _with_origin(app_repo)
    layer = _layer(make_layer, forge={"id": "github", "kind": "github", "status": "stable", "remote": ""})
    _fake_forge(monkeypatch, create_errors=99)
    _build(layer)
    assert main("demo", layer, ["Ship via forge", "--adw-id", ADW_ID]) == 1
    state = _state(layer)
    ship_step = next(step for step in state["steps"] if step["name"] == "ship")
    assert state["status"] == "failed" and ship_step["status"] == "failed"
    assert "fallback to a human PR failed too" in ship_step["detail"]
    assert not deploy_marker.exists()
    assert _git(app_repo, "show", f"{state['branch']}:feature.py") == "FEATURE = True"   # still nothing lost


def test_fallback_failing_too_is_a_real_failure_without_a_forge(make_layer, app_repo, monkeypatch, deploy_marker) -> None:
    _git(app_repo, "checkout", "-q", "-b", "dev")
    layer = _layer(make_layer)
    _build(layer)
    (layer / "agents" / ADW_ID / "PR.md").mkdir(parents=True)               # PR.md cannot be written
    _after_gate(monkeypatch, lambda: _conflicting_commit(app_repo))
    assert main("demo", layer, ["Add a feature", "--adw-id", ADW_ID]) == 1
    state = _state(layer)
    ship_step = next(step for step in state["steps"] if step["name"] == "ship")
    assert state["status"] == "failed" and "fallback to a human PR failed too" in ship_step["detail"]
    assert _git(app_repo, "show", f"{state['branch']}:feature.py") == "FEATURE = True"


def test_integrity_failure_is_never_turned_into_a_pr(make_layer, app_repo, monkeypatch, deploy_marker) -> None:
    _git(app_repo, "checkout", "-q", "-b", "dev")
    layer = _layer(make_layer)
    _build(layer)

    def tampered(*args, **kwargs):
        raise StepFailed(gitsafe.INTEGRITY, "sandboxed worktree refused: HEAD rewritten")
    monkeypatch.setattr(ship, "_merge_locally", tampered)
    assert main("demo", layer, ["Add a feature", "--adw-id", ADW_ID]) == 1
    assert not (layer / f"agents/{ADW_ID}/PR.md").exists()
    assert "git-integrity" in next(step for step in _state(layer)["steps"] if step["name"] == "ship")["detail"]


def test_successful_merge_still_deploys(make_layer, app_repo, monkeypatch, deploy_marker) -> None:
    _git(app_repo, "checkout", "-q", "-b", "dev")
    layer = _layer(make_layer, ship_action="deploy")
    _build(layer)
    assert main("demo", layer, ["Add a feature", "--adw-id", ADW_ID]) == 0
    state = _state(layer)
    assert state["ship"]["shipped"] is True and "fallback" not in state["ship"] and deploy_marker.exists()
