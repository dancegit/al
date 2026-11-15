"""(script NAME #:cmd C #:args (…)): a declared codebase cmd as a workflow step. It runs in the
run's worktree with the run context in ADW_* variables, its output is the step's output, a
non-zero exit fails the run (and a shipping workflow hands its work to a person), and the gate
never counts it as a check. Git is real; the agent is the mock."""
from __future__ import annotations

import json
from pathlib import Path

from adw_modules import events, review
from adw_modules.runner import main

ADW_ID = "5c0e1a00"
LEGACY = """#!/bin/sh
# the team's own workflow script: writes what it was given and the run context
echo "args: $*"
echo "id=$ADW_ID class=$ADW_CLASS prompt=$ADW_PROMPT step=$ADW_STEP"
printf '%s\\n' "$1" > legacy-output.txt
test "$1" != "fail"
"""


def _layer(make_layer, app_repo: Path) -> Path:
    script = app_repo / "adws" / "legacy.sh"
    script.parent.mkdir()
    script.write_text(LEGACY, encoding="utf-8")
    script.chmod(0o755)
    import subprocess
    subprocess.run(["git", "-C", str(app_repo), "add", "adws"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(app_repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "legacy"],
                   check=True, capture_output=True)
    return make_layer({
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "codebase": {"commands": [{"name": "legacy-run", "run": "./adws/legacy.sh", "timeout": 30, "description": ""}]},
        "environments": [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 27950,
                          "portsPerRun": 0, "keep": "never", "status": "stable"}],
        "reviews": [{"id": "human", "kind": "human", "status": "stable", "reviewers": [], "draft": False, "bodyCmd": ""}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "promptInputs": [{"id": "cli", "kind": "cli", "status": "stable", "allowStdin": False}],
        "adws": [{"name": "legacy", "agent": "mock", "review": "human", "environment": ["wt"], "input": ["cli"],
                  "trigger": ["manual"], "classes": [],
                  "steps": [{"name": "run", "kind": "script", "cmd": "legacy-run", "args": ["$prompt", "two words"],
                             "timeout": 0},
                            {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}]}],
    })


def _state(layer: Path) -> dict:
    return json.loads((layer / "agents" / ADW_ID / "state.json").read_text())


def test_a_script_step_runs_the_cmd_with_arguments_and_the_run_context(make_layer, app_repo):
    layer = _layer(make_layer, app_repo)
    assert main("legacy", layer, ["tidy up", "--adw-id", ADW_ID]) == 0
    data = _state(layer)
    record = data["scripts"]["run"]
    assert record["exit"] == 0 and record["args"] == ["tidy up", "two words"]
    assert data["outputs"]["run"].splitlines() == ["args: tidy up two words",
                                                   f"id={ADW_ID} class=chore prompt=tidy up step=run"]
    assert (layer / record["log"]).read_text() == data["outputs"]["run"]
    assert "run" not in data["checks"]                         # not a check: the gate never counts it
    assert data["ship"]["action"] == "pr"                      # human review: PR.md for a person
    kinds = [e["type"] for e in events.read(layer / "agents" / ADW_ID / "events.jsonl")]
    assert "script.start" in kinds and "script.end" in kinds
    assert not data["agent_calls"]


def test_a_failing_script_fails_the_run_and_hands_off(make_layer, app_repo):
    layer = _layer(make_layer, app_repo)
    assert main("legacy", layer, ["fail", "--adw-id", ADW_ID]) == 1
    data = _state(layer)
    assert data["status"] == "failed" and data["scripts"]["run"]["exit"] == 1
    step = next(r for r in data["steps"] if r["name"] == "run")
    assert step["status"] == "failed" and "script legacy-run exited 1" in step["detail"]
    assert data["ship"]["action"] == "handoff" and data["ship"]["stopped_at"] == "run"
    pr = (layer / data["ship"]["target"]).read_text()
    assert "script legacy-run exited 1" in pr


def test_the_gate_does_not_count_a_script_as_verification():
    option = {"kind": "auto", "allowedClasses": ["chore"], "maxDiffLines": 100, "blockProtectedPaths": True, "ship": "merge"}
    facts = {"check_exits": {}, "require_exits": {}, "verdict": {"success": True, "issues": []}, "klass": "chore",
             "diff_lines": 1, "changed_files": [], "protected": [], "changes": "codebase"}
    # a workflow whose only proof is a script step has no check result at all: gather_facts never
    # reads state.scripts, so the decision rests on the checks and require-checks alone
    assert review.decide(option, facts)["ship"] is True
    assert "scripts" not in review.gather_facts.__code__.co_names
