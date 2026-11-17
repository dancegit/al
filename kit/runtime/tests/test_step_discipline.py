"""Step discipline: the "Your step" text of a prompt (${workflow_steps}), and the runtime's check
that a step did only its own job. A build step that commits, creates a ref or leaves a file at the
top of the layer directory fails as a step-discipline violation; what can be undone is undone. A
commit step may commit, and what it leaves uncommitted under the layer or a protected path is
restored so the worktree equals the commit."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from adw_modules import agents, discipline, ir as ir_module, launch
from adw_modules.runner import main
from adw_modules.state import ADWState

ADW_ID = "5d150001"
RUNTIME_TESTS = Path(__file__).resolve().parent
GITIGNORE = "/trees/\n/agents/\n/artefacts/\n/specs/\n/.al-improve/\n__pycache__/\n"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _commands(*extra: dict) -> list[dict]:
    return [{"name": "plan", "template": "plan", "modelSet": "base", "output": "path"},
            {"name": "build", "template": "build", "modelSet": "base", "output": "text"},
            {"name": "commit", "template": "commit", "modelSet": "base", "output": "text"},
            {"name": "review", "template": "review", "modelSet": "base", "output": "json"}, *extra]


def _review() -> dict:
    return {"id": "policy", "kind": "auto", "status": "stable", "command": "review", "reviewers": 1, "maxRetries": 0,
            "artefacts": [], "allowedClasses": ["chore"], "blockProtectedPaths": True, "maxDiffLines": 100,
            "requireChecks": ["test"], "ship": "merge", "bodyCmd": ""}


def _ir(steps: list[dict], changes: str = "codebase") -> dict:
    return {"agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
            "commands": _commands(),
            "codebase": {"commands": [{"name": "test", "run": "./test.sh", "timeout": 10}]},
            "reviews": [_review()],
            "environments": [{"id": "worktree", "kind": "worktree", "dir": "trees", "copyFiles": [],
                              "portBase": 27100, "portsPerRun": 0, "keep": "never", "status": "stable"}],
            "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
            "adws": [{"name": "demo", "agent": "mock", "review": "policy", "environment": ["worktree"],
                      "classes": ["chore"], "changes": changes, "steps": steps}]}


def _script(layer: Path, step: str, data: dict) -> None:
    path = layer / "mock" / "demo" / f"{step}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    app = layer.parent
    if (app / ".git").is_dir() and layer.name == "agentic":
        _git(app, "add", "agentic/mock")
        _git(app, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", f"mock {step}")


def _state(layer: Path) -> dict:
    return json.loads((layer / "agents" / ADW_ID / "state.json").read_text())


def _record(layer: Path, name: str) -> dict:
    return next(r for r in _state(layer)["steps"] if r["name"] == name)


def _events(layer: Path) -> list[dict]:
    path = layer / "agents" / ADW_ID / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


BUILD_ONLY = [{"name": "build", "kind": "run", "command": "build"}]
FULL = [{"name": "plan", "kind": "run", "command": "plan"}, {"name": "build", "kind": "run", "command": "build"},
        {"name": "test", "kind": "check", "cmd": "test"}, {"name": "commit", "kind": "run", "command": "commit"},
        {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}]


# ---------------------------------------------------------------- the ${workflow_steps} text

def test_workflow_steps_marks_this_step_and_says_what_follows(make_layer) -> None:
    layer = ir_module.load(make_layer(_ir(FULL)))
    state = ADWState(adw_id=ADW_ID, adw="demo", steps=[{"name": "plan", "status": "passed"},
                                                       {"name": "build", "status": "running"}])
    text = discipline.workflow_steps(layer, state)
    assert text.startswith("Workflow `demo` (class chore): plan → **build (this step)** → test → commit")
    assert "the checks run the suites (test)" in text
    assert "`commit` commits" in text and "`review` judges" in text and "`ship` delivers" in text
    assert text.index("the checks run") < text.index("`commit` commits") < text.index("`ship` delivers")


def test_workflow_steps_of_a_composed_workflow() -> None:
    layer = ir_module.Layer(json.loads((RUNTIME_TESTS / "full.layer.json").read_text()), RUNTIME_TESTS, RUNTIME_TESTS)
    sdlc = layer.adw("sdlc")
    running = next(s["name"] for s in sdlc["steps"] if s.get("name") == "build")
    state = ADWState(adw_id=ADW_ID, adw="sdlc", klass="feature", steps=[{"name": running, "status": "running"}])
    text = discipline.workflow_steps(layer, state)
    first = text.splitlines()[0]
    listed = first.split(": ", 1)[1].split(" → ")
    assert listed[:3] == ["classify", "plan-feature", f"**{running} (this step)**"]
    assert "plan-chore" not in listed and "plan-bug" not in listed
    assert listed[-1] == "ship"


def test_workflow_steps_of_a_single_command_run(make_layer) -> None:
    layer = ir_module.load(make_layer(_ir(FULL)))
    synthetic = launch.single_command_adw(layer, "build")
    layer.ir["adws"].append(synthetic)
    state = ADWState(adw_id=ADW_ID, adw=synthetic["name"], steps=[{"name": "build", "status": "running"}])
    text = discipline.workflow_steps(layer, state)
    assert "single-command run" in text and "`build` is its only step" in text
    assert "Nothing after it checks, commits or ships the work" in text


def test_may_commit_follows_the_template_and_the_field(make_layer) -> None:
    ir = _ir(BUILD_ONLY)
    ir["commands"] = _commands({"name": "land", "template": "custom", "file": "x.md", "mayCommit": True,
                                "modelSet": "base", "output": "text"})
    ir["hops"] = [{"name": "commit-hop", "template": "run-plan", "modelSet": "heavy"}]
    layer = ir_module.load(make_layer(ir))
    assert discipline.may_commit(layer, "commit")          # template commit, without mayCommit (older IR)
    assert discipline.may_commit(layer, "land")
    assert not discipline.may_commit(layer, "build")
    assert not discipline.may_commit(layer, "commit-hop")


def test_the_scratch_dir_is_inside_the_run_directory(make_layer) -> None:
    layer = ir_module.load(make_layer(_ir(BUILD_ONLY)))
    path = discipline.scratch_dir(layer, ADWState(adw_id=ADW_ID, adw="demo"))
    assert path == Path(layer.layer_dir) / "agents" / ADW_ID / "scratch" and path.is_dir()


# ---------------------------------------------------------------- a build step that leaves its step

def test_a_build_step_that_commits_is_failed_and_the_commit_undone(make_layer, app_repo) -> None:
    layer = make_layer(_ir(BUILD_ONLY))
    _script(layer, "build", {"files": {"feature.py": "FEATURE = True\n"}, "commit": True})
    assert main("demo", layer, ["Add a feature", "--adw-id", ADW_ID, "--keep-worktree"]) == 1
    record = _record(layer, "build")
    assert record["status"] == "failed"
    assert "the build step committed" in record["detail"] and "not a test failure" in record["detail"]
    assert record["discipline"]["violations"] == ["committed"]
    assert any(line.startswith("HEAD reset to") for line in record["discipline"]["restored"])
    worktree = Path(_state(layer)["worktree"])
    assert _git(worktree, "rev-parse", "HEAD") == _git(app_repo, "rev-parse", "main")
    assert _git(worktree, "status", "--short") == "?? feature.py"
    assert any(e["type"] == "step.discipline" and e.get("ok") is False for e in _events(layer))


def test_a_failed_build_that_also_committed_keeps_its_failure_and_the_commit_is_undone(make_layer, app_repo) -> None:
    layer = make_layer(_ir(BUILD_ONLY))
    _script(layer, "build", {"files": {"feature.py": "FEATURE = True\n"}, "commit": True, "fail": "x"})
    assert main("demo", layer, ["Add a feature", "--adw-id", ADW_ID, "--keep-worktree"]) == 1
    record = _record(layer, "build")
    assert "step-discipline" not in record["detail"]
    assert record["discipline"]["violations"] == ["committed"]
    worktree = Path(_state(layer)["worktree"])
    assert _git(worktree, "rev-parse", "HEAD") == _git(app_repo, "rev-parse", "main")


def _after_mock(monkeypatch, action) -> None:
    original = agents._run_mock

    def wrapper(layer, state, agent, step, command, args, raw_path, env_stack=None):
        output = original(layer, state, agent, step, command, args, raw_path, env_stack)
        if step == "build":
            action(Path(agents.work_dir(layer, state)))
        return output

    monkeypatch.setattr(agents, "_run_mock", wrapper)


def test_a_build_step_that_creates_a_branch_is_failed_and_the_ref_left_for_a_person(make_layer, app_repo,
                                                                                    monkeypatch) -> None:
    layer = make_layer(_ir(BUILD_ONLY))
    _script(layer, "build", {"files": {"feature.py": "FEATURE = True\n"}})

    def branch_with_new_work(work: Path) -> None:
        made = _git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "x")
        _git(work, "branch", "extra", made.strip())

    _after_mock(monkeypatch, branch_with_new_work)
    assert main("demo", layer, ["Add a feature", "--adw-id", ADW_ID]) == 1
    discipline_record = _record(layer, "build")["discipline"]
    assert discipline_record["violations"] == ["ref-created"]
    assert "refs/heads/extra" in discipline_record["refs"]["created"]
    assert any("refs/heads/extra" in line for line in discipline_record["not_restored"])
    assert _git(app_repo, "rev-parse", "--verify", "refs/heads/extra")


def test_another_runs_branch_is_not_this_steps_violation(make_layer, app_repo, monkeypatch) -> None:
    layer = make_layer(_ir(BUILD_ONLY))
    _script(layer, "build", {"files": {"feature.py": "FEATURE = True\n"}})
    _after_mock(monkeypatch, lambda work: _git(work, "branch", "chore/0000beef-x"))
    assert main("demo", layer, ["Add a feature", "--adw-id", ADW_ID]) == 0
    assert _record(layer, "build")["discipline"]["ok"] is True


def test_a_build_step_that_writes_at_the_top_of_the_layer_is_failed_and_the_file_moved(make_layer, app_repo) -> None:
    layer = make_layer(_ir(BUILD_ONLY))
    _script(layer, "build", {"files": {"feature.py": "FEATURE = True\n"}, "layer_files": {"validate-x.txt": "notes\n"}})
    assert main("demo", layer, ["Add a feature", "--adw-id", ADW_ID]) == 1
    record = _record(layer, "build")
    assert record["discipline"]["violations"] == ["layer-entry"]
    assert "left validate-x.txt at the top of the layer directory" in record["detail"]
    assert not (layer / "validate-x.txt").exists()
    assert (layer / "agents" / ADW_ID / "scratch" / "stray" / "validate-x.txt").read_text() == "notes\n"


def test_a_normal_build_stays_in_its_step(make_layer, app_repo, monkeypatch) -> None:
    steps = [{"name": "build", "kind": "run", "command": "build", "retries": 1}]
    layer = make_layer(_ir(steps))
    _script(layer, "build", {"files": {"feature.py": "FEATURE = True\n"},
                             "layer_files": {"specs/notes.md": "a spec\n",
                                             f"agents/{ADW_ID}/scratch/helper.sh": "echo ${scratch_dir}\n"}})
    attempts = {"n": 0}
    original = agents._run_mock

    def flaky(layer_, state, agent, step, command, args, raw_path, env_stack=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            from adw_modules.errors import StepFailed
            raise StepFailed(step, "first attempt fails")
        return original(layer_, state, agent, step, command, args, raw_path, env_stack)

    monkeypatch.setattr(agents, "_run_mock", flaky)
    assert main("demo", layer, ["Add a feature", "--adw-id", ADW_ID]) == 0
    assert attempts["n"] == 2
    record = _record(layer, "build")["discipline"]
    assert record["ok"] is True and record["violations"] == []
    scratch = layer / "agents" / ADW_ID / "scratch" / "helper.sh"
    assert scratch.read_text().strip() == f"echo {layer / 'agents' / ADW_ID / 'scratch'}"


# ---------------------------------------------------------------- the commit step

def test_the_commit_step_may_commit(make_layer, app_repo) -> None:
    steps = [{"name": "build", "kind": "run", "command": "build"}, {"name": "commit", "kind": "run", "command": "commit"}]
    layer = make_layer(_ir(steps))
    _script(layer, "build", {"files": {"feature.py": "FEATURE = True\n"}})
    _script(layer, "commit", {"commit": True})
    assert main("demo", layer, ["Add a feature", "--adw-id", ADW_ID, "--keep-worktree"]) == 0
    worktree = Path(_state(layer)["worktree"])
    assert _git(worktree, "rev-parse", "HEAD") != _git(app_repo, "rev-parse", "main")
    assert _git(worktree, "status", "--short") == ""
    assert "discipline" not in _record(layer, "commit")


@pytest.fixture
def in_repo(make_layer, app_repo):
    def factory(steps: list[dict], changes: str = "codebase") -> Path:
        ir = _ir(steps, changes)
        ir["codebase"]["root"] = ".."
        built = make_layer(ir)
        layer = app_repo / "agentic"
        shutil.copytree(built, layer)
        shutil.rmtree(built)
        data = json.loads((layer / "agentic-layer.json").read_text())
        data["codebase"]["root"] = ".."
        data["layer"] = {"id": "in-repo", "kind": "in-repo", "dir": "agentic", "status": "stable"}
        (layer / "agentic-layer.json").write_text(json.dumps(data, indent=2))
        (layer / ".gitignore").write_text(GITIGNORE)
        _git(app_repo, "add", "agentic")
        _git(app_repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "the layer, checked in")
        return layer
    return factory


def _commit_only_app_py(monkeypatch) -> None:
    original = agents._run_mock

    def wrapper(layer, state, agent, step, command, args, raw_path, env_stack=None):
        output = original(layer, state, agent, step, command, args, raw_path, env_stack)
        if step == "commit":
            work = Path(agents.work_dir(layer, state))
            _git(work, "add", "app.py")
            _git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "only app.py")
        return output

    monkeypatch.setattr(agents, "_run_mock", wrapper)


def test_the_commit_step_restores_what_it_left_under_the_layer(in_repo, app_repo, monkeypatch) -> None:
    layer = in_repo(FULL)
    _script(layer, "build", {"files": {"app.py": "print('changed')\n", "agentic/notes.md": "the agent wrote this\n"}})
    _commit_only_app_py(monkeypatch)
    assert main("demo", layer, ["Tidy up", "--adw-id", ADW_ID]) == 0
    state = _state(layer)
    left_out = _record(layer, "commit")["left_out"]
    assert left_out["restored"] == ["agentic/notes.md"]
    assert (layer / "agents" / ADW_ID / "scratch" / "left-out" / "agentic" / "notes.md").is_file()
    reasons = state["ship"].get("reasons") or []
    assert not any(r.startswith("protected-path:") for r in reasons) and "layer-changed" not in reasons
    assert state["ship"]["action"] == "merge"
    assert _git(app_repo, "show", "al/ship/main:app.py") == "print('changed')"


def test_a_layer_change_workflow_keeps_its_layer_files(in_repo, app_repo, monkeypatch) -> None:
    steps = [{"name": "build", "kind": "run", "command": "build"}, {"name": "commit", "kind": "run", "command": "commit"}]
    layer = in_repo(steps, changes="layer")
    _script(layer, "build", {"files": {"app.py": "print('changed')\n", "agentic/notes.md": "the agent wrote this\n"}})
    _commit_only_app_py(monkeypatch)
    assert main("demo", layer, ["Tidy up", "--adw-id", ADW_ID, "--keep-worktree"]) == 0
    left_out = _record(layer, "commit")["left_out"]
    assert left_out["restored"] == [] and left_out["still_uncommitted"] == ["agentic/notes.md"]
    worktree = Path(_state(layer)["worktree"])
    assert (worktree / "agentic" / "notes.md").is_file()


def test_branches_other_people_change_meanwhile_are_not_the_steps_doing(make_layer, app_repo, monkeypatch) -> None:
    """The repository is shared. While a build runs, someone deletes a branch of theirs and creates
    another at the commit the run started from: neither is the build step's work."""
    _git(app_repo, "branch", "someones-old-branch")
    layer = make_layer(_ir(BUILD_ONLY))
    _script(layer, "build", {"files": {"feature.py": "FEATURE = True\n"}})

    def meanwhile(_work: Path) -> None:
        _git(app_repo, "branch", "-D", "someones-old-branch")
        _git(app_repo, "branch", "someones-new-branch")

    _after_mock(monkeypatch, meanwhile)
    assert main("demo", layer, ["Add a feature", "--adw-id", ADW_ID]) == 0
    assert _record(layer, "build")["discipline"]["violations"] == []
