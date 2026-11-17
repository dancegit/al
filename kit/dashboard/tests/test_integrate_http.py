"""GET /api/integrate/preview, POST /api/integrate and GET /api/settle/evidence on the scenario of
integrate_fixtures (real dashboard server, the layer's own adws/integrate.py with the mock agent, no
browser)."""
from __future__ import annotations

import collections
import json
import shutil
import time
from pathlib import Path

import pytest

import integrate_fixtures as fx
from conftest import start_server, stop_server
from test_start import headers, post

RUN = fx.ACCEPTANCE


@pytest.fixture(scope="module")
def scenario_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("integrate-http") / "s"
    fx.build_scenario(root)
    return root


@pytest.fixture()
def layer(tmp_path: Path, scenario_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("AL_APP_ROOT", raising=False)
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
    return server.json(f"/api/integrate/preview?layer=pantry&run={run}")


def start_body(nonce: str, **changes) -> dict:
    return {"layer": "pantry", "run": RUN, "action": "start", "confirm": nonce, **changes}


def snapshot_when(server, predicate, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    data: dict = {}
    while time.monotonic() < deadline:
        data = server.json("/api/snapshot")
        if predicate(data):
            return data
        time.sleep(0.2)
    raise AssertionError(f"the snapshot never matched: {counts(data)}")


def counts(data: dict) -> dict[str, int]:
    return dict(collections.Counter(r.get("bucket") for r in data.get("runs", []) if r["layer"] == "pantry"))


def run_of(data: dict, run_id: str) -> dict:
    return next(r for r in data["runs"] if r["id"] == run_id)


def wait_record(layer: Path, run: str, timeout: float = 120.0) -> dict:
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


def agents_listing(layer: Path) -> list[tuple[str, int]]:
    return sorted((str(p.relative_to(layer)), p.stat().st_size) for p in (layer / "agents").rglob("*") if p.is_file())


def test_preview_returns_plan_and_nonce(layer, running):
    before = (agents_listing(layer), fx.git(layer.parent / "pantry", "for-each-ref"))
    found = preview(running)
    repo = layer.parent / "pantry"
    assert found["eligible"] is True and found["run"] == RUN and found["branch"] == fx.branch_of(RUN)
    assert found["tip"] == fx.git(repo, "rev-parse", f"refs/heads/{fx.branch_of(RUN)}")
    assert found["target"] == "main" and found["target_head"] == fx.git(repo, "rev-parse", "refs/heads/main")
    assert found["conflicts"] == ["INDEX.md", "stock.py"] and found["generated"] == ["INDEX.md"] and found["clean"] is False
    assert found["steps"] == ["prepare", "resolve", "test", "review", "ship"] and found["approval"] == "none"
    assert found["ahead"] == [] and found["start"] == {"enabled": True, "why": ""}
    assert found["tiers"] == ["base", "heavy"] or set(found["tiers"]) >= {"base"}
    assert isinstance(found["nonce"], str) and len(found["nonce"]) >= 16
    assert preview(running)["nonce"] != found["nonce"]
    clean = preview(running, fx.CLEAN)
    assert clean["eligible"] is True and clean["clean"] is True and clean["conflicts"] == []
    assert (agents_listing(layer), fx.git(repo, "for-each-ref")) == before, "a preview writes nothing"
    assert running.get("/api/integrate/preview?layer=pantry&run=deadbeef")[0] == 404
    assert running.get("/api/integrate/preview?layer=nowhere&run=" + RUN)[0] == 404
    assert running.get("/api/integrate")[0] == 405


def test_preview_of_rejected_run_is_not_eligible(layer, running):
    found = preview(running, fx.REJECTED)
    assert found["eligible"] is False and found["reason"] == "review-rejected" and "nonce" not in found
    assert "rejected it" in found["why"]
    changed = preview(running, fx.CHANGED)
    assert changed["eligible"] is False and changed["reason"] == "branch-changed"
    assert "a person changed chore/f0000005-round-the-total since the run ended" in changed["why"]
    protected = preview(running, fx.PROTECTED)
    assert protected["reason"] == "protected-path" and "deploy/release.cfg" in protected["why"]
    for answer in (found, changed, protected):
        assert str(layer.parent) not in json.dumps(answer), "no path of this machine"


def test_start_needs_header_origin_token_and_nonce(layer, running):
    nonce = preview(running)["nonce"]
    assert post(running, start_body(nonce), "/api/integrate", X_AL_Dashboard=None)[0] == 403
    assert post(running, start_body(nonce), "/api/integrate", Origin="http://evil.example")[0] == 403
    assert post(running, b"layer=pantry", "/api/integrate", Content_Type="application/x-www-form-urlencoded")[0] == 415
    for bad in ("", "not-a-nonce", None):
        status, answer = post(running, start_body(bad) if bad is not None else
                              {"layer": "pantry", "run": RUN, "action": "start"}, "/api/integrate")
        assert status == 409 and answer["error"] == "the preview is stale: open it again", bad
    other = preview(running, fx.CLEAN)["nonce"]               # a nonce of another run
    assert post(running, start_body(other), "/api/integrate")[0] == 409
    assert post(running, start_body(other, run=fx.CLEAN), "/api/integrate")[0] == 409, "that refusal used it up"
    assert fx.integration_runs(layer) == [], "nothing started so far"
    status, answer = post(running, start_body(nonce), "/api/integrate")
    assert status == 202, answer
    assert post(running, start_body(nonce), "/api/integrate")[0] == 409, "a nonce is used once"
    wait_record(layer, RUN)


def test_start_needs_the_token_in_a_header(layer):
    server = start_server(["--layer", str(layer), "--token", "t0k"])
    try:
        status, _h, body = server.get("/api/integrate/preview?layer=pantry&run=" + RUN, {"Authorization": "Bearer t0k"})
        nonce = json.loads(body)["nonce"]
        assert status == 200
        assert server.get("/api/integrate/preview?layer=pantry&run=" + RUN)[0] == 401
        assert server.get("/api/settle/evidence?layer=pantry&run=" + RUN + "&kind=landed")[0] == 401
        assert post(server, start_body(nonce), "/api/integrate?token=t0k")[0] == 401
        assert fx.integration_runs(layer) == []
    finally:
        stop_server(server)


@pytest.mark.parametrize("flag, why", [("--read-only", "--read-only"), ("--no-start", "--no-start")])
def test_start_refused_when_read_only_or_no_start(layer, flag, why):
    server = start_server(["--layer", str(layer), flag])
    try:
        found = preview(server)
        assert found["eligible"] is True and "nonce" not in found
        assert found["start"]["enabled"] is False and why in found["start"]["why"]
        status, answer = post(server, start_body("x" * 22), "/api/integrate")
        assert status == 403 and why in answer["error"]
        status, answer = post(server, {"layer": "pantry", "run": RUN, "action": "approve", "who": "Ada"}, "/api/integrate")
        assert status == 403
        assert fx.integration_runs(layer) == []
    finally:
        stop_server(server)


def test_start_returns_run_and_original_leaves_needs_you(layer, running):
    before = snapshot_when(running, lambda data: counts(data).get("needs-you") == 5)
    assert run_of(before, RUN)["integrate"]["offered"] is True
    main = fx.git(layer.parent / "pantry", "rev-parse", "refs/heads/main")
    status, answer = post(running, start_body(preview(running)["nonce"]), "/api/integrate")
    assert status == 202, answer
    new = answer["run_id"]
    assert answer == {"status": "started", "run_id": new, "original": RUN, "url": f"#/run/pantry/{new}",
                      "resumed": False, **{k: v for k, v in answer.items() if k == "httpStatus"}}
    at_once = running.json("/api/snapshot")           # the row moved before the watcher's next tick
    assert run_of(at_once, RUN)["bucket"] in ("running", "landed")
    assert run_of(at_once, new)["integrates"] == RUN
    record = wait_record(layer, RUN)
    assert record["status"] == "landed", (layer / "agents" / new / "launch.log").read_text()[-3000:]
    after = snapshot_when(running, lambda data: run_of(data, RUN).get("bucket") == "landed")
    original = run_of(after, RUN)
    assert original["landed_by"] == new and original["reason"] == f"landed by {new} (integration)"
    assert original["integration"]["by"] == "dashboard" and original["settled"]["via"] == "integrate"
    assert counts(after).get("needs-you") == 4
    assert next(entry for entry in after["layers"] if entry["key"] == "pantry")["needs_you"] == 4
    landed = fx.git(layer.parent / "pantry", "rev-parse", "refs/heads/main")
    assert landed != main and fx.git(layer.parent / "pantry", "log", "-1", "--format=%s", landed).startswith(
        f"chore: Integrate run {RUN} (Give stock a discount) onto main [adw {new}]")
    detail = running.json(f"/api/run?layer=pantry&run={new}")
    assert [n["type"] for n in detail["graph"]["nodes"]][:2] == ["integrate", "integrate"]
    assert detail["summary"]["status"] == "passed"
    # the same run again: refused by the runtime, in its words
    again = preview(running)
    assert again["eligible"] is False and again["reason"] == "settled"


def test_start_refusal_of_the_runtime_is_409(layer, running):
    nonce = preview(running)["nonce"]
    (layer / "agents" / RUN / "settlement.json").write_text(json.dumps({"as": "abandoned", "who": "Ada Example"}))
    status, answer = post(running, start_body(nonce), "/api/integrate")
    assert status == 409 and answer["reason"] == "settled" and answer["error"].startswith("settled: ")
    assert fx.integration_runs(layer) == []


def test_start_unknown_tier_is_400(layer, running):
    nonce = preview(running)["nonce"]
    status, answer = post(running, start_body(nonce, model_set="colossal"), "/api/integrate")
    assert status == 400 and "tiers" in answer["error"]
    assert post(running, start_body(nonce, model_set="Not A Tier"), "/api/integrate")[0] == 400
    assert post(running, start_body(nonce, model_set=7), "/api/integrate")[0] == 400
    assert fx.integration_runs(layer) == []
    status, answer = post(running, start_body(nonce, model_set="base"), "/api/integrate")
    assert status == 202, answer      # the refused tries did not use the preview up
    wait_record(layer, RUN)
    state = fx.state_of(layer, answer["run_id"])
    assert next(c for c in state["agent_calls"] if c["step"] == "resolve")["tier"] == "base"


def test_approve_through_the_page(layer, running):
    (layer / "templates" / "integrate.json").write_text(json.dumps(
        {"version": 1, "approval": "person", "generated": [{"path": "INDEX.md", "regenerate": "./index.sh"}]}))
    found = preview(running)
    assert found["approval"] == "person" and found["steps"][-2:] == ["approve", "ship"]
    status, answer = post(running, start_body(found["nonce"]), "/api/integrate")
    assert status == 202, answer
    new = answer["run_id"]
    assert wait_record(layer, RUN)["status"] == "waiting"
    waiting = snapshot_when(running, lambda data: run_of(data, RUN).get("reason", "").endswith("waits for approval"))
    assert run_of(waiting, RUN)["bucket"] == "needs-you" and run_of(waiting, RUN)["next"] == f"approve integration {new}"
    for who in ("", " ", "x" * 81, "two\nlines", 7):
        assert post(running, {"layer": "pantry", "run": new, "action": "approve", "who": who}, "/api/integrate")[0] == 400
    status, answer = post(running, {"layer": "pantry", "run": new, "action": "approve", "who": "Ada Example"},
                          "/api/integrate")
    assert status == 202 and answer["run_id"] == new and answer["resumed"] is True, answer
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and wait_record(layer, RUN)["status"] == "waiting":
        time.sleep(0.3)
    assert wait_record(layer, RUN)["status"] == "landed"
    assert json.loads((layer / "agents" / new / "approval.json").read_text())["by"] == "Ada Example"
    status, answer = post(running, {"layer": "pantry", "run": new, "action": "approve", "who": "Ada Example"},
                          "/api/integrate")
    assert status == 409 and answer["reason"] == "not-waiting"


def test_evidence_endpoint_reads_only(layer, running):
    snapshot_when(running, lambda data: counts(data).get("needs-you") == 5)
    before = (agents_listing(layer), fx.git(layer.parent / "pantry", "for-each-ref"))
    landed = running.json(f"/api/settle/evidence?layer=pantry&run={fx.LANDED}&kind=landed")
    assert landed["found"] is True and landed["as"] == "landed" and landed["how"] == "patch-id"
    assert landed["run"] == fx.LANDED and landed["kind"] == "landed"
    replaced = running.json(f"/api/settle/evidence?layer=pantry&run={fx.REPLACED}&kind=replaced")
    assert replaced["found"] is True and replaced["as"] == "superseded" and replaced["by"] == fx.REPLACEMENT
    nothing = running.json(f"/api/settle/evidence?layer=pantry&run={RUN}&kind=landed")
    assert nothing["found"] is False and nothing["evidence"]
    assert running.get(f"/api/settle/evidence?layer=pantry&run={RUN}&kind=magic")[0] == 400
    assert running.get(f"/api/settle/evidence?layer=pantry&run={RUN}")[0] == 400
    assert running.get("/api/settle/evidence?layer=pantry&run=deadbeef&kind=landed")[0] == 404
    assert running.get(f"/api/settle/evidence?layer=pantry&run={RUN}&kind=landed", headers(running),
                       method="POST", body=b"{}")[0] == 405
    assert (agents_listing(layer), fx.git(layer.parent / "pantry", "for-each-ref")) == before
    # the write stays the guarded /api/settle, with what the evidence proposed
    status, answer = post(running, {"layer": "pantry", "run": fx.LANDED, "as": landed["as"], "by": landed["by"],
                                    "note": landed["note"], "who": "Ada Example"}, "/api/settle")
    assert status == 200 and answer["record"]["by"] == landed["by"], answer


def test_post_integrate_rejects_extra_keys(layer, running):
    nonce = preview(running)["nonce"]
    for body in (start_body(nonce, force=True), start_body(nonce, dry_run=True), start_body(nonce, argv=["--x"]),
                 {"layer": "pantry", "action": "start", "confirm": nonce}, ["pantry"],
                 start_body(nonce, action="force"), start_body(nonce, run=7)):
        status, answer = post(running, body, "/api/integrate")
        assert status == 400, (body, answer)
    assert post(running, b"{not json", "/api/integrate")[0] == 400
    assert post(running, start_body(nonce, run="deadbeef"), "/api/integrate")[0] == 404
    assert post(running, start_body(nonce, layer="nowhere"), "/api/integrate")[0] == 404
    assert fx.integration_runs(layer) == []
