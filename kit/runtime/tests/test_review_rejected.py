"""A review whose reviewers refuse in every round ends `rejected`, and its run ends `handed-over`.

Regression for "a review that fails is reported as `step review passed: ok`": the review step returns
normally (a later ship step routes the work to a person), so the runner used to call the step and the
run `passed`. Runs go through `runner.main` with real git, the mock agent and no forge.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from adw_modules import events
from adw_modules.runner import main

ADW_ID = "aaaa0001"
MOCK = {"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}
TEXT = {"output": "text", "description": "", "file": "", "focus": [], "fabric": [], "relevant": [], "validation": []}
AGENT_REVIEW = {"id": "peer", "kind": "agent", "status": "stable", "artefacts": [], "command": "review",
                "reviewers": 2, "maxRetries": 2}
AUTO_REVIEW = {"id": "auto", "kind": "auto", "status": "stable", "artefacts": [], "command": "review", "reviewers": 2,
               "maxRetries": 2, "allowedClasses": ["chore"], "blockProtectedPaths": True, "maxDiffLines": 100,
               "requireChecks": [], "ship": "pr", "deployCmd": ""}
HUMAN_REVIEW = {"id": "human", "kind": "human", "status": "stable", "reviewers": [], "draft": False, "bodyCmd": ""}
REFUSED = {"success": False, "summary": "proof missing",
           "issues": [{"severity": "blocker", "description": "no proof", "resolution": "add proof"}]}
APPROVED = {"success": True, "summary": "looks right", "issues": []}
NOTIFIER = {"id": "hook", "kind": "command", "status": "stable", "events": ["needs-human"], "timeout": 5,
            "run": "cat > /dev/null"}


@pytest.fixture(autouse=True)
def no_settings_of_the_person(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for key in ("AL_DASHBOARD_URL", "ADW_FANOUT_PARENT", "AL_MODEL_SET", "AL_MODELS", "AL_MAX_COST"):
        monkeypatch.delenv(key, raising=False)


def _layer(make_layer, review: dict, *, steps: list[dict] | None = None, codebase: dict | None = None) -> Path:
    """build → review (with patch + implement) → ship, on the mock agent, without a forge (or the given steps)."""
    if steps is None:
        steps = [{"name": "build", "kind": "run", "command": "build", "modelSet": "inherit"},
                 {"name": "review", "kind": "review", "patch": "patch", "implement": "implement"},
                 {"name": "ship", "kind": "ship"}]
    overrides = {
        "agents": [MOCK],
        "commands": [{"name": "build", "template": "build", "modelSet": "base", **TEXT},
                     {"name": "patch", "template": "patch", "modelSet": "base", **{**TEXT, "output": "path"}},
                     {"name": "implement", "template": "implement", "modelSet": "base", **TEXT},
                     {"name": "review", "template": "review", "modelSet": "base", **{**TEXT, "output": "json"}}],
        "environments": [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 28300,
                          "portsPerRun": 0, "keep": "never", "status": "stable"}],
        "reviews": [review],
        "adws": [{"name": "demo", "agent": "mock", "review": review["id"], "environment": ["wt"], "input": ["cli"],
                  "trigger": ["manual"], "classes": [], "steps": steps}],
        "promptInputs": [{"id": "cli", "kind": "cli", "status": "stable", "allowStdin": False}],
        "budget": None,
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "notifiers": [NOTIFIER],
    }
    if codebase is not None:
        overrides["codebase"] = codebase
    layer = make_layer(overrides)
    _mock(layer, "build", files={"feature.py": "FEATURE = True\n"}, output="built")
    return layer


def _mock(layer: Path, step: str, **script) -> None:
    path = layer / "mock" / f"{step}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(script), encoding="utf-8")


def _reviewers(layer: Path, rounds: int, verdict: dict) -> None:
    for number in range(1, rounds + 1):
        for index in (1, 2):
            name = f"review-{index}" if number == 1 else f"review-{number}-{index}"
            _mock(layer, name, output=json.dumps(verdict))


def _run(layer: Path) -> int:
    return main("demo", layer, ["do it", "--adw-id", ADW_ID, "--dry-run", "--notify", "--class", "chore"])


def _state(layer: Path) -> dict:
    return json.loads((layer / "agents" / ADW_ID / "state.json").read_text())


def _events(layer: Path) -> list[dict]:
    return events.read(layer / "agents" / ADW_ID / "events.jsonl")


def _log(layer: Path) -> str:
    return (layer / "agents" / ADW_ID / "run.log").read_text()


def _record(state: dict, name: str) -> dict:
    return next(record for record in state["steps"] if record["name"] == name)


def _step_end(layer: Path, name: str) -> dict:
    return next(e for e in _events(layer) if e["type"] == "step.end" and e["step"] == name)


def test_rejected_review_hands_the_run_over(make_layer, capsys):
    layer = _layer(make_layer, AGENT_REVIEW)
    _reviewers(layer, 3, REFUSED)
    assert _run(layer) == 0
    out = capsys.readouterr().out
    state, log = _state(layer), _log(layer)
    assert state["status"] == "handed-over", log
    assert state["ship"]["handed_over"][0] == "review-rejected"
    record, ended = _record(state, "review"), _step_end(layer, "review")
    assert record["status"] == "rejected" and ended["status"] == "rejected"
    assert record["detail"].startswith("fail after 3 round(s): ") and ended["detail"].startswith("fail after 3 round(s): ")
    assert "step review (review) rejected: fail after 3 round(s): " in log
    assert "step review (review) passed" not in log
    assert [e for e in _events(layer) if e["type"] == "run.end"][-1]["status"] == "handed-over"
    assert "status:    handed-over" in out
    assert "review:    rejected (agent) after 3 round(s), 2 blocker(s)" in out
    handoff = next(line for line in out.splitlines() if line.startswith("handoff:"))
    assert "review-rejected" in handoff
    assert state["ship"]["action"] == "pr"                              # the ship step still ran
    assert state["notified"] == {"hook": ["needs-human"]}


def test_auto_review_rejected_lists_the_gate_fallback(make_layer):
    layer = _layer(make_layer, AUTO_REVIEW)
    _reviewers(layer, 3, REFUSED)
    assert _run(layer) == 0
    state = _state(layer)
    assert state["status"] == "handed-over", _log(layer)
    reasons = state["ship"]["handed_over"]
    assert "review-rejected" in reasons and "review-failed" in reasons
    assert _record(state, "review")["status"] == "rejected"


def test_review_passing_in_round_two_stays_passed(make_layer, capsys):
    layer = _layer(make_layer, AGENT_REVIEW)
    _reviewers(layer, 1, REFUSED)
    _mock(layer, "review-2-1", output=json.dumps(APPROVED))
    _mock(layer, "review-2-2", output=json.dumps(APPROVED))
    assert _run(layer) == 0
    out = capsys.readouterr().out
    state = _state(layer)
    assert state["status"] == "passed", _log(layer)
    record = _record(state, "review")
    assert record["status"] == "passed" and record["detail"].startswith("pass after 2 round(s)")
    assert "handed_over" not in state["ship"]
    assert "review:    pass (agent) after 2 round(s)" in out


def test_human_review_is_unaffected(make_layer, capsys):
    layer = _layer(make_layer, HUMAN_REVIEW)
    assert _run(layer) == 0
    out = capsys.readouterr().out
    state = _state(layer)
    assert state["status"] == "passed", _log(layer)
    record = _record(state, "review")
    assert record["status"] == "passed" and record["detail"] == "ok"
    assert "review:    pass (human)" in out
    assert not any(line.startswith("handoff:") for line in out.splitlines())


GATE = '#!/bin/sh\n[ -f "$ADW_LAYER_DIR/ready" ] || { echo waiting; exit 75; }\nexit 0\n'


def test_resume_after_a_waiting_step_does_not_rerun_a_rejected_review(make_layer, app_repo):
    script = app_repo / "adws" / "gate.sh"
    script.parent.mkdir(exist_ok=True)
    script.write_text(GATE, encoding="utf-8")
    script.chmod(0o755)
    for args in (["add", "adws"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "gate"]):
        subprocess.run(["git", "-C", str(app_repo), *args], check=True, capture_output=True)
    layer = _layer(make_layer, AGENT_REVIEW,
                   codebase={"commands": [{"name": "gate", "run": "./adws/gate.sh", "timeout": 30, "description": ""}]},
                   steps=[{"name": "build", "kind": "run", "command": "build", "modelSet": "inherit"},
                          {"name": "review", "kind": "review", "patch": "patch", "implement": "implement"},
                          {"name": "wait", "kind": "script", "cmd": "gate", "args": [], "timeout": 0,
                           "waitingExit": [75], "journal": ""},
                          {"name": "ship", "kind": "ship"}])
    _reviewers(layer, 3, REFUSED)
    assert _run(layer) == 75, _log(layer)
    state = _state(layer)
    assert state["status"] == "waiting"
    assert _record(state, "review")["status"] == "rejected"
    assert _record(state, "wait")["status"] == "waiting"
    calls_before = len(state["agent_calls"])
    attempts_before = _record(state, "review")["attempts"]
    review_events = lambda: [e for e in _events(layer) if e["type"] in ("step.start", "step.end") and e["step"] == "review"]
    review_end_before = sum(1 for e in review_events() if e["type"] == "step.end")
    review_events_before = len(review_events())

    (layer / "ready").write_text("", encoding="utf-8")
    assert main("demo", layer, ["--resume", "--adw-id", ADW_ID, "--notify"]) == 0, _log(layer)
    after = _state(layer)
    assert len(after["agent_calls"]) == calls_before                    # no reviewer, patch or implement ran again
    record = _record(after, "review")
    assert record["status"] == "rejected" and record["attempts"] == attempts_before
    assert sum(1 for e in review_events() if e["type"] == "step.end") == review_end_before
    assert len(review_events()) == review_events_before
    assert after["status"] == "handed-over"
    assert "review-rejected" in after["ship"]["handed_over"]
    assert _record(after, "wait")["status"] == "passed"
