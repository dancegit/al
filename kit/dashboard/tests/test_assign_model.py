""""Give to an agent" in the dashboard's model, on the scenario of assign_fixtures (the pantry of
integrate_fixtures plus a rejected run, a "needs a person" run, a failed check and a run that changed
nothing): what a run's summary says about agents/<id>/assign.json, the next action of every run under
Needs you, the reason of a run that stopped twice for the same reason, the left-out artefact count of
the run detail, and that the record is part of a run's signature."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

import assign_fixtures as af
import dashboard as d
import integrate_fixtures as fx

NO_RECORD = {"attempts": [], "clicks": 0, "left": 2, "approvals": 0, "same_reason": False}
SUFFIX = ", or Give to an agent"


@pytest.fixture(scope="module")
def scenario_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("assign") / "s"
    af.build_scenario(root)
    return root


@pytest.fixture()
def layer(tmp_path: Path, scenario_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("AL_APP_ROOT", raising=False)
    root = tmp_path / "s"
    shutil.copytree(scenario_root, root, symlinks=True)
    return root / "pantry.al"


def model_for(layer: Path, ticks: int = 3) -> d.Model:
    model = d.Model([layer], [])
    model.discover()
    for _ in range(ticks):
        model.tick()
    return model


def item(model: d.Model, run_id: str) -> dict:
    return model.attention[f"pantry/{run_id}"]


def summary(model: d.Model, run_id: str) -> dict:
    return model.summaries[f"pantry/{run_id}"]


def attempt(n: int, **fields) -> dict:
    return {"n": n, "at": d.utc_stamp(), "by": "dashboard", "who": "", "kind": "blockers", "note": "",
            "run": f"c00000a{n}", "reason": "review:blockers", "outcome": "stopped", "after": "review:blockers",
            "why": "its review rejected it again: the change is not right yet", **fields}


def write_record(layer: Path, run_id: str, attempts: list[dict], approvals: list[dict] | None = None) -> Path:
    path = layer / "agents" / run_id / d.ASSIGN_FILE
    path.write_text(json.dumps({"version": 1, "attempts": attempts, "approvals": approvals or []}))
    return path


def test_summary_has_assign(layer):
    model = model_for(layer)
    for run in af.KINDS:
        assert summary(model, run)["assign"] == {"script": True, **NO_RECORD}, run
    assert d.read_assign(layer, af.REJECTED) is None
    # what the runtime records: one click that landed nothing, the stand-in's attempt (not a click), an approval
    write_record(layer, af.REJECTED,
                 [attempt(1), attempt(2, by="decider", outcome="started", after="", why="", run="c00000a2")],
                 [{"id": "650f1f73de", "kind": "protected-path", "text": "x", "path": "deploy/release.cfg",
                   "by": "Ada Example", "at": d.utc_stamp(), "via": "dashboard"}])
    read = d.read_assign(layer, af.REJECTED)
    assert (read["clicks"], read["left"], read["approvals"], read["same_reason"]) == (1, 1, 1, False)
    assert [(a["n"], a["by"], a["kind"], a["run"], a["outcome"]) for a in read["attempts"]] == [
        (1, "dashboard", "blockers", "c00000a1", "stopped"), (2, "decider", "blockers", "c00000a2", "started")]
    assert set(read["attempts"][0]) == {"n", "at", "by", "kind", "run", "outcome", "why"}
    assert summary(model_for(layer), af.REJECTED)["assign"] == {"script": True, **read}
    # an attempt the record still calls started is shown with what the run's other records say
    write_record(layer, af.REJECTED, [attempt(1, outcome="started", after="", why="")])
    (layer / "agents" / af.REJECTED / d.INTEGRATION_FILE).write_text(json.dumps(
        {"run": "c00000a1", "status": "failed", "why": "its review rejected it again", "commit": "", "target": "main",
         "by": "dashboard", "at": d.utc_stamp(), "previous": []}))
    shown = summary(model_for(layer), af.REJECTED)["assign"]["attempts"][0]
    assert (shown["outcome"], shown["why"]) == ("stopped", "its review rejected it again")
    assert d.read_assign(layer, af.REJECTED)["attempts"][0]["outcome"] == "started", "the file is the runtime's"
    # a broken record is no record; a layer generated before `al assign` says so
    write_record(layer, af.PERSON, []).write_text("{broken")
    assert d.read_assign(layer, af.PERSON) is None
    (layer / d.ASSIGN_SCRIPT).unlink()
    old = model_for(layer)
    assert summary(old, af.PERSON)["assign"] == {"script": False, **NO_RECORD}
    assert d.assign_status(old.layers["pantry"], old) == {
        "enabled": False, "why": "generated before `al assign`: run `al gen pantry`"}


def test_summary_mirrors_the_runtime(layer):
    """The dashboard's constants and its reading of assign.json are the runtime's (adw_modules/assign.py)."""
    for name in [n for n in sys.modules if n == "adw_modules" or n.startswith("adw_modules.")]:
        del sys.modules[name]
    sys.path.insert(0, str(layer / "adws"))
    try:
        from adw_modules import assign, ir
        assert assign.RECORD_FILE == d.ASSIGN_FILE and assign.SCRIPT.as_posix() == d.ASSIGN_SCRIPT
        assert assign.MAX_CLICKS == d.ASSIGN_CLICKS and assign.NOTE_MAX == d.ASSIGN_NOTE_CHARS
        assert assign.REASONS == d.ASSIGN_REASONS and assign.MAX_WHO == d.INTEGRATE_WHO_CHARS
        write_record(layer, af.REJECTED, [attempt(1), attempt(2), attempt(3, by="decider")])
        loaded = ir.load(layer)
        plan = assign.plan(loaded, af.REJECTED)
        decision = assign.plan(loaded, af.PERSON)["decisions"][0]
        assert d.ASSIGN_ITEM.match(decision["id"])
        assign.approve(layer, af.PERSON, decision["id"], "Ada Example", via="dashboard")
    finally:
        sys.path.remove(str(layer / "adws"))
        for name in [n for n in sys.modules if n == "adw_modules" or n.startswith("adw_modules.")]:
            del sys.modules[name]
    read = d.read_assign(layer, af.REJECTED)
    assert (read["clicks"], read["left"], read["same_reason"]) == (plan["clicks"], plan["left"], plan["same_reason"])
    assert (read["clicks"], read["left"], read["same_reason"]) == (2, 0, True)
    assert [a["outcome"] for a in read["attempts"]] == [a["outcome"] for a in plan["attempts"]]
    assert d.read_assign(layer, af.PERSON) == {**NO_RECORD, "approvals": 1}


def test_needs_you_next_offers_the_agent(layer):
    model = model_for(layer)
    assert model.needs_you("pantry") == len(af.KINDS) == 9
    for run in af.KINDS:
        found = item(model, run)
        assert found["bucket"] == "needs-you" and found["next"].endswith(SUFFIX), (run, found["next"])
    assert item(model, af.REJECTED)["next"] == f"merge {af.branch_of(af.REJECTED)} into main, or `al settle`{SUFFIX}"
    assert item(model, fx.ACCEPTANCE)["next"] == (
        f"merge {fx.branch_of(fx.ACCEPTANCE)} into main, or `al integrate`, or `al settle`{SUFFIX}")
    assert item(model, af.FAILED_CHECK)["next"] == item(model, af.NOTHING)["next"] == f"Start again{SUFFIX}"
    for run in (fx.LANDED, fx.REPLACED, fx.MOVER):          # nothing is offered for a run that needs nobody
        assert SUFFIX not in item(model, run)["next"]
    # an integration that waits for a person's approval: approving it is the one next action
    (layer / "agents" / fx.CLEAN / d.INTEGRATION_FILE).write_text(json.dumps(
        {"run": "c00000b1", "status": "waiting", "why": "", "commit": "", "target": "main", "by": "dashboard",
         "at": d.utc_stamp(), "previous": []}))
    assert item(model_for(layer), fx.CLEAN)["next"] == "approve integration c00000b1"
    # a layer generated before `al assign`: the next actions are the ones from before it
    (layer / d.ASSIGN_SCRIPT).unlink()
    old = model_for(layer)
    assert item(old, af.REJECTED)["next"] == f"merge {af.branch_of(af.REJECTED)} into main, or `al settle`"
    assert item(old, af.NOTHING)["next"] == "Start again"
    assert not [run for run in af.KINDS if SUFFIX in item(old, run)["next"]]


def test_same_reason_is_in_the_reason(layer):
    before = item(model_for(layer), af.REJECTED)["reason"]
    assert before == "review-rejected, review-failed"
    write_record(layer, af.REJECTED, [attempt(1)])
    assert item(model_for(layer), af.REJECTED)["reason"] == before, "once is not twice"
    write_record(layer, af.REJECTED, [attempt(1), attempt(2)])
    model = model_for(layer)
    given = summary(model, af.REJECTED)["assign"]
    assert (given["same_reason"], given["clicks"], given["left"]) == (True, 2, 0)
    found = item(model, af.REJECTED)
    assert found["bucket"] == "needs-you"
    assert found["reason"] == f"given to an agent twice, stopped for the same reason: {before}"
    assert found["next"].endswith(SUFFIX), "the page still shows the plan, and why it stays with you"
    # twice, for two different reasons: the limit is reached, the reason stays the run's own
    write_record(layer, af.REJECTED, [attempt(1, reason="conflict", after="check:test"), attempt(2, after="review:blockers",
                                                                                               reason="check:test")])
    model = model_for(layer)
    assert summary(model, af.REJECTED)["assign"]["same_reason"] is False
    assert item(model, af.REJECTED)["reason"] == before
    assert item(model, af.PERSON)["reason"] == before, "another run's record changes nothing"


def test_left_out_count_in_the_detail(layer):
    model = model_for(layer)
    detail = model.run_detail("pantry", af.REJECTED)
    assert detail["left_out"] == [af.LEFT_OUT] == [{"id": "shots", "left_out": 3, "matched": 5}]
    assert [a["name"] for a in detail["artefacts"]] == ["a-total.png", "b-discount.png"], "the entry names no file"
    assert model.run_detail("pantry", af.PERSON)["left_out"] == []
    assert model.run_detail("pantry", fx.ACCEPTANCE)["left_out"] == []
    # an entry that says nothing was left out, or is not a count, is not shown
    manifest = layer / "artefacts" / af.REJECTED / "manifest.json"
    entries = json.loads(manifest.read_text())
    entries[-1]["left_out"] = 0
    entries.append({"id": "clips", "type": "left-out", "step": "review", "path": "", "left_out": "many", "matched": 4})
    manifest.write_text(json.dumps(entries))
    assert model_for(layer).run_detail("pantry", af.REJECTED)["left_out"] == []


def test_assign_json_is_part_of_the_signature(layer):
    model = model_for(layer)
    files = d.RunFiles(model.layers["pantry"], af.REJECTED)
    before = (files.signature(deep=False), files.signature(deep=True))
    assert summary(model, af.REJECTED)["assign"]["clicks"] == 0
    write_record(layer, af.REJECTED, [attempt(1), attempt(2)])
    assert files.signature(deep=False) != before[0] and files.signature(deep=True) != before[1]
    model.checked.clear()                          # (a finished run's files are looked at every four seconds)
    model.tick()                                   # the watcher reads the run again, and its bucket's words
    assert summary(model, af.REJECTED)["assign"]["clicks"] == 2
    assert item(model, af.REJECTED)["reason"].startswith("given to an agent twice, stopped for the same reason: ")
    other = d.RunFiles(model.layers["pantry"], af.PERSON)
    assert other.signature(deep=False) == d.RunFiles(model.layers["pantry"], af.PERSON).signature(deep=False)
