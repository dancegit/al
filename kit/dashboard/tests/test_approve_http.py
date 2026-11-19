"""POST /api/approve: the Approve button of a run stopped for a decision only a person may take (its
plan's Proof plan needs `approval:vocabulary`). Real dashboard server, the layer's own
adws/dispatch.py --grant, the mock agent, no browser."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from conftest import start_server, stop_server
from test_start import RUN_TIMEOUT, key, plan_build, post, wait_state

PLAN = ("# Chore: use the new word\n\n### Proof plan\n| # | Criterion | Proven by | Writes | Needs |\n"
        "|---|---|---|---|---|\n| C1 | the tests pass | `test` | - | approval:vocabulary |\n")


@pytest.fixture()
def site(start_site):
    layer = start_site["bare"]
    (layer / "mock").mkdir(exist_ok=True)
    (layer / "mock" / "plan.json").write_text(json.dumps({"layer_files": {"specs/plan-word.md": PLAN},
                                                          "output": "specs/plan-word.md"}))
    running = start_server(["--layer", str(layer)])
    try:
        yield running, layer
    finally:
        stop_server(running)


def _stopped_run(running, layer: Path) -> str:
    status, data = post(running, {**plan_build(), "layer": "bare"})
    assert status == 202, data
    state = wait_state(layer, data["run_id"])
    assert state["status"] == "failed" and state["proof_plan"]["stopped"]["party"] == "approver"
    return data["run_id"]


def test_approve_runs_dispatch_grant(site):
    running, layer = site
    run = _stopped_run(running, layer)
    detail = running.json(f"/api/run?layer=bare&run={run}")
    assert detail["proof_plan"]["stopped"]["approvals"] == ["vocabulary"]
    status, data = post(running, {"layer": "bare", "run": run, "what": ["vocabulary"]}, path="/api/approve")
    assert status == 202, data
    assert data["status"] == "resumed" and data["url"] == f"#/run/bare/{run}"
    deadline = time.monotonic() + RUN_TIMEOUT
    state: dict = {}
    while time.monotonic() < deadline:
        state = json.loads((layer / "agents" / run / "state.json").read_text())
        if state.get("status") == "passed" and state.get("ended"):
            break
        time.sleep(0.3)
    assert state["status"] == "passed", (layer / "agents" / run / "resume.log").read_text()
    assert [(a["what"], a["who"], a["via"]) for a in state["approvals"]] == [("vocabulary", "dashboard", "dashboard")]
    again, refused = post(running, {"layer": "bare", "run": run, "what": ["vocabulary"]}, path="/api/approve")
    assert again == 409 and refused["reason"] == "not-stopped-for-an-approver"


def test_approve_refuses_bad_token_and_missing_guard(site):
    running, layer = site
    body = {"layer": "bare", "run": "0badc0de", "what": ["vocabulary"]}
    for bad in ({**body, "what": ["Not A Token"]}, {**body, "what": []}, {**body, "what": "vocabulary"},
                {**body, "run": "nope"}, {**body, "extra": 1}):
        status, data = post(running, bad, path="/api/approve")
        assert status == 400, bad
    assert post(running, body, path="/api/approve", X_AL_Dashboard=None)[0] == 403
    assert post(running, body, path="/api/approve", Origin="http://evil.example")[0] == 403
    assert post(running, {**body, "layer": "nope"}, path="/api/approve")[0] == 404
    status, data = post(running, body, path="/api/approve")
    assert status == 409 and data["reason"] == "unknown-run"
    status, _headers, _body = running.get("/api/approve")
    assert status == 405
    assert key()   # the start helpers are shared with test_start
