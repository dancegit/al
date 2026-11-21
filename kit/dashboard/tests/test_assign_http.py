"""GET /api/assign/preview, GET /api/assign/bulk and POST /api/assign on the scenario of assign_fixtures
(real dashboard server, the layer's own adws/assign.py with the mock agent, no browser)."""
from __future__ import annotations

import collections
import json
import shutil
import time
from pathlib import Path

import pytest

import assign_fixtures as af
import integrate_fixtures as fx
from conftest import start_server, stop_server
from test_start import post, run_dirs

RUN = af.REJECTED
STALE = "the preview is stale: open it again"
# the order a bulk start gives the nine runs: what touches only its own worktree, then one integration at a
# time (integrate, then blockers; oldest first), then what stays with a person
ORDER = [af.FAILED_CHECK, af.NOTHING, fx.ACCEPTANCE, fx.CLEAN, fx.REJECTED, af.REJECTED, fx.PROTECTED, fx.CHANGED,
         af.PERSON]


@pytest.fixture(scope="module")
def scenario_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("assign-http") / "s"
    af.build_scenario(root)
    return root


@pytest.fixture()
def layer(tmp_path: Path, scenario_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("AL_APP_ROOT", "AL_MODEL_SET", "AL_MODELS", "AL_MAX_COST", "AL_DASHBOARD_TOKEN", "AL_DECIDER"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "s"
    shutil.copytree(scenario_root, root, symlinks=True)
    return root / "pantry.al"


@pytest.fixture()
def running(layer: Path):
    server = start_server(["--layer", str(layer)])
    try:
        yield server
    finally:
        stop_server(server)


def preview(server, run: str = RUN) -> dict:
    return server.json(f"/api/assign/preview?layer=pantry&run={run}")


def start_body(nonce, **changes) -> dict:
    return {"layer": "pantry", "run": RUN, "action": "start", "confirm": nonce, **changes}


def counts(data: dict) -> dict[str, int]:
    return dict(collections.Counter(r.get("bucket") for r in data.get("runs", []) if r["layer"] == "pantry"))


def run_of(data: dict, run_id: str) -> dict:
    return next(r for r in data["runs"] if r["id"] == run_id)


def snapshot_when(server, predicate, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    data: dict = {}
    while time.monotonic() < deadline:
        data = server.json("/api/snapshot")
        if predicate(data):
            return data
        time.sleep(0.2)
    raise AssertionError(f"the snapshot never matched: {counts(data)}")


def records(layer: Path) -> list[str]:
    return sorted(p.parent.name for p in (layer / "agents").glob("*/assign.json"))


def wait_ended(layer: Path, runs: list[str], timeout: float = 240.0) -> dict[str, str]:
    """Wait until none of `runs` is running any more (their detached processes have ended) → {run: status}."""
    deadline = time.monotonic() + timeout
    status: dict[str, str] = {}
    while time.monotonic() < deadline:
        status = {}
        for run in runs:
            path = layer / "agents" / run / "state.json"
            try:
                status[run] = str(json.loads(path.read_text()).get("status")) if path.is_file() else "not written"
            except ValueError:
                status[run] = "running"                 # caught between two writes
        if not [s for s in status.values() if s in ("running", "not written")]:
            return status
        time.sleep(0.3)
    raise AssertionError(f"the runs did not end: {status}")


def wait_integration(layer: Path, run: str, timeout: float = 240.0) -> dict:
    deadline = time.monotonic() + timeout
    record: dict = {}
    while time.monotonic() < deadline:
        path = layer / "agents" / run / "integration.json"
        if path.is_file():
            record = json.loads(path.read_text())
            if record.get("status") not in ("queued", "running"):
                return record
        time.sleep(0.3)
    raise AssertionError(f"the integration of {run} did not end: {record}")


def test_preview_for_every_kind_of_stopped_run(layer, running):
    repo = layer.parent / "pantry"
    before = (run_dirs(layer), records(layer), fx.git(repo, "for-each-ref"))
    for run, kind in af.KINDS.items():
        found = preview(running, run)
        assert (found["run"], found["kind"]) == (run, kind), found
        assert found["stop"]["why"] and found["summary"] and found["steps"], found
        assert found["start"] == {"enabled": True, "why": ""} and found["attempts"] == [] and found["left"] == 2
        can_approve = any(item["approvable"] and not item["approved"] for item in found["decisions"])
        assert ("nonce" in found) == (kind != "person" or can_approve), (run, found["blocked"])
        assert (found["blocked"] == "needs-a-person") == (kind == "person"), found
        assert str(layer.parent) not in json.dumps(found), "no path of this machine"
    rejected = preview(running)
    assert rejected["blockers"] == [af.BLOCKER] and rejected["moved"] is False
    assert rejected["summary"] == "address 1 blocker (the review's patch round) and review once more"
    assert rejected["integrate"]["steps"] == ["prepare", "resolve", "address", "test", "review", "ship"]
    assert rejected["integrate"]["target_head"] == fx.git(repo, "rev-parse", "refs/heads/main")
    assert isinstance(rejected["nonce"], str) and len(rejected["nonce"]) >= 16 and preview(running)["nonce"] != rejected["nonce"]
    conflict = preview(running, fx.ACCEPTANCE)
    assert conflict["integrate"]["conflicts"] == ["INDEX.md", "stock.py"] and conflict["tiers"]
    moved = preview(running, fx.REJECTED)
    assert moved["moved"] is True and moved["summary"].startswith("bring it onto main first, then address 1 blocker")
    person = preview(running, af.PERSON)
    assert [(item["text"], item["path"], item["approvable"]) for item in person["decisions"]] == [
        (af.PERSON_BLOCKER["description"], "deploy/release.cfg", True)]
    assert preview(running, fx.CHANGED)["decisions"][0]["approvable"] is False
    check = preview(running, af.FAILED_CHECK)
    assert (check["from_step"], check["tier"]) == ("test", "heavy")
    assert (run_dirs(layer), records(layer), fx.git(repo, "for-each-ref")) == before, "a preview starts and writes nothing"
    # a run that needs nobody has a plan too: nothing to give
    done = preview(running, fx.MOVER)
    assert (done["kind"], done["blocked"]) == ("none", "not-stopped") and "nonce" not in done
    assert running.get("/api/assign/preview?layer=pantry&run=deadbeef")[0] == 404
    assert running.get(f"/api/assign/preview?layer=nowhere&run={RUN}")[0] == 404
    assert running.get("/api/assign")[0] == 405 and running.get("/api/assign/bulk?layer=nowhere")[0] == 404


def test_start_needs_the_previews_nonce(layer, running):
    nonce = preview(running)["nonce"]
    assert post(running, start_body(nonce), "/api/assign", X_AL_Dashboard=None)[0] == 403
    assert post(running, start_body(nonce), "/api/assign", Origin="http://evil.example")[0] == 403
    assert post(running, b"layer=pantry", "/api/assign", Content_Type="application/x-www-form-urlencoded")[0] == 415
    for bad in ("", "not-a-nonce", None, 7):
        body = start_body(bad) if bad is not None else {"layer": "pantry", "run": RUN, "action": "start"}
        status, answer = post(running, body, "/api/assign")
        assert status == 409 and answer["error"] == STALE, bad
    other = preview(running, fx.CLEAN)["nonce"]                 # the nonce of another run's preview
    assert post(running, start_body(other), "/api/assign")[0] == 409
    assert post(running, start_body(other, run=fx.CLEAN), "/api/assign")[0] == 409, "that refusal used it up"
    bulk = running.json("/api/assign/bulk?layer=pantry").get("nonce")      # nor is a bulk preview's a run's
    assert post(running, start_body(bulk), "/api/assign")[0] == 409
    assert records(layer) == [] and fx.integration_runs(layer) == [], "nothing started so far"
    before = run_dirs(layer)
    status, answer = post(running, start_body(nonce, note="  keep  the discount optional "), "/api/assign")
    assert status == 202, answer
    new = answer["run_id"]
    assert answer == {"status": "started", "kind": "blockers", "run": RUN, "run_id": new, "attempt": 1,
                      "url": f"#/run/pantry/{new}"}
    assert new not in before and new in run_dirs(layer)
    status, again = post(running, start_body(nonce), "/api/assign")
    assert status == 409 and again["error"] == STALE, "a nonce is used once"
    record = af.record_of(layer, RUN)["attempts"]
    assert [(a["n"], a["by"], a["kind"], a["run"], a["note"]) for a in record] == [
        (1, "dashboard", "blockers", new, "keep the discount optional")]
    at_once = running.json("/api/snapshot")           # both rows are read again before the watcher's next tick
    assert run_of(at_once, RUN)["assign"]["clicks"] == 1 and run_of(at_once, new)["integrates"] == RUN
    assert run_of(at_once, RUN)["bucket"] in ("running", "landed")
    # while the agent is on it, the plan says so and nothing more can start
    busy = preview(running)
    assert busy["kind"] == "none" or busy["attempts"][0]["outcome"] != "started", busy
    assert wait_integration(layer, RUN)["status"] == "landed", (layer / "agents" / new / "launch.log").read_text()[-3000:]
    after = snapshot_when(running, lambda data: run_of(data, RUN).get("bucket") == "landed")
    assert run_of(after, RUN)["landed_by"] == new
    assert run_of(after, RUN)["assign"]["attempts"][0]["outcome"] == "landed"
    assert "negative discount" in fx.git(layer.parent / "pantry", "show", "main:stock.py"), "the blocker was addressed"
    settled = preview(running)
    assert (settled["kind"], settled["blocked"]) == ("none", "settled") and "nonce" not in settled


@pytest.mark.parametrize("flag", ["--read-only", "--no-start"])
def test_start_is_refused_read_only_and_no_start(layer, flag):
    server = start_server(["--layer", str(layer), flag])
    try:
        found = preview(server)
        assert found["kind"] == "blockers" and "nonce" not in found
        assert found["start"]["enabled"] is False and flag in found["start"]["why"]
        person = preview(server, af.PERSON)
        assert person["kind"] == "person" and "nonce" not in person
        bulk = server.json("/api/assign/bulk?layer=pantry")
        assert "nonce" not in bulk and bulk["start"]["enabled"] is False
        item = person["decisions"][0]["id"]
        for body in (start_body("x" * 22), {"layer": "pantry", "action": "bulk", "confirm": "x" * 22},
                     {"layer": "pantry", "run": af.PERSON, "action": "approve", "item": item, "who": "Ada Example",
                      "confirm": "x" * 22}):
            status, answer = post(server, body, "/api/assign")
            assert status == 403 and flag in answer["error"], (body, answer)
        assert records(layer) == [] and fx.integration_runs(layer) == []
    finally:
        stop_server(server)


def test_approve_records_and_returns_the_new_plan(layer, running):
    snapshot_when(running, lambda data: counts(data).get("needs-you") == 9)
    before = run_dirs(layer)
    found = preview(running, af.PERSON)
    assert (found["kind"], found["blocked"]) == ("person", "needs-a-person")
    item = found["decisions"][0]["id"]
    body = {"layer": "pantry", "run": af.PERSON, "action": "approve", "item": item, "who": "Ada Example"}
    for who in ("", " ", "x" * 81, "two\nlines", 7):
        assert post(running, {**body, "who": who, "confirm": found["nonce"]}, "/api/assign")[0] == 400
    for bad in ("", "the lot", "650F1F73DE", 7):
        assert post(running, {**body, "item": bad, "confirm": found["nonce"]}, "/api/assign")[0] == 400
    status, answer = post(running, body, "/api/assign")
    assert status == 409 and answer["error"] == STALE, "an approval needs the preview's nonce too"
    status, answer = post(running, {**body, "confirm": preview(running)["nonce"]}, "/api/assign")
    assert status == 409, "another run's preview"
    # a person cannot start it instead of approving
    status, answer = post(running, start_body(found["nonce"], run=af.PERSON), "/api/assign")
    assert status == 409 and answer["reason"] == "needs-a-person", answer
    assert records(layer) == []
    found = preview(running, af.PERSON)
    status, plan = post(running, {**body, "confirm": found["nonce"]}, "/api/assign")
    assert status == 200, plan
    assert (plan["run"], plan["kind"], plan["blocked"]) == (af.PERSON, "blockers", "")
    assert [(d["id"], d["approved"]["by"]) for d in plan["decisions"]] == [(item, "Ada Example")]
    assert isinstance(plan["nonce"], str) and plan["nonce"] != found["nonce"] and plan["start"]["enabled"] is True
    record = af.record_of(layer, af.PERSON)
    assert record["attempts"] == [] and len(record["approvals"]) == 1
    approval = record["approvals"][0]
    assert (approval["id"], approval["text"], approval["path"], approval["by"], approval["via"]) == (
        item, af.PERSON_BLOCKER["description"], "deploy/release.cfg", "Ada Example", "dashboard")
    assert records(layer) == [af.PERSON] and run_dirs(layer) == before, "exactly that item, and nothing started"
    at_once = running.json("/api/snapshot")
    assert run_of(at_once, af.PERSON)["assign"]["approvals"] == 1 and run_of(at_once, af.PERSON)["bucket"] == "needs-you"
    # the same item again, and an item the plan does not have: refused in the runtime's words
    status, answer = post(running, {**body, "confirm": plan["nonce"]}, "/api/assign")
    assert status == 409 and answer["reason"] == "bad-item" and "approved already" in answer["error"], answer
    status, answer = post(running, {**body, "item": "0123456789", "confirm": preview(running, af.PERSON)["nonce"]},
                          "/api/assign")
    assert status == 409 and answer["reason"] == "bad-item"
    assert len(af.record_of(layer, af.PERSON)["approvals"]) == 1
    # an item that is a person's only (a branch a person changed) is never approved from the page
    changed = preview(running, fx.CHANGED)
    assert "nonce" not in changed and changed["decisions"][0]["approvable"] is False
    assert preview(running, af.PERSON)["kind"] == "blockers"


def test_bulk_preview_lists_the_order_and_bulk_starts(layer, running):
    snapshot_when(running, lambda data: counts(data).get("needs-you") == 9)
    before = run_dirs(layer)
    found = running.json("/api/assign/bulk?layer=pantry")
    assert [plan["run"] for plan in found["plans"]] == ORDER
    assert [plan["kind"] for plan in found["plans"]] == [af.KINDS[run] for run in ORDER]
    assert [bool(plan["skip"]) for plan in found["plans"]] == [False] * 6 + [True] * 3
    assert found["startable"] == 6 and found["start"]["enabled"] is True and found["layer"] == "pantry"
    assert all(plan["summary"] and plan["title"] for plan in found["plans"])
    assert found["plans"][-1]["skip"].startswith("needs-a-person: ")
    assert str(layer.parent) not in json.dumps(found), "no path of this machine"
    assert run_dirs(layer) == before and records(layer) == [], "looking starts nothing"
    bulk = {"layer": "pantry", "action": "bulk"}
    status, answer = post(running, bulk, "/api/assign")
    assert status == 409 and answer["error"] == STALE
    assert post(running, {**bulk, "confirm": preview(running)["nonce"]}, "/api/assign")[0] == 409, "a run's nonce is not a bulk's"
    assert post(running, {**bulk, "confirm": found["nonce"], "run": RUN}, "/api/assign")[0] == 400
    # five runs are settled by a person meanwhile: the next preview lists what is left, and that is what starts
    for run in (fx.ACCEPTANCE, fx.CLEAN, fx.REJECTED, fx.PROTECTED, fx.CHANGED):
        status, answer = post(running, {"layer": "pantry", "run": run, "as": "abandoned", "by": "", "note": "",
                                        "who": "Ada Example"}, "/api/settle")
        assert status == 200, answer
    left = running.json("/api/assign/bulk?layer=pantry")
    assert [(plan["run"], bool(plan["skip"])) for plan in left["plans"]] == [
        (af.FAILED_CHECK, False), (af.NOTHING, False), (af.REJECTED, False), (af.PERSON, True)]
    assert left["startable"] == 3
    status, answer = post(running, {**bulk, "confirm": left["nonce"]}, "/api/assign")
    assert status == 202, answer
    results = answer["results"]
    assert [(r["run"], r["kind"], r["status"]) for r in results] == [
        (af.FAILED_CHECK, "check", "started"), (af.NOTHING, "start-again", "started"),
        (af.REJECTED, "blockers", "started"), (af.PERSON, "person", "skipped")]
    assert answer["started"] == 3 and results[0]["run_id"] == af.FAILED_CHECK, "a resume is the same run"
    again, integration = results[1]["run_id"], results[2]["run_id"]
    assert again not in before and integration not in before and again != integration
    assert results[1]["url"] == f"#/run/pantry/{again}" and results[3]["run_id"] == "" and results[3]["why"]
    assert post(running, {**bulk, "confirm": left["nonce"]}, "/api/assign")[0] == 409, "a nonce is used once"
    assert [a["by"] for run in (af.FAILED_CHECK, af.NOTHING, af.REJECTED)
            for a in af.record_of(layer, run)["attempts"]] == ["dashboard"] * 3
    assert records(layer) == sorted([af.FAILED_CHECK, af.NOTHING, af.REJECTED])
    assert json.loads((layer / "agents" / again / "state.json").read_text())["replaces"] == af.NOTHING
    wait_integration(layer, af.REJECTED)
    wait_ended(layer, [af.FAILED_CHECK, again, integration])
    after = snapshot_when(running, lambda data: run_of(data, af.NOTHING).get("bucket") == "settled")
    assert run_of(after, af.NOTHING)["replaced_by"] == again


def test_bulk_takes_exactly_the_given_runs(layer, running):
    """runs= limits the bulk preview to the rows the page shows, the nonce covers exactly that list, and a
    bulk start with a different runs is refused (spending the nonce); malformed, unknown and no longer
    Needs-you ids are refused."""
    snapshot_when(running, lambda data: counts(data).get("needs-you") == 9)
    asked = f"/api/assign/bulk?layer=pantry&runs={af.FAILED_CHECK},{af.PERSON}"
    found = running.json(asked)
    assert [plan["run"] for plan in found["plans"]] == [af.FAILED_CHECK, af.PERSON]
    assert found["startable"] == 1 and sorted(found["runs"]) == sorted([af.FAILED_CHECK, af.PERSON])   # oldest first
    status, _headers, body = running.get("/api/assign/bulk?layer=pantry&runs=deadbeef")
    assert status == 409 and "deadbeef" in json.loads(body)["error"]
    assert running.get("/api/assign/bulk?layer=pantry&runs=xyz")[0] == 400
    assert running.get("/api/assign/bulk?layer=pantry&runs=")[0] == 400
    status, answer = post(running, {"layer": "pantry", "run": fx.CLEAN, "as": "abandoned", "by": "", "note": "",
                                    "who": "Ada Example"}, "/api/settle")
    assert status == 200, answer
    snapshot_when(running, lambda data: run_of(data, fx.CLEAN).get("bucket") == "settled")
    status, _headers, body = running.get(f"/api/assign/bulk?layer=pantry&runs={fx.CLEAN}")
    assert status == 409 and fx.CLEAN in json.loads(body)["error"], body
    bulk = {"layer": "pantry", "action": "bulk"}
    status, answer = post(running, {**bulk, "confirm": found["nonce"], "runs": [af.FAILED_CHECK, af.NOTHING]},
                          "/api/assign")
    assert status == 409 and answer["error"] == STALE
    status, answer = post(running, {**bulk, "confirm": found["nonce"], "runs": [af.FAILED_CHECK, af.PERSON]},
                          "/api/assign")
    assert status == 409, "the refused confirmation spent the nonce"
    assert records(layer) == []
    fresh = running.json(asked)
    assert post(running, {**bulk, "confirm": fresh["nonce"], "runs": "x"}, "/api/assign")[0] == 400
    status, answer = post(running, {**bulk, "confirm": fresh["nonce"], "runs": [af.FAILED_CHECK, af.PERSON]},
                          "/api/assign")
    assert status == 202, answer
    assert [(r["run"], r["status"]) for r in answer["results"]] == [(af.FAILED_CHECK, "started"), (af.PERSON, "skipped")]
    assert records(layer) == [af.FAILED_CHECK]
    wait_ended(layer, [af.FAILED_CHECK])


def test_unknown_keys_and_long_note_are_400(layer, running):
    nonce = preview(running)["nonce"]
    item = preview(running, af.PERSON)["decisions"][0]["id"]
    for body in (start_body(nonce, force=True), start_body(nonce, model_set="heavy"), start_body(nonce, argv=["--x"]),
                 start_body(nonce, note="x" * 501), start_body(nonce, note="two\nlines"), start_body(nonce, note=7),
                 start_body(nonce, note="a tab\there"), start_body(nonce, override="yes"), start_body(nonce, override=1),
                 start_body(nonce, item=item), start_body(nonce, who="Ada Example"), start_body(nonce, run=7),
                 start_body(nonce, action="force"), {"layer": "pantry", "action": "start", "confirm": nonce},
                 {"run": RUN, "action": "start", "confirm": nonce}, ["pantry"],
                 {"layer": "pantry", "run": af.PERSON, "action": "approve", "item": item, "who": "Ada Example",
                  "confirm": nonce, "note": "and the rest"},
                 {"layer": "pantry", "action": "bulk", "confirm": nonce, "override": True}):
        status, answer = post(running, body, "/api/assign")
        assert status == 400, (body, answer)
    assert post(running, b"{not json", "/api/assign")[0] == 400
    assert post(running, start_body(nonce, run="deadbeef"), "/api/assign")[0] == 404
    assert post(running, start_body(nonce, layer="nowhere"), "/api/assign")[0] == 404
    assert post(running, {"layer": "nowhere", "action": "bulk", "confirm": nonce}, "/api/assign")[0] == 404
    assert post(running, start_body(nonce, note="x" * 4200), "/api/assign")[0] == 413, "the body limit of every write"
    assert records(layer) == [] and fx.integration_runs(layer) == []
    # none of the refused tries used the preview up: a note of 500 characters starts it
    status, answer = post(running, start_body(nonce, note="é" * 500, override=False), "/api/assign")
    assert status == 202, answer
    assert af.record_of(layer, RUN)["attempts"][0]["note"] == "é" * 500
    wait_integration(layer, RUN)
    wait_ended(layer, [answer["run_id"]])


def test_layer_without_the_script_is_409(layer, running):
    nonce = preview(running)["nonce"]
    (layer / "adws" / "assign.py").unlink()            # as a layer generated before `al assign`
    words = "generated before `al assign`: run `al gen pantry`"
    status, _headers, body = running.get(f"/api/assign/preview?layer=pantry&run={RUN}")
    assert status == 409 and json.loads(body)["error"] == words
    status, _headers, body = running.get("/api/assign/bulk?layer=pantry")
    assert status == 409 and json.loads(body)["error"] == words
    status, answer = post(running, start_body(nonce), "/api/assign")
    assert status == 409 and answer["error"] == words
    assert post(running, start_body(nonce), "/api/assign")[1]["error"] == STALE, "and the nonce is spent"
    assert records(layer) == [] and fx.integration_runs(layer) == []
    old = start_server(["--layer", str(layer)])
    try:
        data = snapshot_when(old, lambda data: counts(data).get("needs-you") == 9)
        assert not [r["id"] for r in data["runs"] if r["assign"]["script"]]
        assert run_of(data, RUN)["next"] == f"merge {af.branch_of(RUN)} into main, or `al settle`"
        assert run_of(data, af.NOTHING)["next"] == "Start again"
    finally:
        stop_server(old)
