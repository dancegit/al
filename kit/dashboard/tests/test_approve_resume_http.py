"""POST /api/approve-resume: Approve & resume of a run that waits only for a person's approval (its
plan's Proof plan needs `approval:vocabulary`). Real dashboard server and the layer's own
adws/dispatch.py (--approvable writes agents/<id>/approvable.json, --approve-resume records and
resumes), the mock agent, no browser."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from conftest import start_server, stop_server
from test_start import RUN_TIMEOUT, held_run, plan_build, post, wait_state

PLAN = ("# Chore: use the new word\n\n### Proof plan\n| # | Criterion | Proven by | Writes | Needs |\n"
        "|---|---|---|---|---|\n| C1 | the tests pass | `test` | - | approval:vocabulary |\n")
PATH = "/api/approve-resume"


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


def _stopped_run(running, layer: Path, prompt: str = "Add a word count") -> str:
    status, data = post(running, {**plan_build(prompt), "layer": "bare"})
    assert status == 202, data
    state = wait_state(layer, data["run_id"])
    assert state["status"] == "failed" and state["proof_plan"]["stopped"]["party"] == "approver"
    return data["run_id"]


def _approval(running, run: str, timeout: float = 60) -> dict:
    """The row's approval once the dashboard's tick asked the layer's launcher for it."""
    deadline = time.monotonic() + timeout
    row: dict = {}
    while time.monotonic() < deadline:
        row = next((r for r in running.json("/api/snapshot")["runs"] if r["id"] == run), {})
        if row.get("approval"):
            return row["approval"]
        time.sleep(0.3)
    raise AssertionError(f"run {run} never got its approval: {row.get('bucket')} {row.get('status')}")


def _body(run: str, approval: dict, **changes) -> dict:
    return {"layer": "bare", "run": run, "items": [item["id"] for item in approval["items"]],
            "token": approval["token"], "who": "Robin Example", "note": "agreed in review", **changes}


def _state(layer: Path, run: str) -> dict:
    return json.loads((layer / "agents" / run / "state.json").read_text())


def _passed(layer: Path, run: str) -> dict:
    deadline = time.monotonic() + RUN_TIMEOUT
    state: dict = {}
    while time.monotonic() < deadline:
        state = _state(layer, run)
        if state.get("status") == "passed" and state.get("ended"):
            break
        time.sleep(0.3)
    assert state["status"] == "passed", (layer / "agents" / run / "resume.log").read_text()
    return state


def test_row_gets_its_approval_from_the_layers_launcher(site):
    running, layer = site
    run = _stopped_run(running, layer)
    approval = _approval(running, run)
    assert approval["kind"] == "proof" and approval["from_step"] == "prove" and approval["after"] == "resume from prove"
    assert approval["items"][0]["id"] == "vocabulary"
    assert approval["items"][0]["text"] == "vocabulary: needed by C1: the tests pass"
    record = json.loads((layer / "agents" / run / "approvable.json").read_text())
    assert record["token"] == approval["token"] and record["waiting"] is True and record["run"] == run
    layers = {item["key"]: item for item in running.json("/api/snapshot")["layers"]}
    assert layers["bare"]["waiting_approval"] == 1
    assert running.json(f"/api/run?layer=bare&run={run}")["summary"]["approval"]["token"] == approval["token"]


def test_approve_resume_records_who_and_note_and_resumes(site):
    running, layer = site
    run = _stopped_run(running, layer)
    approval = _approval(running, run)
    status, data = post(running, _body(run, approval), path=PATH)
    assert status == 202, data
    assert data == {"status": "resumed", "run_id": run, "url": f"#/run/bare/{run}",
                    "message": f"approved vocabulary for run {run}; resuming from step prove"}
    state = _passed(layer, run)
    (given,) = state["approvals"]
    assert (given["what"], given["who"], given["via"], given["note"]) == ("vocabulary", "Robin Example", "dashboard",
                                                                         "agreed in review")
    again, refused = post(running, _body(run, approval), path=PATH)
    assert again == 409 and refused["reason"] == "stale", refused


def test_stale_token_is_refused(site):
    running, layer = site
    run = _stopped_run(running, layer)
    approval = _approval(running, run)
    status, data = post(running, _body(run, approval, token="0" * 16), path=PATH)
    assert status == 409 and data == {"error": "what was shown is stale: open it again", "reason": "stale"}
    assert "approvals" not in _state(layer, run)


def test_unknown_item_is_refused(site):
    running, layer = site
    run = _stopped_run(running, layer)
    approval = _approval(running, run)
    status, data = post(running, _body(run, approval, items=["vocabulary", "edit:docs/x.md"]), path=PATH)
    assert status == 409 and data["reason"] == "bad-item" and "edit:docs/x.md" in data["error"], data
    assert "approvals" not in _state(layer, run)


def test_a_run_whose_process_is_alive_is_refused(site):
    running, layer = site
    run = _stopped_run(running, layer)
    approval = _approval(running, run)
    with held_run(layer, run) as child:
        status, data = post(running, _body(run, approval), path=PATH)
        assert status == 409 and data["reason"] == "running", data
        assert f"process {child.pid}" in data["error"] and data["refused"] is True
    assert "approvals" not in _state(layer, run)


def test_bulk_approve_resume_answers_one_result_per_run(site):
    running, layer = site
    first = _stopped_run(running, layer, "Add a word count")
    second = _stopped_run(running, layer, "Add a line count")
    one, two = _approval(running, first), _approval(running, second)
    body = {"layer": "bare", "who": "Robin Example", "note": "",
            "runs": [{"run": first, "items": ["vocabulary"], "token": "0" * 16},
                     {"run": second, "items": ["vocabulary"], "token": two["token"]}]}
    status, data = post(running, body, path=PATH)
    assert status == 202, data
    assert data["status"] == "done" and data["resumed"] == 1
    assert [(r["run"], r["status"], r["reason"]) for r in data["results"]] == [(first, "refused", "stale"),
                                                                              (second, "resumed", "")]
    assert data["results"][1]["url"] == f"#/run/bare/{second}" and data["results"][1]["run_id"] == second
    assert "resuming from step prove" in data["results"][1]["message"]
    assert _passed(layer, second)["approvals"][0]["who"] == "Robin Example"
    assert "approvals" not in _state(layer, first) and one["token"] != two["token"]
    none, refused = post(running, {**body, "runs": body["runs"][:1]}, path=PATH)
    assert none == 409 and refused["resumed"] == 0 and len(refused["results"]) == 1


def test_bad_bodies_and_missing_guard(site):
    running, layer = site
    body = {"layer": "bare", "run": "0badc0de", "items": ["vocabulary"], "token": "0" * 16, "who": "Robin Example"}
    entry = {"run": "0badc0de", "items": ["vocabulary"], "token": "0" * 16}
    without = lambda key: {k: v for k, v in body.items() if k != key}  # noqa: E731
    for bad in ({**body, "runs": [entry]},                               # both run and runs
                without("run"),                                           # neither
                without("who"), {**body, "who": "  "}, {**body, "who": "x" * 81},
                {**body, "note": "first line\nsecond line"}, {**body, "note": "x" * 501},
                {**body, "extra": 1}, {**body, "run": "nope"},
                {**body, "items": []}, {**body, "items": "vocabulary"}, {**body, "items": ["has space"]},
                {**body, "items": ["x"] * 51}, {**body, "token": ""}, {**body, "token": "x" * 65},
                {"layer": "bare", "who": "Robin Example", "runs": []},
                {"layer": "bare", "who": "Robin Example", "runs": [entry, entry]},
                {"layer": "bare", "who": "Robin Example", "runs": [{**entry, "extra": 1}]},
                {"layer": "bare", "who": "Robin Example", "runs": [entry], "token": "0" * 16},
                ["not", "an", "object"]):
        status, data = post(running, bad, path=PATH)
        assert status == 400, (bad, status, data)
    assert post(running, body, path=PATH, X_AL_Dashboard=None)[0] == 403
    assert post(running, body, path=PATH, Origin="http://evil.example")[0] == 403
    assert post(running, {**body, "layer": "nope"}, path=PATH)[0] == 404
    status, data = post(running, body, path=PATH)
    assert status == 404 and data["error"] == "unknown run"
    status, _headers, _body_ = running.get(PATH)
    assert status == 405
    assert not (layer / "agents" / "0badc0de").exists()


def test_no_start_refuses(start_site):
    layer = start_site["bare"]
    running = start_server(["--layer", str(layer), "--no-start"])
    try:
        body = {"layer": "bare", "run": "0badc0de", "items": ["vocabulary"], "token": "0" * 16, "who": "Robin Example"}
        status, data = post(running, body, path=PATH)
        assert status == 403 and "--no-start" in data["error"]
    finally:
        stop_server(running)
