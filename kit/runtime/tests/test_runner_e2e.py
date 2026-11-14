from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from adw_modules.runner import main


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _layer(make_layer, app_repo: Path, *, steps=None, review=None, keep="always", commands=None,
           extra_commands=None, adw_id="1234abcd") -> Path:
    command_defs = [
        {"name": "classify", "template": "classify", "modelSet": "base", "output": "json"},
        {"name": "plan", "template": "plan", "modelSet": "base", "output": "path"},
        {"name": "build", "template": "build", "modelSet": "base", "output": "text"},
        {"name": "resolve-test", "template": "resolve-test", "modelSet": "base", "output": "text"},
        {"name": "review", "template": "review", "modelSet": "base", "output": "json"},
        *(extra_commands or []),
    ]
    layer_dir = make_layer({
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "commands": command_defs,
        "codebase": {"commands": commands or [{"name": "test", "run": "./test.sh", "timeout": 5}]},
        "reviews": [{"id": "policy", "kind": "auto", "status": "stable", "command": "review", "reviewers": 1,
                     "maxRetries": 0, "artefacts": [], "allowedClasses": ["chore", "bug"],
                     "blockProtectedPaths": True, "maxDiffLines": 100, "requireChecks": ["test"], "ship": "merge",
                     **(review or {})}],
        "environments": [{"id": "worktree", "kind": "worktree", "dir": "trees", "copyFiles": [],
                          "portBase": 26000, "portsPerRun": 0, "keep": keep, "status": "stable"}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "adws": [{"name": "demo", "agent": "mock", "review": "policy", "environment": ["worktree"],
                  "classes": ["chore", "bug"], "steps": steps or [
                      {"name": "plan", "kind": "run", "command": "plan"},
                      {"name": "build", "kind": "run", "command": "build"},
                      {"name": "test", "kind": "check", "cmd": "test", "resolve": "resolve-test", "retries": 1},
                      {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}]}],
    })
    return layer_dir


def _script(layer: Path, name: str, data: dict) -> None:
    path = layer / "mock" / "demo" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_full_run_pure_local_merge_keeps_app_checkout_untouched(make_layer, app_repo):
    layer = _layer(make_layer, app_repo)
    _script(layer, "build", {"files": {"feature.py": "FEATURE = True\n"}})
    original_head = _git(app_repo, "rev-parse", "HEAD")
    original_symbolic = _git(app_repo, "symbolic-ref", "HEAD")
    assert main("demo", layer, ["Add a feature", "--adw-id", "1234abcd"]) == 0
    state = json.loads((layer / "agents/1234abcd/state.json").read_text())
    assert state["status"] == "passed"
    assert Path(state["plan"]).is_relative_to(layer / "specs")
    assert (layer / state["checks"]["test"]["log"]).is_file()
    assert _git(app_repo, "status", "--porcelain") == ""
    assert _git(app_repo, "rev-parse", "HEAD") == original_head
    assert _git(app_repo, "symbolic-ref", "HEAD") == original_symbolic
    assert _git(app_repo, "show", "al/ship/main:feature.py") == "FEATURE = True"
    assert _git(app_repo, "rev-parse", "refs/heads/main") == original_head
    assert (Path(state["worktree"]) / "feature.py").read_text() == "FEATURE = True\n"  # mock files land in the worktree
    assert not (app_repo / "feature.py").exists()
    assert [step["status"] for step in state["steps"]] == ["passed"] * 5
    assert state["ship"]["action"] == "merge" and state["ship"]["shipped"] is False
    assert "merge --ff-only al/ship/main" in state["ship"]["note"]
    assert state["ship"]["commit"] == _git(app_repo, "rev-parse", "al/ship/main")
    assert "=== ADW summary ===" in (layer / "agents/1234abcd/run.log").read_text()


def test_pure_local_merge_fast_forwards_unchecked_default_branch(make_layer, app_repo):
    _git(app_repo, "checkout", "-b", "dev")
    layer = _layer(make_layer, app_repo, adw_id="1234abce")
    _script(layer, "build", {"files": {"feature.py": "FEATURE = True\n"}})
    assert main("demo", layer, ["Add feature", "--adw-id", "1234abce"]) == 0
    assert _git(app_repo, "show", "main:feature.py") == "FEATURE = True"
    assert _git(app_repo, "status", "--porcelain") == ""
    assert _git(app_repo, "branch", "--show-current") == "dev"


def test_check_resolve_fixes_worktree_and_exhaustion_keeps_failure(make_layer, app_repo):
    layer = _layer(make_layer, app_repo, keep="on-failure")
    _script(layer, "build", {"files": {"test.sh": "#!/bin/sh\nexit 1\n"}})
    _script(layer, "test-resolve-1", {"files": {"test.sh": "#!/bin/sh\nexit 0\n"}})
    assert main("demo", layer, ["Fix tests", "--adw-id", "1234abcf"]) == 0
    resolved = json.loads((layer / "agents/1234abcf/state.json").read_text())
    assert resolved["checks"]["test"]["exit"] == 0
    assert (layer / "agents/1234abcf/checks/test.2.log").is_file()
    assert not Path(resolved["worktree"]).exists()  # keep=on-failure removes the worktree of a passing run

    failed_layer = _layer(make_layer, app_repo, keep="on-failure", adw_id="1234abd0")
    (failed_layer / "mock/demo/test-resolve-1.json").unlink(missing_ok=True)
    _script(failed_layer, "build", {"files": {"test.sh": "#!/bin/sh\nexit 1\n"}})
    assert main("demo", failed_layer, ["Break tests", "--adw-id", "1234abd0"]) == 1
    failed = json.loads((failed_layer / "agents/1234abd0/state.json").read_text())
    assert failed["status"] == "failed"
    assert Path(failed["worktree"]).is_dir()


def test_class_route_fixed_class_and_resume_skip_passed_steps(make_layer, app_repo):
    steps = [
        {"name": "classify", "kind": "run", "command": "classify"},
        {"name": "plan-chore", "kind": "run", "command": "plan", "when": "class-chore"},
        {"name": "plan-bug", "kind": "run", "command": "plan", "when": "class-bug"},
    ]
    layer = _layer(make_layer, app_repo, steps=steps, adw_id="1234abd1")
    _script(layer, "classify", {"output": '{"class":"bug"}'})
    assert main("demo", layer, ["Route me", "--adw-id", "1234abd1"]) == 0
    state = json.loads((layer / "agents/1234abd1/state.json").read_text())
    assert state["steps"][1]["status"] == "skipped" and state["steps"][2]["status"] == "passed"

    fixed_layer = _layer(make_layer, app_repo, steps=steps, adw_id="1234abd2")
    _script(fixed_layer, "classify", {"output": '{"class":"bug"}'})
    assert main("demo", fixed_layer, ["Route me", "--class", "chore", "--adw-id", "1234abd2"]) == 0
    fixed = json.loads((fixed_layer / "agents/1234abd2/state.json").read_text())
    assert fixed["klass"] == "chore" and fixed["steps"][1]["status"] == "passed"

    resume_layer = _layer(make_layer, app_repo, steps=[
        {"name": "plan", "kind": "run", "command": "plan"},
        {"name": "build", "kind": "run", "command": "build"},
        {"name": "test", "kind": "check", "cmd": "test", "retries": 0},
    ], commands=[{"name": "test", "run": "./test.sh", "timeout": 5}], adw_id="1234abd3")
    _script(resume_layer, "build", {"files": {"test.sh": "#!/bin/sh\nexit 1\n"}})
    assert main("demo", resume_layer, ["Resume me", "--adw-id", "1234abd3"]) == 1
    before = json.loads((resume_layer / "agents/1234abd3/state.json").read_text())
    Path(before["worktree"], "test.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    assert main("demo", resume_layer, ["--resume", "--adw-id", "1234abd3"]) == 0
    after = json.loads((resume_layer / "agents/1234abd3/state.json").read_text())
    attempts = {item["name"]: item["attempts"] for item in before["steps"]}
    assert {item["name"]: item["attempts"] for item in after["steps"]} == {**attempts, "test": 2}
    assert after["status"] == "passed"


def test_forge_pr_path_calls_create_pr(make_layer, app_repo, monkeypatch):
    layer = _layer(make_layer, app_repo, review={"kind": "human", "reviewers": ["alice"], "draft": True}, adw_id="1234abd4")
    ir_path = layer / "agentic-layer.json"
    ir_data = json.loads(ir_path.read_text())
    ir_data["forge"] = {"id": "github", "kind": "github", "status": "stable", "remote": ""}
    ir_path.write_text(json.dumps(ir_data), encoding="utf-8")
    _script(layer, "build", {"files": {"feature.py": "FEATURE = True\n"}})
    bare = app_repo.parent / "origin.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True, text=True, timeout=30)
    _git(app_repo, "remote", "add", "origin", str(bare))
    calls = []
    class FakeForge:
        kind = "github"
        def create_pr(self, branch, base, title, body, draft=False, reviewers=()):
            calls.append((branch, base, title, body, draft, tuple(reviewers)))
            return {"number": 17, "url": "https://forge.invalid/pr/17"}
    fake_module = SimpleNamespace(get_client=lambda _layer: FakeForge())
    monkeypatch.setitem(sys.modules, "adw_modules.forge", fake_module)
    import adw_modules
    monkeypatch.setattr(adw_modules, "forge", fake_module, raising=False)
    assert main("demo", layer, ["Open PR", "--adw-id", "1234abd4"]) == 0
    saved = json.loads((layer / "agents/1234abd4/state.json").read_text())
    assert saved["review"]["verdict"]["mode"] == "human"
    assert saved["ship"]["action"] == "pr" and saved["ship"]["shipped"] is False
    assert calls and calls[0][0] == saved["branch"] and calls[0][1] == "main"
    assert calls[0][4:] == (True, ("alice",))
    assert "## Review verdict" in calls[0][3]
    assert _git(bare, "rev-parse", f"refs/heads/{saved['branch']}") == saved["ship"]["commit"]
    assert _git(app_repo, "status", "--porcelain") == ""
