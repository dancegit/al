from __future__ import annotations

import json
from pathlib import Path

import pytest

from adw_modules import ir, state
from adw_modules.envs import build
from adw_modules.errors import StepFailed
from adw_modules import agents
from adw_modules.review import run_review


def _workflow(make_layer, app_repo, review_kind="agent", retries=0):
    layer_dir = make_layer({
        "commands": [
            {"name": "review", "template": "review", "modelSet": "base", "output": "json"},
            {"name": "patch", "template": "patch", "modelSet": "base", "output": "path"},
            {"name": "implement", "template": "implement", "modelSet": "base", "output": "text"},
        ],
        "codebase": {"commands": [{"name": "test", "run": "./test.sh", "timeout": 5}]},
        "reviews": [{"id": "review-policy", "kind": review_kind, "status": "stable", "command": "review",
                     "reviewers": 2 if review_kind == "agent" else 1, "maxRetries": retries, "artefacts": []}],
        "adws": [{"name": "demo", "agent": "mock-agent", "review": "review-policy",
                  "environment": ["worktree"],
                  "steps": [{"name": "test", "kind": "check", "cmd": "test", "resolve": "", "retries": 0, "collect": []},
                            {"name": "review", "kind": "review", "patch": "patch", "implement": "implement"}]}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
    })
    layer = ir.load(layer_dir)
    run_state = state.ADWState("b1b2b3b4", "demo", prompt="fix issue")
    env = build(layer, run_state, ["worktree"])
    env.setup()
    return layer, run_state, env


def test_human_review_verdict(make_layer, app_repo):
    layer, run_state, env = _workflow(make_layer, app_repo, "human")
    verdict = run_review(layer, run_state, {"name": "review", "kind": "review"}, env)
    assert verdict == {"success": True, "summary": "awaiting human review", "issues": [], "mode": "human"}


def test_multiple_reviewers_combined_failure(make_layer, app_repo, monkeypatch):
    layer, run_state, env = _workflow(make_layer, app_repo)
    outputs = [json.dumps({"success": True, "summary": "ok", "issues": []}),
               json.dumps({"success": False, "summary": "bad", "issues": []})]
    monkeypatch.setattr(agents, "run_agent", lambda *args, **kwargs: outputs.pop(0))
    with pytest.raises(StepFailed, match="review failed"):
        run_review(layer, run_state, {"name": "review", "kind": "review"}, env)
    assert run_state.review["verdict"]["success"] is False


def test_blocker_patch_implement_reruns_checks_and_second_round_passes(make_layer, app_repo, monkeypatch):
    layer, run_state, env = _workflow(make_layer, app_repo, retries=1)
    checks_dir = layer.layer_dir / "agents" / run_state.adw_id / "checks"
    checks_dir.mkdir(parents=True)
    (checks_dir / "test.1.log").write_text("passed\n", encoding="utf-8")
    run_state.checks["test"] = {"cmd": "./test.sh", "exit": 0,
                                "log": f"agents/{run_state.adw_id}/checks/test.1.log"}
    run_state.steps.append({"name": "test", "kind": "check", "status": "passed", "attempts": 1})
    responses = {
        "review-1": [json.dumps({"success": False, "summary": "blocker", "issues": [
            {"severity": "blocker", "description": "broken", "resolution": "fix it"}]})],
        "review-2": [json.dumps({"success": True, "summary": "ok", "issues": []})],
        "review-2-1": [json.dumps({"success": True, "summary": "fixed", "issues": []})],
        "review-2-2": [json.dumps({"success": True, "summary": "fixed", "issues": []})],
    }
    called = []
    def fake_agent(layer_arg, state_arg, step_name, command, args, model_set, env_stack):
        called.append(step_name)
        if step_name == "patch-1":
            target = Path(layer_arg.layer_dir) / "specs" / "patch.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("patch", encoding="utf-8")
            return str(target)
        if step_name == "implement-1":
            return "implemented"
        return responses[step_name].pop(0)
    monkeypatch.setattr(agents, "run_agent", fake_agent)
    run_review(layer, run_state, {"name": "review", "kind": "review", "patch": "patch", "implement": "implement"}, env)
    assert len(run_state.review["rounds"]) == 2
    assert "patch-1" in called and "implement-1" in called
    assert Path(layer.layer_dir, run_state.checks["test"]["log"]).name == "test.2.log"
    assert (checks_dir / "test.2.log").exists()
    assert run_state.review["rounds"][0]["patch"]["checks_rerun"] == ["test"]
    assert run_state.review["verdict"]["success"] is True


def test_verdict_with_criterion_and_kind_validates() -> None:
    verdict = {"success": False, "summary": "C2 has no proof", "issues": [
        {"severity": "blocker", "description": "the report is missing", "resolution": "write it", "artefact": "",
         "criterion": "C2", "kind": "fixable"},
        {"severity": "tech_debt", "description": "naming", "resolution": "rename", "artefact": "", "criterion": "",
         "kind": "needs-declaration"},
        {"severity": "skippable", "description": "older verdicts have neither field", "resolution": ""}]}
    agents.validate_review_verdict(verdict, "review-1")
    schema = json.loads((Path(__file__).resolve().parents[2] / "schemas" / "review.schema.json").read_text())
    issue = schema["$defs"]["issue"]["properties"]
    assert issue["criterion"]["type"] == "string"
    assert tuple(issue["kind"]["enum"]) == agents.ISSUE_KINDS


def test_bad_kind_is_refused() -> None:
    bad = {"severity": "blocker", "description": "x", "resolution": "y", "artefact": "", "kind": "urgent"}
    with pytest.raises(StepFailed, match="review issue kind must be one of"):
        agents.validate_review_verdict({"success": False, "summary": "s", "issues": [bad]}, "review-1")
    with pytest.raises(StepFailed, match="criterion must be a string"):
        agents.validate_review_verdict({"success": False, "summary": "s", "issues": [
            {**bad, "kind": "fixable", "criterion": 2}]}, "review-1")
