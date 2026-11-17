"""A workflow without a ship step keeps its work: what the agents left in the worktree is
committed on the run's branch and copied into the run's artefacts before teardown, and the
summary says so. Git is real, the agent is the mock; a sandboxed run fakes podman."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from adw_modules import envs, keep, sandbox, steps
from adw_modules.errors import StepFailed
from adw_modules.runner import main

ID = "4ee90001"
REPORT = "".join(f"line {n} of the report\n" for n in range(1, 937))


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    if check:
        assert result.returncode == 0, result.stderr
    return result


def _layer(make_layer, *, copy_files=None, sandboxed=False, with_ship=False) -> Path:
    environments = [{"id": "worktree", "kind": "worktree", "dir": "trees", "copyFiles": copy_files or [],
                     "portBase": 27800, "portsPerRun": 0, "keep": "never", "status": "stable"}]
    if sandboxed:
        environments.append({"id": "box", "kind": "podman", "agentInside": False, "baseImage": "debian", "cpus": "1",
                             "memory": "1g", "network": "none", "packages": [], "setup": [], "status": "stable",
                             "scope": "run", "caches": [], "credentials": []})
    adw_steps = [{"name": "build", "kind": "run", "command": "build"}]
    if with_ship:
        adw_steps += [{"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}]
    layer = make_layer({
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "commands": [{"name": "build", "template": "build", "modelSet": "base", "output": "text"},
                     {"name": "review", "template": "review", "modelSet": "base", "output": "json"}],
        "reviews": [{"id": "person", "kind": "human", "status": "stable", "command": "review", "reviewers": [],
                     "maxRetries": 0, "artefacts": [], "allowedClasses": ["chore"], "blockProtectedPaths": True,
                     "maxDiffLines": 100, "requireChecks": [], "ship": "pr", "deployCmd": ""},
                    {"id": "policy", "kind": "auto", "status": "stable", "command": "review", "reviewers": 1,
                     "maxRetries": 0, "artefacts": [], "allowedClasses": ["chore"], "blockProtectedPaths": True,
                     "maxDiffLines": 100, "requireChecks": [], "ship": "merge", "deployCmd": ""}],
        "environments": environments,
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "adws": [{"name": "pb", "agent": "mock", "review": "policy" if with_ship else "person",
                  "environment": ["worktree", "box"] if sandboxed else ["worktree"], "classes": ["chore"],
                  "steps": adw_steps}],
    })
    if sandboxed:
        (layer / "env").mkdir(exist_ok=True)
        (layer / "env" / "Containerfile").write_text("FROM debian\n")
    return layer


def _mock(layer: Path, data: dict) -> None:
    path = layer / "mock" / "pb" / "build.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _state(layer: Path) -> dict:
    return json.loads((layer / "agents" / ID / "state.json").read_text())


def _kept(layer: Path) -> Path:
    return layer / "artefacts" / ID / "kept-files"


def _branch_exists(app: Path, branch: str) -> bool:
    return bool(branch) and _git(app, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
                                 check=False).returncode == 0


def test_untracked_report_is_kept_on_branch_and_as_artefact(make_layer, app_repo, capsys):
    layer = _layer(make_layer)
    _mock(layer, {"files": {"docs/report.md": REPORT}})
    assert main("pb", layer, ["Write the report", "--adw-id", ID]) == 0
    out = capsys.readouterr().out
    state = _state(layer)
    branch = state["branch"]
    assert state["status"] == "passed"
    assert not Path(state["worktree"]).exists()
    assert _branch_exists(app_repo, branch)
    assert _git(app_repo, "show", f"{branch}:docs/report.md").stdout == REPORT
    message = _git(app_repo, "log", "-1", "--format=%s", branch).stdout
    assert message.startswith(f"work of run {ID}, kept because the workflow has no ship step")
    assert (_kept(layer) / "docs" / "report.md").read_text() == REPORT
    assert any(item["id"] == "kept-files" for item in state["artefacts"])
    assert "+++ b/docs/report.md" in (layer / "artefacts" / ID / "kept-diff" / "changes.diff").read_text()
    assert state["kept"]["count"] == 1
    assert state["kept"]["diff"] == f"artefacts/{ID}/kept-diff/changes.diff"
    assert f"kept:      branch {branch} (1 files)" in out
    assert f"delivery:  none (no ship step); work kept on branch {branch}" in out
    assert "delivered=None" not in out
    assert "branch deleted" not in out


def test_tracked_change_is_kept(make_layer, app_repo):
    layer = _layer(make_layer)
    _mock(layer, {"files": {"app.py": "print('changed')\n"}})
    assert main("pb", layer, ["Change the app", "--adw-id", ID]) == 0
    state = _state(layer)
    assert _git(app_repo, "show", f"{state['branch']}:app.py").stdout == "print('changed')\n"
    assert (_kept(layer) / "app.py").read_text() == "print('changed')\n"
    assert state["kept"]["files"] == ["app.py"]


def test_tracked_and_untracked_are_kept_in_one_commit(make_layer, app_repo):
    layer = _layer(make_layer)
    _mock(layer, {"files": {"app.py": "print('changed')\n", "docs/report.md": REPORT}})
    assert main("pb", layer, ["Both", "--adw-id", ID]) == 0
    state = _state(layer)
    branch = state["branch"]
    assert state["kept"]["count"] == 2
    assert _git(app_repo, "rev-list", "--count", f"main..{branch}").stdout.strip() == "1"
    assert (_kept(layer) / "app.py").is_file() and (_kept(layer) / "docs" / "report.md").is_file()


def test_large_file_is_on_branch_but_listed_too_large(make_layer, app_repo, monkeypatch, capsys):
    monkeypatch.setattr(keep, "MAX_FILE_BYTES", 1000)
    layer = _layer(make_layer)
    _mock(layer, {"files": {"big.txt": "x" * 5000, "small.md": "small\n"}})
    assert main("pb", layer, ["Large", "--adw-id", ID]) == 0
    out = capsys.readouterr().out
    state = _state(layer)
    assert _git(app_repo, "show", f"{state['branch']}:big.txt").stdout == "x" * 5000
    assert not (_kept(layer) / "big.txt").exists()
    assert (_kept(layer) / "small.md").is_file()
    assert state["kept"]["too_large"] == [{"path": "big.txt", "bytes": 5000}]
    assert "1 too large" in out


def test_run_that_changed_nothing_leaves_nothing(make_layer, app_repo, capsys):
    layer = _layer(make_layer)
    _mock(layer, {"output": "nothing to do"})
    assert main("pb", layer, ["Nothing", "--adw-id", ID]) == 0
    out = capsys.readouterr().out
    state = _state(layer)
    assert not _branch_exists(app_repo, state["branch"])
    assert not _kept(layer).exists()
    assert state["kept"] == {"changed": False, "workflow_ships": False}
    assert "delivery:  nothing changed" in out
    assert "kept:" not in out


def test_commits_without_changed_files_are_nothing_changed(make_layer, app_repo, monkeypatch, capsys):
    layer = _layer(make_layer)
    _mock(layer, {"output": "nothing to do"})
    real_run_step = steps.run_step

    def run_step(layer_, state, adw, step, stack):
        result = real_run_step(layer_, state, adw, step, stack)
        if step.get("name") == "build":
            _git(Path(state.worktree), "-c", "user.name=Mock", "-c", "user.email=mock@example.invalid",
                 "commit", "-q", "--allow-empty", "-m", "empty")
        return result
    monkeypatch.setattr(steps, "run_step", run_step)
    assert main("pb", layer, ["Empty commit", "--adw-id", ID]) == 0
    out = capsys.readouterr().out
    state = _state(layer)
    assert state["kept"] == {"changed": False, "workflow_ships": False}
    assert not (layer / "artefacts" / ID / "kept-diff").exists()
    assert "delivery:  nothing changed" in out
    assert "kept:" not in out


def test_dry_run_behaves_as_today(make_layer, app_repo, capsys):
    layer = _layer(make_layer)
    _mock(layer, {"files": {"docs/report.md": REPORT}})
    assert main("pb", layer, ["Write the report", "--dry-run", "--adw-id", ID]) == 0
    out = capsys.readouterr().out
    state = _state(layer)
    assert "kept" not in state
    assert not _kept(layer).exists()
    assert "kept:" not in out and "delivery:" not in out


def test_failed_before_changing_anything_behaves_as_today(make_layer, app_repo):
    layer = _layer(make_layer)
    _mock(layer, {"fail": "boom"})
    assert main("pb", layer, ["Fail", "--adw-id", ID]) == 1
    state = _state(layer)
    assert state["status"] == "failed"
    assert not _branch_exists(app_repo, state["branch"])
    assert not _kept(layer).exists()


def test_copy_files_are_never_kept(make_layer, app_repo):
    (app_repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    layer = _layer(make_layer, copy_files=[".env"])
    _mock(layer, {"files": {"notes.md": "notes\n"}})
    assert main("pb", layer, ["Notes", "--adw-id", ID]) == 0
    state = _state(layer)
    tracked = _git(app_repo, "ls-tree", "-r", "--name-only", state["branch"]).stdout.split()
    assert "notes.md" in tracked and ".env" not in tracked
    assert not (_kept(layer) / ".env").exists()
    assert (_kept(layer) / "notes.md").is_file()


def test_symlink_in_worktree_is_not_followed(make_layer, app_repo, monkeypatch):
    layer = _layer(make_layer)
    _mock(layer, {"files": {"notes.md": "notes\n"}})
    real_run_step = steps.run_step

    def run_step(layer_, state, adw, step, stack):
        result = real_run_step(layer_, state, adw, step, stack)
        if step.get("name") == "build":
            os.symlink("/etc/hostname", Path(state.worktree) / "leak")
        return result
    monkeypatch.setattr(steps, "run_step", run_step)
    assert main("pb", layer, ["Notes", "--adw-id", ID]) == 0
    state = _state(layer)
    assert not (_kept(layer) / "leak").exists() and not (_kept(layer) / "leak").is_symlink()
    assert "leak" in state["kept"]["skipped"]
    assert (_kept(layer) / "notes.md").is_file()


def test_keep_failure_keeps_the_worktree(make_layer, app_repo, monkeypatch, capsys):
    def broken(*_args, **_kwargs):
        raise StepFailed("keep", "boom")
    monkeypatch.setattr(keep, "_commit", broken)
    layer = _layer(make_layer)
    _mock(layer, {"files": {"docs/report.md": REPORT}})
    assert main("pb", layer, ["Write the report", "--adw-id", ID]) == 0
    out = capsys.readouterr().out
    state = _state(layer)
    assert Path(state["worktree"]).is_dir()
    assert (Path(state["worktree"]) / "docs" / "report.md").read_text() == REPORT
    assert "boom" in state["kept"]["error"]
    assert "kept:      FAILED" in out
    assert "keeping failed, the worktree is kept" in out


def test_workflow_with_ship_step_is_untouched(make_layer, app_repo):
    layer = _layer(make_layer, with_ship=True)
    _mock(layer, {"files": {"feature.py": "FEATURE = True\n"}})
    assert main("pb", layer, ["Add a feature", "--adw-id", ID]) == 0
    state = _state(layer)
    assert "kept" not in state
    assert not _kept(layer).exists()
    assert state["ship"]["action"] == "merge"


def test_sandboxed_run_keeps_on_published_branch(make_layer, app_repo, monkeypatch):
    layer = _layer(make_layer, sandboxed=True)
    real_run = envs.subprocess.run
    monkeypatch.setattr(envs.subprocess, "run", lambda argv, **kw: SimpleNamespace(returncode=0, stdout="", stderr="")
                        if argv[0] == "podman" else real_run(argv, **kw))
    monkeypatch.setattr(sandbox, "doctor", lambda *a, **k: [])
    _mock(layer, {"files": {"docs/report.md": REPORT}})
    assert main("pb", layer, ["Write the report", "--adw-id", ID]) == 0
    state = _state(layer)
    assert state["status"] == "passed", (layer / "agents" / ID / "run.log").read_text()
    assert _branch_exists(app_repo, state["branch"])
    assert _git(app_repo, "show", f"{state['branch']}:docs/report.md").stdout == REPORT
    assert (_kept(layer) / "docs" / "report.md").is_file()
    assert not (layer / "agents" / ID / "objects").exists()
