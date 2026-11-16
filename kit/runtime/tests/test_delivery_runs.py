"""Full runs (runner.main, mock agent, real git, no podman unless marked) of a layer that delivers
into stages: the run's worktree starts from its stage branch and its ship step delivers into it.
The stages are the compiler's real output (data/delivery-sprint.json)."""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from adw_modules import delivery, fanout, ir
from adw_modules.runner import main

RUNTIME = Path(__file__).resolve().parents[1]
DATA = Path(__file__).resolve().parent / "data" / "delivery-sprint.json"
NOW = datetime(2026, 11, 16, 9, 30, tzinfo=timezone.utc)
SPRINT = "sprint/2026-s47"
RUN_CODE = ("import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; "
            "from adw_modules.runner import main; sys.exit(main(sys.argv[2], Path(sys.argv[3]), sys.argv[4:]))")


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
                            capture_output=True, text=True, timeout=60)
    if check:
        assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _commit_on(app: Path, branch: str, path: str, content: str) -> str:
    """A commit on `branch` of the app made in a throwaway worktree (the app checkout is not touched)."""
    tree = app.parent / f"helper-{abs(hash((branch, path, content)))}"
    _git(app, "worktree", "add", str(tree), branch)
    (tree / path).write_text(content, encoding="utf-8")
    _git(tree, "add", path)
    _git(tree, "commit", "-m", f"{branch}: {path}")
    commit = _git(tree, "rev-parse", "HEAD")
    _git(app, "worktree", "remove", "--force", str(tree))
    return commit


def _adw(name: str = "demo", review_step: dict | None = None, **extra) -> dict:
    return {"name": name, "agent": "mock", "review": "policy", "environment": ["worktree"],
            "classes": ["chore", "bug", "feature"], "steps": [
                {"name": "plan", "kind": "run", "command": "plan"},
                {"name": "build", "kind": "run", "command": "build"},
                {"name": "test", "kind": "check", "cmd": "test", "resolve": "resolve-test", "retries": 1},
                {"name": "review", "kind": "review", **(review_step or {})}, {"name": "ship", "kind": "ship"}],
            **extra}


def _sprint_layer(make_layer, app_repo, *, stages=None, adws=None, review=None, extra=None) -> Path:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    overrides = {
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "commands": [{"name": n, "template": t, "modelSet": "base", "output": o} for n, t, o in (
            ("plan", "plan", "path"), ("build", "build", "text"), ("resolve-test", "resolve-test", "text"),
            ("review", "review", "json"), ("patch", "plan-patch", "text"), ("implement", "implement", "text"),
            ("commit", "commit", "text"))],
        "codebase": {"commands": [{"name": "test", "run": "./test.sh", "timeout": 5},
                                  {"name": "has-sprint", "run": "test -f sprint.txt", "timeout": 5}]},
        "reviews": [{"id": "policy", "kind": "auto", "status": "stable", "command": "review", "reviewers": 1,
                     "maxRetries": 0, "artefacts": [], "allowedClasses": ["chore", "bug", "feature"],
                     "blockProtectedPaths": True, "maxDiffLines": 100, "requireChecks": ["test"], "ship": "merge",
                     "deployCmd": "", **(review or {})}],
        "environments": [{"id": "worktree", "kind": "worktree", "dir": "trees", "copyFiles": [],
                          "portBase": 27000, "portsPerRun": 0, "keep": "always", "status": "stable"}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "concurrency": {"maxRuns": 8, "maxAgents": 4, "maxChecks": 4, "waitSeconds": 60},
        "stages": stages if stages is not None else data["stages"], "cycle": data["cycle"],
        "delivery": data["delivery"], "adws": adws or [_adw()],
        **(extra or {}),
    }
    return make_layer(overrides)


def _script(layer: Path, adw: str, step: str, data: dict) -> None:
    path = layer / "mock" / adw / f"{step}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _open_cycle(layer: Path, app_repo: Path, name: str = "2026-s47") -> dict:
    del app_repo
    return delivery.cycle_start(ir.load(layer), name=name, now=NOW)


def _state(layer: Path, adw_id: str) -> dict:
    return json.loads((layer / "agents" / adw_id / "state.json").read_text())


def _events(layer: Path, adw_id: str) -> list[dict]:
    path = layer / "agents" / adw_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _branches(app: Path) -> set[str]:
    return set(_git(app, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())


def test_feature_run_branches_from_the_sprint_and_delivers_into_it(make_layer, app_repo):
    layer = _sprint_layer(make_layer, app_repo)
    _open_cycle(layer, app_repo)
    _commit_on(app_repo, SPRINT, "sprint.txt", "already in the sprint\n")
    _script(layer, "demo", "build", {"files": {"feature.py": "FEATURE = True\n"}})
    main_head, symbolic = _git(app_repo, "rev-parse", "refs/heads/main"), _git(app_repo, "symbolic-ref", "HEAD")
    assert main("demo", layer, ["Add a feature", "--class", "feature", "--adw-id", "1234abcd"]) == 0
    state = _state(layer, "1234abcd")
    assert state["status"] == "passed"
    assert (Path(state["worktree"]) / "sprint.txt").is_file()            # started from the sprint's head
    assert _git(app_repo, "show", f"{SPRINT}:feature.py") == "FEATURE = True"
    assert _git(app_repo, "show", f"{SPRINT}:sprint.txt") == "already in the sprint"
    assert _git(app_repo, "rev-parse", "refs/heads/main") == main_head
    assert _git(app_repo, "symbolic-ref", "HEAD") == symbolic and _git(app_repo, "status", "--porcelain") == ""
    assert state["delivery"]["stage"] == "sprint" and state["delivery"]["branch"] == SPRINT
    assert state["delivery"]["delivered"] is True and state["ship"]["target"] == SPRINT
    assert "delivery:  sprint -> sprint/2026-s47 (merge; delivered=True)" in (layer / "agents/1234abcd/run.log").read_text()
    types = [event["type"] for event in _events(layer, "1234abcd")]
    assert "delivery.target" in types and "delivery.result" in types


def test_two_runs_into_the_same_stage(make_layer, app_repo, monkeypatch):
    layer = _sprint_layer(make_layer, app_repo, adws=[_adw("demo"), _adw("other")])
    _open_cycle(layer, app_repo)
    _script(layer, "demo", "build", {"files": {"first.py": "FIRST = 1\n"}})
    _script(layer, "other", "build", {"files": {"second.py": "SECOND = 2\n"}})
    original = delivery.before_ship

    def second_run_ships_first(*args, **kwargs):
        env = {k: v for k, v in os.environ.items() if k != "AL_APP_ROOT"}
        done = subprocess.run([sys.executable, "-c", RUN_CODE, str(RUNTIME), "other", str(layer),
                               "Second change", "--class", "feature", "--adw-id", "2234abcd"],
                              capture_output=True, text=True, timeout=300, env=env)
        assert done.returncode == 0, done.stdout + done.stderr
        return original(*args, **kwargs)

    monkeypatch.setattr(delivery, "before_ship", second_run_ships_first)
    assert main("demo", layer, ["First change", "--class", "feature", "--adw-id", "1234abcd"]) == 0
    second = _state(layer, "2234abcd")
    assert _git(app_repo, "show", f"{SPRINT}:first.py") == "FIRST = 1"
    assert _git(app_repo, "show", f"{SPRINT}:second.py") == "SECOND = 2"
    assert _git(app_repo, "merge-base", "--is-ancestor", second["ship"]["commit"], SPRINT, check=False) == ""
    assert _state(layer, "1234abcd")["delivery"]["behind"] >= 1


def test_stage_moved_meanwhile_is_merged_and_reported_stale(make_layer, app_repo, monkeypatch):
    layer = _sprint_layer(make_layer, app_repo)
    _open_cycle(layer, app_repo)
    _script(layer, "demo", "build", {"files": {"feature.py": "FEATURE = True\n"}})
    original = delivery.before_ship

    def someone_else_delivers(*args, **kwargs):
        _commit_on(app_repo, SPRINT, "meanwhile.txt", "landed while the run worked\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(delivery, "before_ship", someone_else_delivers)
    assert main("demo", layer, ["Add a feature", "--class", "feature", "--adw-id", "1234abcd"]) == 0
    stale = [e for e in _events(layer, "1234abcd") if e["type"] == "delivery.stale"]
    assert stale and stale[0]["behind"] == 1 and stale[0]["branch"] == SPRINT
    assert _state(layer, "1234abcd")["delivery"]["delivered"] is True
    assert _git(app_repo, "show", f"{SPRINT}:meanwhile.txt") == "landed while the run worked"
    assert _git(app_repo, "show", f"{SPRINT}:feature.py") == "FEATURE = True"


def test_conflict_with_the_stage_head_runs_a_patch_round(make_layer, app_repo, monkeypatch):
    layer = _sprint_layer(make_layer, app_repo,
                          adws=[_adw(review_step={"patch": "patch", "implement": "implement"})])
    _open_cycle(layer, app_repo)
    _script(layer, "demo", "build", {"files": {"app.py": "print('run')\n"}})
    _script(layer, "demo", "deliver-patch-1", {"output": "keep both prints"})
    _script(layer, "demo", "deliver-implement-1", {"files": {"app.py": "print('sprint')\nprint('run')\n"}})
    original = delivery.before_ship
    moved = {}

    def same_line_changes(*args, **kwargs):
        moved["commit"] = _commit_on(app_repo, SPRINT, "app.py", "print('sprint')\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(delivery, "before_ship", same_line_changes)
    assert main("demo", layer, ["Change the greeting", "--class", "feature", "--adw-id", "1234abcd"]) == 0
    state = _state(layer, "1234abcd")
    assert state["delivery"]["delivered"] is True
    assert state["delivery"]["conflict"]["files"] == ["app.py"]
    assert len(state["review"]["rounds"]) == 2
    assert _git(app_repo, "show", f"{SPRINT}:app.py") == "print('sprint')\nprint('run')"
    merges = _git(app_repo, "log", "--merges", "--format=%P %s", SPRINT).splitlines()
    assert any(moved["commit"] in line.split()[:2] and "merge sprint/2026-s47 into" in line for line in merges)
    assert [e["resolved"] for e in _events(layer, "1234abcd") if e["type"] == "delivery.conflict"] == [True]


def test_conflict_without_patch_commands_is_handed_over_with_its_reason(make_layer, app_repo, monkeypatch):
    layer = _sprint_layer(make_layer, app_repo)
    _open_cycle(layer, app_repo)
    _script(layer, "demo", "build", {"files": {"app.py": "print('run')\n"}})
    original = delivery.before_ship
    moved = {}

    def same_line_changes(*args, **kwargs):
        moved["head"] = _commit_on(app_repo, SPRINT, "app.py", "print('sprint')\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(delivery, "before_ship", same_line_changes)
    assert main("demo", layer, ["Change the greeting", "--class", "feature", "--adw-id", "1234abcd"]) == 1
    state = _state(layer, "1234abcd")
    assert state["status"] == "failed"
    assert state["ship"]["action"] == "handoff"
    assert any("delivery:conflict" in reason for reason in state["ship"]["reasons"])
    assert _git(app_repo, "rev-parse", f"refs/heads/{SPRINT}") == moved["head"]


def test_class_routed_to_another_stage(make_layer, app_repo):
    data = json.loads(DATA.read_text(encoding="utf-8"))
    hotfix = {**copy.deepcopy(data["stages"][0]), "name": "hotfix", "branch": "hotfix/{date}", "from": "main",
              "into": "main", "accepts": ["bug"], "agentsMayMerge": True, "origin": "declared"}
    layer = _sprint_layer(make_layer, app_repo, stages=[*data["stages"], hotfix],
                          adws=[_adw(deliverTo="hotfix", classes=["bug"])])
    _open_cycle(layer, app_repo)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _git(app_repo, "branch", f"hotfix/{today}")
    _script(layer, "demo", "build", {"files": {"fix.py": "FIXED = True\n"}})
    assert main("demo", layer, ["Fix it now", "--class", "bug", "--adw-id", "1234abcd"]) == 0
    assert _git(app_repo, "show", f"hotfix/{today}:fix.py") == "FIXED = True"
    assert _git(app_repo, "show", f"{SPRINT}:fix.py", check=False) == ""

    develop = {**hotfix, "name": "develop", "branch": "develop", "into": "", "accepts": ["bug", "feature"]}
    main_stage = data["stages"][1]
    other = _sprint_layer(make_layer, app_repo, stages=[develop, main_stage], adws=[_adw()],
                          extra={"cycle": None, "delivery": None})
    _git(app_repo, "branch", "develop")
    _script(other, "demo", "build", {"files": {"bug.py": "BUG = 0\n"}})
    assert main("demo", other, ["Fix a bug", "--class", "bug", "--adw-id", "2234abcd"]) == 0
    assert _git(app_repo, "show", "develop:bug.py") == "BUG = 0"
    assert _state(other, "2234abcd")["delivery"]["stage"] == "develop"


def test_forbidden_target_is_refused_before_any_work(make_layer, app_repo):
    layer = _sprint_layer(make_layer, app_repo, adws=[_adw(deliverTo="main")])
    before = _branches(app_repo)
    assert main("demo", layer, ["Ship to main", "--class", "chore", "--adw-id", "1234abcd"]) == 1
    state = _state(layer, "1234abcd")
    assert state["status"] == "failed" and state["delivery"]["refused"] == "forbidden-target"
    assert not (layer / "trees").exists() or not any((layer / "trees").iterdir())
    assert _branches(app_repo) == before


def test_unsupported_entry_is_handed_over(make_layer, app_repo):
    data = json.loads(DATA.read_text(encoding="utf-8"))
    stages = copy.deepcopy(data["stages"])
    stages[0]["entry"] = "squash"
    layer = _sprint_layer(make_layer, app_repo, stages=stages)
    _open_cycle(layer, app_repo)
    _script(layer, "demo", "build", {"files": {"feature.py": "FEATURE = True\n"}})
    head = _git(app_repo, "rev-parse", f"refs/heads/{SPRINT}")
    assert main("demo", layer, ["Add a feature", "--class", "feature", "--adw-id", "1234abcd"]) == 1
    state = _state(layer, "1234abcd")
    assert state["ship"]["action"] == "handoff"
    assert any("delivery:entry-unsupported" in reason for reason in state["ship"]["reasons"])
    assert _git(app_repo, "rev-parse", f"refs/heads/{SPRINT}") == head


def test_fan_out_candidates_start_from_the_parent_stage(make_layer, app_repo, monkeypatch):
    child = {"name": "child", "agent": "mock", "review": "policy", "environment": ["worktree"], "classes": [],
             "steps": [{"name": "build", "kind": "run", "command": "build"},
                       {"name": "sprint-file", "kind": "check", "cmd": "has-sprint", "resolve": "", "retries": 0},
                       {"name": "commit", "kind": "run", "command": "commit"}]}
    parent = {"name": "parent", "agent": "mock", "review": "policy", "environment": ["worktree"], "classes": [],
              "steps": [{"name": "race", "kind": "fan-out", "adw": "child", "count": 2, "pick": "first",
                         "maxParallel": 0}]}
    layer = _sprint_layer(make_layer, app_repo, adws=[parent, child])
    _open_cycle(layer, app_repo)
    _commit_on(app_repo, SPRINT, "sprint.txt", "already in the sprint\n")
    _script(layer, "child", "build", {"files": {"candidate.txt": "${adw_id}\n"}})
    _script(layer, "child", "commit", {"commit": True})

    def launch(lay, name, args, env, log):
        env = {k: v for k, v in env.items() if k != "AL_APP_ROOT"}
        return subprocess.Popen([sys.executable, "-c", RUN_CODE, str(RUNTIME), name, str(lay.layer_dir), *args],
                                cwd=lay.layer_dir, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)

    monkeypatch.setattr(fanout, "launcher", launch)
    assert main("parent", layer, ["Race it", "--class", "feature", "--adw-id", "fa000001"]) == 0
    candidates = _state(layer, "fa000001")["fanout"]["race"]["candidates"]
    passed = [c for c in candidates if c.get("exit") == 0]
    assert passed   # each candidate's check proves sprint.txt was in its worktree
    for item in candidates:
        assert _state(layer, item["id"])["delivery"]["branch"] == SPRINT


def test_layer_without_stages_runs_exactly_as_before(make_layer, app_repo):
    from test_runner_e2e import _layer, _script as e2e_script

    layer = _layer(make_layer, app_repo)
    e2e_script(layer, "build", {"files": {"feature.py": "FEATURE = True\n"}})
    original_head = _git(app_repo, "rev-parse", "HEAD")
    assert main("demo", layer, ["Add a feature", "--adw-id", "1234abcd"]) == 0
    state = _state(layer, "1234abcd")
    assert state["status"] == "passed" and "delivery" not in state
    assert _git(app_repo, "show", "al/ship/main:feature.py") == "FEATURE = True"
    assert _git(app_repo, "rev-parse", "refs/heads/main") == original_head
    assert [step["name"] for step in state["steps"]] == ["plan", "build", "test", "review", "ship"]
    assert not any(e["type"].startswith("delivery.") for e in _events(layer, "1234abcd"))
    assert "delivery:" not in (layer / "agents/1234abcd/run.log").read_text()


@pytest.mark.podman
def test_sandboxed_delivery_into_the_sprint(make_layer, app_repo, monkeypatch):
    if shutil.which("podman") is None or subprocess.run(["podman", "info"], capture_output=True).returncode != 0:
        pytest.skip("podman is not installed or not usable here")
    from test_sandbox_podman import BASES, TEST_IMAGE, _podman
    from adw_modules import envs

    image = TEST_IMAGE if _podman("image", "exists", TEST_IMAGE).returncode == 0 else None
    if image is None:
        pytest.skip(f"the sandbox test image {TEST_IMAGE} is not built (see test_sandbox_podman, bases {BASES})")
    monkeypatch.setattr(envs.EnvStack, "image", property(lambda self: image))
    sandbox = {"id": "box", "kind": "podman", "agentInside": False, "baseImage": "debian", "cpus": "1", "memory": "1g",
               "network": "none", "packages": [], "setup": [], "status": "stable", "scope": "run", "caches": [],
               "credentials": []}
    layer = _sprint_layer(make_layer, app_repo, adws=[_adw(environment=["worktree", "box"])])
    data = json.loads((layer / "agentic-layer.json").read_text())
    data["environments"].append(sandbox)
    (layer / "agentic-layer.json").write_text(json.dumps(data))
    _open_cycle(layer, app_repo)
    _script(layer, "demo", "build", {"files": {"feature.py": "FEATURE = True\n"}})
    assert main("demo", layer, ["Add a feature", "--class", "feature", "--adw-id", "1234abcd"]) == 0
    assert _git(app_repo, "show", f"{SPRINT}:feature.py") == "FEATURE = True"
