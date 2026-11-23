"""POST /api/approve-ship: Approve & ship / Approve & merge of a handed-over run whose gate only asked
for a person's yes (ship_fixtures: `diff-too-large`, green checks, a passed review). Real dashboard
server and the layer's own adws/dispatch.py (--approvable writes agents/<id>/approvable.json,
--approve-ship records the approval and fast-forwards the stage branch or settles the run as shipped);
no browser, and no git command of the dashboard's."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import dashboard
import ship_fixtures as shf
from conftest import start_server, stop_server
from test_start import post

PATH = "/api/approve-ship"
RUN, OTHER, THIRD = "f2000001", "f2000002", "f2000003"


@pytest.fixture()
def site(start_site):
    layer, app = start_site["bare"], start_site["app"]
    shf.add_ship_run(layer, app, RUN)
    running = start_server(["--layer", str(layer)])
    try:
        yield running, layer, app
    finally:
        stop_server(running)


def _row(running, run: str) -> dict:
    return next((r for r in running.json("/api/snapshot")["runs"] if r["id"] == run), {})


def _approval(running, run: str, timeout: float = 60) -> dict:
    """The row's approval once the dashboard's tick asked the layer's launcher for it."""
    deadline = time.monotonic() + timeout
    row: dict = {}
    while time.monotonic() < deadline:
        row = _row(running, run)
        if row.get("approval"):
            return row["approval"]
        time.sleep(0.3)
    raise AssertionError(f"run {run} never got its approval: {row.get('bucket')} {row.get('status')}")


def _body(run: str, approval: dict, **changes) -> dict:
    return {"layer": "bare", "run": run, "token": approval["token"], "who": "Robin Example",
            "note": "agreed in review", **changes}


def _state(layer: Path, run: str) -> dict:
    return json.loads((layer / "agents" / run / "state.json").read_text())


def _settlement(layer: Path, run: str) -> dict | None:
    path = layer / "agents" / run / "settlement.json"
    return json.loads(path.read_text()) if path.is_file() else None


def _refs(app: Path) -> str:
    return shf.git(app, "for-each-ref", "--format=%(refname) %(objectname)")


def _bucket(running, run: str, wanted: str, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    row: dict = {}
    while time.monotonic() < deadline:
        row = _row(running, run)
        if row.get("bucket") == wanted:
            return row
        time.sleep(0.3)
    raise AssertionError(f"run {run} is under {row.get('bucket')}, not {wanted}")


def test_row_gets_its_ship_approval_from_the_layers_launcher(site):
    running, layer, _app = site
    approval = _approval(running, RUN)
    assert approval["kind"] == "ship" and [item["id"] for item in approval["items"]] == ["ship"]
    ship = approval["ship"]
    assert (ship["way"], ship["label"], ship["target"]) == ("merge", "Approve & merge", "integration")
    assert (ship["diff_lines"], ship["files_total"], ship["files"]) == (3148, 1, [f"shelf_{RUN}.py"])
    assert ship["reasons"] == [shf.DIFF] and ship["commit"] == _state(layer, RUN)["ship"]["commit"]
    assert ship["checks"] == [{"name": "test", "phase": "check", "state": "passed"}] and ship["pr_url"] == ""
    record = json.loads((layer / "agents" / RUN / "approvable.json").read_text())
    assert record["token"] == approval["token"] and record["kind"] == "ship" and record["run"] == RUN
    assert record["token"] == shf.record(layer, RUN)["token"], "the fixture's record is the runtime's"
    layers = {item["key"]: item for item in running.json("/api/snapshot")["layers"]}
    assert layers["bare"]["waiting_approval"] == 1
    assert running.json(f"/api/run?layer=bare&run={RUN}")["summary"]["approval"]["ship"]["label"] == "Approve & merge"


def test_approve_ship_records_who_and_note_and_merges_through_the_launcher(site, monkeypatch):
    running, layer, app = site
    approval = _approval(running, RUN)
    commit, main = _state(layer, RUN)["ship"]["commit"], shf.git(app, "rev-parse", "main")
    calls: list[tuple] = []
    real = dashboard.approve_ship_via_layer

    def recording(layer_, run_id, token, who, note, *more):
        calls.append((layer_.key, run_id, token, who, note))
        return real(layer_, run_id, token, who, note, *more)

    monkeypatch.setattr(dashboard, "approve_ship_via_layer", recording)
    status, data = post(running, _body(RUN, approval), path=PATH)
    assert status == 200, data
    branch = _state(layer, RUN)["branch"]
    assert data == {"status": "merged", "run_id": RUN, "url": f"#/run/bare/{RUN}",
                    "message": f"approved by Robin Example: {branch} is merged into integration ({commit[:7]})"}
    assert calls == [("bare", RUN, approval["token"], "Robin Example", "agreed in review")]
    (given,) = _state(layer, RUN)["approvals"]
    assert (given["what"], given["who"], given["via"], given["note"]) == (
        f"ship:{shf.DIFF}", "Robin Example", "dashboard", "agreed in review")
    assert shf.git(app, "rev-parse", "integration") == commit and shf.git(app, "rev-parse", "main") == main
    settled = _settlement(layer, RUN)
    assert (settled["as"], settled["by"], settled["who"], settled["via"]) == ("landed", commit, "Robin Example",
                                                                             "dashboard")
    row = _bucket(running, RUN, "settled")                # it left Needs you
    assert row["approval"] is None and row["settled"]["as"] == "landed"
    events = (layer / "agents" / RUN / "events.jsonl").read_text()
    assert '"type":"approval.granted"' in events and '"type":"ship.approved"' in events
    again, refused = post(running, _body(RUN, approval), path=PATH)
    assert again == 409 and refused["reason"] == "stale" and len(calls) == 1, refused


def test_stale_token_is_409(site):
    running, layer, app = site
    approval = _approval(running, RUN)
    before = _refs(app)
    status, data = post(running, _body(RUN, approval, token="0" * 16), path=PATH)
    assert status == 409 and data == {"error": "what was shown is stale: open it again", "reason": "stale"}
    assert "approvals" not in _state(layer, RUN) and _settlement(layer, RUN) is None and _refs(app) == before


def test_red_checks_are_refused_with_the_reason(site):
    running, layer, app = site
    approval = _approval(running, RUN)
    before = _refs(app)
    shf.set_checks(layer, RUN, 1)                         # a check went red after the page got the approval
    status, data = post(running, _body(RUN, approval), path=PATH)
    assert status == 409 and data["refused"] is True, data
    assert data["reason"] in ("not-waiting", "checks-not-green") and "test failed" in data["error"]
    assert "approvals" not in _state(layer, RUN) and _settlement(layer, RUN) is None and _refs(app) == before
    assert _row(running, RUN)["bucket"] == "needs-you"


def test_without_the_write_guard_is_403_and_bad_bodies_are_400(site):
    running, layer, app = site
    before = _refs(app)
    body = {"layer": "bare", "run": RUN, "token": "0" * 16, "who": "Robin Example"}
    entry = {"run": RUN, "token": "0" * 16}
    without = lambda key: {k: v for k, v in body.items() if k != key}  # noqa: E731
    for bad in ({**body, "runs": [entry]},                               # both run and runs
                without("run"),                                           # neither
                without("who"), {**body, "who": "  "}, {**body, "who": "x" * 81},
                {**body, "note": "first line\nsecond line"}, {**body, "note": "x" * 501},
                {**body, "extra": 1}, {**body, "items": ["ship"]}, {**body, "run": "nope"},
                {**body, "token": ""}, {**body, "token": "x" * 65}, without("token"),
                {"layer": "bare", "who": "Robin Example", "runs": []},
                {"layer": "bare", "who": "Robin Example", "runs": [entry, entry]},
                {"layer": "bare", "who": "Robin Example", "runs": [{**entry, "items": ["ship"]}]},
                {"layer": "bare", "who": "Robin Example", "runs": [entry], "token": "0" * 16},
                ["not", "an", "object"]):
        status, data = post(running, bad, path=PATH)
        assert status == 400, (bad, status, data)
    assert post(running, body, path=PATH, X_AL_Dashboard=None)[0] == 403
    assert post(running, body, path=PATH, Origin="http://evil.example")[0] == 403
    assert post(running, {**body, "layer": "nope"}, path=PATH)[0] == 404
    status, data = post(running, {**body, "run": "0badc0de"}, path=PATH)
    assert status == 404 and data["error"] == "unknown run"
    status, _headers, _body_ = running.get(PATH)
    assert status == 405
    assert "approvals" not in _state(layer, RUN) and _settlement(layer, RUN) is None and _refs(app) == before


def test_no_start_refuses(start_site):
    layer, app = start_site["bare"], start_site["app"]
    shf.add_ship_run(layer, app, RUN)
    token = shf.record(layer, RUN)["token"]
    running = start_server(["--layer", str(layer), "--no-start"])
    try:
        status, data = post(running, {"layer": "bare", "run": RUN, "token": token, "who": "Robin Example"}, path=PATH)
        assert status == 403 and "--no-start" in data["error"]
    finally:
        stop_server(running)
    assert "approvals" not in _state(layer, RUN) and _settlement(layer, RUN) is None


def test_bulk_answers_one_result_per_run(site):
    running, layer, app = site
    shf.add_ship_run(layer, app, OTHER, way="pr", reasons=("class-not-allowed:feature",))
    shf.add_ship_run(layer, app, THIRD, way="branch")
    one, two, three = (_approval(running, run) for run in (RUN, OTHER, THIRD))
    assert (two["ship"]["way"], two["ship"]["pr_url"], three["ship"]["way"]) == ("pr", shf.PR_URL, "branch")
    main = shf.git(app, "rev-parse", "main")
    body = {"layer": "bare", "who": "Robin Example", "note": "",
            "runs": [{"run": THIRD, "token": "0" * 16}, {"run": RUN, "token": one["token"]},
                     {"run": OTHER, "token": two["token"]}]}
    status, data = post(running, body, path=PATH)
    assert status == 200, data
    assert data["status"] == "done" and data["approved"] == 2
    assert [(r["run"], r["status"], r["reason"]) for r in data["results"]] == [
        (THIRD, "refused", "stale"), (RUN, "merged", ""), (OTHER, "shipped", "")]
    assert data["results"][1]["url"] == f"#/run/bare/{RUN}" and "is merged into integration" in data["results"][1]["message"]
    assert f"shipped to {shf.PR_URL}" in data["results"][2]["message"]
    assert shf.git(app, "rev-parse", "integration") == _state(layer, RUN)["ship"]["commit"]
    assert shf.git(app, "rev-parse", "main") == main
    shipped = _settlement(layer, OTHER)
    assert (shipped["as"], shipped["by"]) == ("shipped", shf.PR_URL)
    assert _state(layer, OTHER)["approvals"][0]["what"] == "ship:class-not-allowed:feature"
    assert "approvals" not in _state(layer, THIRD) and _settlement(layer, THIRD) is None
    assert _bucket(running, OTHER, "settled")["reason"].startswith(f"shipped ({shf.PR_URL}) by Robin Example")
    none, refused = post(running, {**body, "runs": body["runs"][:1]}, path=PATH)
    assert none == 409 and refused["approved"] == 0 and len(refused["results"]) == 1
    # the third is approved on its own: the default branch is never moved, a person merges
    status, data = post(running, _body(THIRD, three, note=""), path=PATH)
    assert status == 200 and data["status"] == "shipped" and "a person merges it" in data["message"], data
    assert shf.git(app, "rev-parse", "main") == main
    assert _settlement(layer, THIRD)["by"] == _state(layer, THIRD)["ship"]["commit"]


def test_approve_resume_refuses_a_ship_approval(site):
    running, layer, app = site
    approval = _approval(running, RUN)
    before = _refs(app)
    body = {"layer": "bare", "run": RUN, "items": ["ship"], "token": approval["token"], "who": "Robin Example"}
    status, data = post(running, body, path="/api/approve-resume")
    assert status == 409 and data == {"error": "this run waits for a ship approval: use Approve & ship",
                                      "reason": "ship"}
    assert "approvals" not in _state(layer, RUN) and _settlement(layer, RUN) is None and _refs(app) == before


def test_settle_as_shipped_over_http(site):
    running, layer, _app = site
    approval = _approval(running, RUN)
    body = {"layer": "bare", "run": RUN, "as": "shipped", "who": "Ada", "note": "opened by hand"}
    status, data = post(running, body, path="/api/settle")
    assert status == 400 and data["error"] == "shipped needs by: the pull request's URL or the commit"
    assert post(running, {**body, "by": "javascript:alert(1)"}, path="/api/settle")[0] == 400
    assert _settlement(layer, RUN) is None
    status, data = post(running, {**body, "by": shf.PR_URL}, path="/api/settle")
    assert status == 200, data
    assert (data["record"]["as"], data["record"]["by"], data["record"]["via"]) == ("shipped", shf.PR_URL, "dashboard")
    row = _bucket(running, RUN, "settled")
    assert row["reason"] == f"shipped ({shf.PR_URL}) by Ada: opened by hand"
    status, data = post(running, _body(RUN, approval), path=PATH)      # settled: nothing is left to approve
    assert status == 409 and data["reason"] == "stale" and "approvals" not in _state(layer, RUN)
