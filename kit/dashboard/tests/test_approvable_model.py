"""Approve & resume in the model (no server): a Needs-you run's `approval`, read from the
agents/<id>/approvable.json its layer's runtime wrote (read_approvable), the count behind the
"waiting for approval (N)" chip, the same model on the run page, and hostile text kept as data."""
from __future__ import annotations

import json
from pathlib import Path

import approvable_fixtures as apf
import dashboard as d


def _model(layer: Path) -> tuple[d.Model, dict]:
    records = apf.add_approvable_runs(layer)
    model = d.Model([layer], [])
    model.discover()
    model.tick()
    return model, records


def _row(model: d.Model, run: str) -> dict:
    return next(r for r in model.snapshot()["runs"] if r["id"] == run)


def test_valid_record_gives_the_row_its_approval(fx):
    model, records = _model(fx["layer"])
    row = _row(model, apf.PROOF)
    assert row["bucket"] == "needs-you"
    assert row["approval"] == {"kind": "proof", "from_step": "prove", "after": "resume from prove",
                               "token": records[apf.PROOF]["token"],
                               "items": [{"id": "vocabulary", "kind": "vocabulary", "paths": [],
                                          "text": "vocabulary: needed by C2: every screen is shown"}]}
    paths = _row(model, apf.PATHS)["approval"]
    assert (paths["kind"], paths["from_step"]) == ("protected-paths", "evidence")
    assert paths["items"][0]["paths"] == apf.PROTECTED and paths["items"][0]["text"].startswith("protected paths: 2 files")


def test_record_with_another_basis_is_ignored(fx):
    model, _records = _model(fx["layer"])
    assert _row(model, apf.STALE)["approval"] is None
    assert _row(model, apf.DECLARATION)["approval"] is None       # no record at all
    assert _row(model, "a1000001")["approval"] is None            # a run that never stopped for a person
    # a record that says the run does not wait, or has no items or no token, gives no approval either
    path = fx["layer"] / "agents" / apf.PROOF / d.APPROVABLE_FILE
    good = json.loads(path.read_text())
    state = json.loads((fx["layer"] / "agents" / apf.PROOF / "state.json").read_text())
    assert d.read_approvable(fx["layer"], apf.PROOF, state) is not None
    for change in ({"waiting": False}, {"items": []}, {"token": ""}, {"items": [{"id": "has space", "text": "x"}]}):
        path.write_text(json.dumps({**good, **change}))
        assert d.read_approvable(fx["layer"], apf.PROOF, state) is None, change
    # the run moved on (an approval was recorded): the record is about an earlier state
    path.write_text(json.dumps(good))
    assert d.read_approvable(fx["layer"], apf.PROOF, {**state, "approvals": [{"what": "vocabulary"}]}) is None


def test_basis_is_the_one_the_fixture_computes(fx):
    apf.add_approvable_runs(fx["layer"])
    state = json.loads((fx["layer"] / "agents" / apf.PATHS / "state.json").read_text())
    assert d.approval_basis(fx["layer"], apf.PATHS, state) == apf.basis(fx["layer"], apf.PATHS)
    assert d.approval_basis(fx["layer"], apf.PATHS, state).startswith("failed|") and state["ended"] in apf.basis(
        fx["layer"], apf.PATHS)


def test_layer_counts_runs_waiting_for_approval(fx):
    model, _records = _model(fx["layer"])
    assert model.waiting_approval("demo-shop") == 2
    layer = next(item for item in model.layer_list() if item["key"] == "demo-shop")
    assert layer["waiting_approval"] == 2 and layer["needs_you"] >= 4
    rows = {r["id"]: r for r in model.snapshot()["runs"]}
    assert rows[apf.DECLARATION]["bucket"] == "needs-you" and rows[apf.DECLARATION]["approval"] is None
    assert rows[apf.STALE]["bucket"] == "needs-you"
    assert model.waiting_approval("no-such-layer") == 0


def test_run_detail_has_the_approval_for_the_banner(fx):
    model, records = _model(fx["layer"])
    summary = model.run_detail("demo-shop", apf.PATHS)["summary"]
    assert summary["bucket"] == "needs-you" and summary["approval"]["token"] == records[apf.PATHS]["token"]
    assert summary["approval"]["after"] == "resume from evidence"
    assert model.run_detail("demo-shop", apf.DECLARATION)["summary"]["approval"] is None


def test_hostile_item_text_stays_text(fx):
    apf.add_approvable_runs(fx["layer"])
    hostile = "<img src=x onerror=alert(1)> vocabulary " + "x" * 400
    apf.record(fx["layer"], apf.PROOF, "proof", [{**apf.PROOF_ITEM, "text": hostile, "paths": ["<b>docs</b>/x.md"] * 80}],
               "prove", "<script>alert(2)</script> resume from prove")
    model = d.Model([fx["layer"]], [])
    model.discover()
    model.tick()
    approval = _row(model, apf.PROOF)["approval"]
    (item,) = approval["items"]
    assert item["text"].startswith("<img src=x onerror=alert(1)> vocabulary ") and len(item["text"]) == d.APPROVABLE_TEXT
    assert item["paths"] == ["<b>docs</b>/x.md"] * d.APPROVABLE_ITEMS       # data, cut to the limit
    assert approval["after"] == "<script>alert(2)</script> resume from prove"


def test_a_layer_without_a_launcher_is_never_asked(fx, monkeypatch):
    asked = []
    monkeypatch.setattr(d, "approvable_via_layer", lambda layer, runs, **more: asked.append(runs) or (0, {}, ""))
    model, _records = _model(fx["layer"])
    model.approvable_tick = 0.0
    model.tick_approvable(1e12)
    assert asked == [] and not model.may_run_scripts(model.layers["demo-shop"])
