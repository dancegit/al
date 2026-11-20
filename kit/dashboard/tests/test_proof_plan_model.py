"""The run page's proof card in the model (RunModel.detail["proof_plan"], proof_plan_view): the table
with a state per criterion from the moment the plan exists, why a run stopped before its build and
who can clear it, the review rounds' progress, hostile text kept as data, and the Layers view's
review numbers (review_convergence)."""
from __future__ import annotations

import json
from pathlib import Path

import dashboard as d
import proof_plan_fixtures as pf


def _model(layer: Path) -> d.Model:
    pf.add_proof_runs(layer)
    model = d.Model([layer], [])
    model.discover()
    model.tick()
    return model


def test_detail_carries_planned_rows_before_build(fx):
    model = _model(fx["layer"])
    card = model.run_detail("demo-shop", pf.PLANNED)["proof_plan"]
    assert card["table"] == "parsed" and [(r["id"], r["state"]) for r in card["rows"]] == [("C1", "planned"),
                                                                                         ("C2", "planned")]
    assert card["stopped"] == {} and card["pack"] == [] and card["rounds"] == []
    old = model.run_detail("demo-shop", "a1000001")
    assert old["proof_plan"] == {}   # a run without a Proof plan


def test_detail_carries_stop_party_and_fix(fx):
    model = _model(fx["layer"])
    card = model.run_detail("demo-shop", pf.BEFORE_BUILD)["proof_plan"]
    assert (card["stopped"]["at"], card["stopped"]["outcome"], card["stopped"]["party"]) == (
        "prove", "needs-declaration", "layer author")
    assert card["stopped"]["reason"].startswith("proof:needs-declaration:")
    assert card["rows"][1]["fix"] == pf.DECLARATION and card["rows"][1]["party"] == "layer author"
    person = model.run_detail("demo-shop", pf.NEEDS_PERSON)["proof_plan"]["stopped"]
    assert person["party"] == "approver" and person["approvals"] == ["vocabulary"]
    assert person["question"].startswith("may this run vocabulary?")


def test_detail_carries_rounds_progress(fx):
    model = _model(fx["layer"])
    card = model.run_detail("demo-shop", pf.CONVERGED)["proof_plan"]
    assert card["rounds"] == [{"round": 1, "resolved": [], "open": [], "new": [], "seconds": 60.0, "success": True}]
    assert card["why_stopped"] == "passed"
    assert [r["state"] for r in card["pack"]] == ["proven", "proven"]
    assert card["pack"][1]["files"] == ["artefacts/b1000003/proof-image/cart.png"]
    assert card["debt"] == [{"text": "debt from run b1000003: cart.py: a module constant — read settings",
                             "where": "tasklist:tasks.md:12"}]
    stuck = model.run_detail("demo-shop", pf.NO_PROGRESS)["proof_plan"]
    assert stuck["stopped"]["outcome"] == "no-progress" and stuck["rows"][1]["state"] == "files-missing"


def test_hostile_text_is_data(fx):
    model = _model(fx["layer"])
    card = model.run_detail("demo-shop", pf.PLANNED)["proof_plan"]
    assert card["rows"][0]["criterion"] == pf.HOSTILE   # unchanged: the page sets it as text, never as markup
    view = d.proof_plan_view({"proof_plan": {"table": "parsed", "rows": [{"id": "C1", "criterion": "x" * 5000}],
                                             "stopped": {"reason": None}}})
    assert len(view["rows"][0]["criterion"]) == 500 and view["stopped"]["reason"] == ""
    assert d.proof_plan_view({"proof_plan": {"table": "absent"}}) == {}
    assert d.proof_plan_view({"proof_plan": "not a dict"}) == {} and d.proof_plan_view(None) == {}


def test_layers_reviews_numbers(fx):
    model = _model(fx["layer"])
    (layer,) = [item for item in model.layer_list() if item["key"] == "demo-shop"]
    reviews = layer["reviews"]
    assert reviews["runs_with_proof_plan"] == 5
    assert reviews["rounds_per_run"] == {"mean": 1.0, "max": 1}
    assert reviews["stopped_early"]["by_reason"] == {"prove:needs-declaration": 1, "evidence:no-progress": 1,
                                                     "prove:needs-person": 1}
    assert reviews["stopped_early"]["by_party"] == {"layer author": 1, "person": 1, "approver": 1}
    assert reviews["repeated_blockers"] == 1 and reviews["causes"] == {"fixable": 1}
    assert reviews["missing_declarations"] == [{"declaration": pf.DECLARATION, "runs": 1}]


def test_fixture_timelines_follow_plan_prove_build(fx):
    """plan, then prove, then build only if prove passed: a run stopped at prove has no build at all."""
    layer = fx["layer"]
    pf.add_proof_runs(layer)

    def steps(rid: str) -> list[dict]:
        return json.loads((layer / "agents" / rid / "state.json").read_text())["steps"]

    for rid in (pf.BEFORE_BUILD, pf.NEEDS_PERSON):
        records = steps(rid)
        assert not [r for r in records if r["name"] == "build"]
        assert (records[-1]["name"], records[-1]["status"]) == ("prove", "failed")
    records = steps(pf.PLANNED)
    after_plan = [r["name"] for r in records if r["name"] != "classify" and not r["name"].startswith("plan-")]
    assert after_plan == ["prove", "build"]
    builds = [r for r in records if r["name"] == "build"]
    assert len(builds) == 1 and builds[0]["status"] == "running"


def test_pack_row_carried_shows_where_it_was_carried_from():
    state = {"proof_plan": {"table": "parsed", "rows": [], "pack": {"rows": [
        {"id": "C1", "state": "proven", "tree": "carried", "carried": {"from_tree": "ab" * 20, "changed": ["README.md"]}},
        {"id": "C2", "state": "proven", "tree": "current"}]}}}
    pack = d.proof_plan_view(state)["pack"]
    assert pack[0]["carried"] == "carried from abababab (only README.md changed)"
    assert pack[1]["carried"] == ""
