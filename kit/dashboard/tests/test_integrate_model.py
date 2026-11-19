""""Resolve with an agent" in the dashboard's model, on the scenario of integrate_fixtures (an invented
pantry with real git history): which runs are offered it, the buckets of attention() while an
integration runs, waits, fails or has landed, the evidence for a settlement, and the graph."""
from __future__ import annotations

import collections
import json
import shutil
import sys
from pathlib import Path

import pytest

import dashboard as d
import integrate_fixtures as fx
import settle_fixtures as sf

NEW = "c0000009"     # an integration run written by hand


@pytest.fixture(scope="module")
def scenario_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("integrate") / "s"
    fx.build_scenario(root)
    return root


@pytest.fixture()
def scenario(tmp_path: Path, scenario_root: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    monkeypatch.delenv("AL_APP_ROOT", raising=False)
    root = tmp_path / "s"
    shutil.copytree(scenario_root, root, symlinks=True)
    return {"root": root, "repo": root / "pantry", "layer": root / "pantry.al"}


def model_for(layer: Path, ticks: int = 3) -> d.Model:
    model = d.Model([layer], [])
    model.discover()
    for _ in range(ticks):
        model.tick()
    return model


def buckets(model: d.Model) -> dict[str, list[str]]:
    out: dict[str, list[str]] = collections.defaultdict(list)
    for key in sorted(model.summaries):
        out[model.attention[key]["bucket"]].append(key.split("/", 1)[1])
    return dict(out)


def item(model: d.Model, run_id: str) -> dict:
    return model.attention[f"pantry/{run_id}"]


def summary(model: d.Model, run_id: str) -> dict:
    return model.summaries[f"pantry/{run_id}"]


def integration_run(layer: Path, status: str, original: str = fx.ACCEPTANCE, run_id: str = NEW, **state) -> None:
    """An integration run of `original`, as the runtime leaves it."""
    definition = {"name": "integrate", "integration": {"run": original, "target": "main"}, "review": "gate",
                  "agent": "mock", "environment": ["wt"],
                  "steps": [{"kind": "integrate", "name": "prepare", "action": "prepare"},
                            {"kind": "integrate", "name": "resolve", "action": "resolve"},
                            {"kind": "check", "name": "test", "cmd": "test", "whenAffected": True},
                            {"kind": "review", "name": "review"},
                            {"kind": "integrate", "name": "approve", "action": "approve"},
                            {"kind": "ship", "name": "ship"}]}
    now = d.utc_stamp().replace("Z", "+00:00")
    sf.write_run(layer, run_id, adw="integrate", status=status, dry_run=False, started=now,
                 ended="" if status == "running" else now, heartbeat=now, definition=definition,
                 prompt=f"Integrate run {original} (Give stock a discount) onto main",
                 integration={"run": original, "target": "main"}, **state)


def record(layer: Path, original: str, **fields) -> None:
    data = {"run": NEW, "status": "running", "why": "", "commit": "", "target": "main", "by": "dashboard",
            "at": d.utc_stamp(), "previous": [], **fields}
    (layer / "agents" / original / d.INTEGRATION_FILE).write_text(json.dumps(data))


# ------------------------------------------------------------------------------ the offer

def test_offer_only_for_conflict_runs_whose_review_passed(scenario):
    model = model_for(scenario["layer"])
    assert buckets(model) == {"needs-you": fx.NEEDS_YOU, "landed": [fx.LANDED], "settled": [fx.REPLACED],
                              "done": sorted([fx.MOVER, fx.REPLACEMENT])}
    for run in (fx.ACCEPTANCE, fx.CLEAN):
        offer = summary(model, run)["integrate"]
        assert offer == {"offered": True, "why": "", "conflict": True, "script": True}, run
        assert item(model, run)["next"] == f"merge {fx.branch_of(run)} into main, or `al integrate`, or `al settle`, or Give to an agent"
    rejected = summary(model, fx.REJECTED)["integrate"]
    assert rejected["offered"] is False and rejected["why"] == "its review rejected it" and rejected["conflict"] is False
    assert item(model, fx.REJECTED)["next"] == f"merge {fx.branch_of(fx.REJECTED)} into main, or `al settle`, or Give to an agent"
    # a protected path and a changed branch are the runtime's to refuse: the page offers, the preview says no
    for run in (fx.PROTECTED, fx.CHANGED):
        assert summary(model, run)["integrate"]["offered"] is True, run
    passed = summary(model, fx.MOVER)["integrate"]
    assert passed["offered"] is False and "handed over" in passed["why"]
    for run in fx.HANDED_OVER:
        assert item(model, run)["landed_by"] == "" and item(model, run)["integration"] is None


def test_offer_off_without_script_or_when_switched_off(scenario):
    layer = scenario["layer"]
    settings = layer / d.INTEGRATE_SETTINGS
    settings.write_text(json.dumps({"version": 1, "enabled": False}))
    model = model_for(layer)
    off = summary(model, fx.ACCEPTANCE)["integrate"]
    assert off["offered"] is False and off["why"] == "switched off in templates/integrate.json" and off["conflict"] is True
    assert "al integrate" not in item(model, fx.ACCEPTANCE)["next"]
    settings.write_text("{broken")
    assert "templates/integrate.json" in d.integrate_offer(model.layers["pantry"], summary(model, fx.ACCEPTANCE))["why"]
    settings.write_text(json.dumps({"version": 1, "colour": "red"}))
    broken = d.integrate_offer(model.layers["pantry"], summary(model, fx.ACCEPTANCE))
    assert broken["offered"] is False and "unknown key colour" in broken["why"]
    settings.unlink()
    assert d.integrate_offer(model.layers["pantry"], summary(model, fx.ACCEPTANCE))["offered"] is True
    (layer / d.INTEGRATE_SCRIPT).unlink()       # a layer generated before the kit workflow
    old = d.integrate_offer(model.layers["pantry"], summary(model, fx.ACCEPTANCE))
    assert old == {"offered": False, "why": "run `al gen` to add adws/integrate.py", "conflict": True, "script": False}
    assert d.integrate_status(model.layers["pantry"], model) == {
        "enabled": False, "why": "generated before `al integrate`: run `al gen pantry`"}


# ------------------------------------------------------------------------------ the buckets

def test_settled_by_integration_is_in_landed_with_link(scenario):
    layer = scenario["layer"]
    integration_run(layer, "passed", ship={"action": "merge", "shipped": True, "commit": "0" * 40, "target": "main"})
    record(layer, fx.ACCEPTANCE, status="landed", commit="0" * 40)
    (layer / "agents" / fx.ACCEPTANCE / d.SETTLEMENT_FILE).write_text(json.dumps(
        {"as": "landed", "by": NEW, "note": f"landed by integration run {NEW} in 0000000 on main",
         "who": "al integrate", "at": d.utc_stamp(), "via": "integrate"}))
    d.write_settlement(layer, fx.CLEAN, as_="landed", who="Ada Example", via="dashboard", status="handed-over")
    model = model_for(layer)
    landed = item(model, fx.ACCEPTANCE)
    assert landed["bucket"] == "landed" and landed["landed_by"] == NEW
    assert landed["reason"] == f"landed by {NEW} (integration)"
    assert landed["integration"]["run"] == NEW and landed["integration"]["status"] == "landed"
    assert summary(model, NEW)["integrates"] == fx.ACCEPTANCE          # both runs link to each other
    assert summary(model, fx.ACCEPTANCE)["integration"]["run"] == NEW
    assert summary(model, fx.ACCEPTANCE)["integrate"]["offered"] is False
    person = item(model, fx.CLEAN)                                     # a person's settlement: as before
    assert person["bucket"] == "settled" and person["reason"] == "landed by Ada Example" and person["landed_by"] == ""
    assert model.needs_you("pantry") == 3


def test_running_integration_moves_original_to_running(scenario):
    layer = scenario["layer"]
    integration_run(layer, "running", pid=1, host="elsewhere")
    record(layer, fx.ACCEPTANCE, status="running")
    record(layer, fx.CLEAN, status="queued", run="c000000a")       # launched, its run not written yet
    model = model_for(layer)
    assert summary(model, NEW)["status"] in ("running", "unknown")
    for run, by in ((fx.ACCEPTANCE, NEW), (fx.CLEAN, "c000000a")):
        assert item(model, run)["bucket"] == "running" and item(model, run)["reason"] == f"being integrated by {by}"
    assert model.needs_you("pantry") == 3


def test_failed_integration_keeps_original_in_needs_you_with_why(scenario):
    layer = scenario["layer"]
    why = "stopped at resolve: integrate:undecidable: stock.py: is the discount taken before or after the tax?"
    integration_run(layer, "failed", steps=[{"name": "resolve", "kind": "integrate", "status": "failed", "attempts": 1,
                                             "started": "", "ended": "", "detail": f"resolve: {why}"}])
    record(layer, fx.ACCEPTANCE, status="failed", why=why)
    model = model_for(layer)
    failed = item(model, fx.ACCEPTANCE)
    assert failed["bucket"] == "needs-you"
    assert failed["reason"] == f"integration {NEW} did not land: {why}"
    assert failed["next"] == f"merge {fx.branch_of(fx.ACCEPTANCE)} into main, or `al integrate`, or `al settle`, or Give to an agent"
    assert summary(model, fx.ACCEPTANCE)["integrate"]["offered"] is True, "it can be integrated again"
    own = item(model, NEW)                 # the failed integration run itself is not "started again"
    assert own["bucket"] == "needs-you" and own["next"] == f"integrate {fx.ACCEPTANCE} again, or `al settle`, or Give to an agent"
    assert summary(model, NEW)["integrate"]["offered"] is False
    # once the original is settled, its failed integration needs nobody either
    d.write_settlement(layer, fx.ACCEPTANCE, as_="abandoned", who="Ada Example", via="dashboard", status="handed-over")
    model = model_for(layer)
    assert item(model, NEW)["bucket"] == "settled" and item(model, NEW)["reason"] == f"run {fx.ACCEPTANCE} is settled (abandoned)"


def test_waiting_integration_says_approve(scenario):
    layer = scenario["layer"]
    integration_run(layer, "waiting", steps=[{"name": "approve", "kind": "integrate", "status": "waiting", "attempts": 1,
                                              "started": "", "ended": "", "detail": "approve: waiting for a person"}])
    record(layer, fx.ACCEPTANCE, status="waiting", why="waits for a person's approval before it ships")
    model = model_for(layer)
    waiting = item(model, fx.ACCEPTANCE)
    assert waiting["bucket"] == "needs-you" and waiting["reason"] == f"integration {NEW} waits for approval"
    assert waiting["next"] == f"approve integration {NEW}"
    own = summary(model, NEW)
    assert own["status"] == "waiting" and own["resume"] == f"uv run adws/integrate.py approve {fx.ACCEPTANCE} --who NAME"


# ------------------------------------------------------------------------------ the evidence

def evidence(model: d.Model, run: str, kind: str) -> dict:
    code, answer = model.evidence("pantry", run, kind)
    assert code == 200, answer
    return answer


def test_evidence_landed_finds_squash_merge(scenario):
    model = model_for(scenario["layer"])
    found = evidence(model, fx.LANDED, "landed")
    squash = fx.git(scenario["repo"], "rev-parse", "refs/heads/main~1")
    assert found["found"] is True and found["as"] == "landed" and found["how"] == "patch-id"
    assert found["by"] == squash[:7] and found["note"] == "landed (patch-id) on main"
    assert len(found["evidence"]) == 1 and "the same change (equal patch-id) is a commit of main" in found["evidence"][0]
    assert squash[:7] in found["evidence"][0]


def test_evidence_landed_not_found_says_what_differs(scenario):
    model = model_for(scenario["layer"])
    found = evidence(model, fx.ACCEPTANCE, "landed")
    tip = fx.git(scenario["repo"], "rev-parse", f"refs/heads/{fx.branch_of(fx.ACCEPTANCE)}")
    assert found["found"] is False and found["by"] == "" and found["note"] == ""
    assert found["evidence"][0] == f"tip {tip[:7]} is not on main" == found["why"]
    assert "no commit of main has the same change (patch-id)" in found["evidence"]
    assert any(line.startswith("no path it changed") or "differ" in line for line in found["evidence"][2:])
    partly = evidence(model, fx.PROTECTED, "landed")
    assert partly["found"] is False
    # an integration that landed it is evidence as well
    record(scenario["layer"], fx.ACCEPTANCE, status="landed", commit=fx.git(scenario["repo"], "rev-parse", "main"))
    model = model_for(scenario["layer"])
    by_run = evidence(model, fx.ACCEPTANCE, "landed")
    assert by_run["found"] is True and by_run["by"] == NEW and by_run["how"] == "integration"


def test_evidence_replaced_proposes_later_shipped_run(scenario):
    model = model_for(scenario["layer"])
    found = evidence(model, fx.REPLACED, "replaced")
    assert found["found"] is True and found["as"] == "superseded" and found["by"] == fx.REPLACEMENT
    assert found["note"] == f"superseded by {fx.REPLACEMENT}: Redo {fx.REPLACED} on the new price table"
    assert found["evidence"] == [f"run {fx.REPLACEMENT} (passed, landed) names run {fx.REPLACED} in its request"]


def test_evidence_replaced_none(scenario):
    layer = scenario["layer"]
    model = model_for(layer)
    found = evidence(model, fx.ACCEPTANCE, "replaced")
    assert found["found"] is False and found["by"] == ""
    assert found["why"] == "no later run names it, starts it again or has the same source"
    # a later run that names it but has not landed is evidence, not a proposal
    integration_run(layer, "failed")
    model = model_for(layer)
    waiting = evidence(model, fx.ACCEPTANCE, "replaced")
    assert waiting["found"] is False and waiting["why"] == f"{NEW} has not landed yet"
    assert waiting["evidence"] == [f"run {NEW} (failed, not landed) names run {fx.ACCEPTANCE} in its request"]
    assert model.evidence("pantry", fx.ACCEPTANCE, "magic")[0] == 400
    assert model.evidence("pantry", "deadbeef", "landed")[0] == 404
    assert model.evidence("nowhere", fx.ACCEPTANCE, "landed")[0] == 404


# ------------------------------------------------------------------------------ the graph and the records

def test_graph_has_integrate_nodes(scenario):
    layer = scenario["layer"]
    integration_run(layer, "waiting")
    model = model_for(layer)
    graph = model.run_detail("pantry", NEW)["graph"]
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert [n for n in nodes] == ["prepare", "resolve", "test", "review", "approve", "ship"]
    assert (nodes["prepare"]["type"], nodes["prepare"]["sub"]) == ("integrate", "apply the branch")
    assert (nodes["resolve"]["type"], nodes["resolve"]["sub"]) == ("integrate", "resolve conflicts")
    assert (nodes["approve"]["type"], nodes["approve"]["sub"]) == ("integrate", "a person approves")
    assert nodes["test"]["type"] == "check" and nodes["ship"]["type"] == "ship"
    assert [s["name"] for s in summary(model, NEW)["steps"]] == ["prepare", "resolve", "test", "review", "approve", "ship"]


def test_runtime_settlement_is_read_by_dashboard(scenario):
    layer = scenario["layer"]
    for name in [n for n in sys.modules if n == "adw_modules" or n.startswith("adw_modules.")]:
        del sys.modules[name]
    sys.path.insert(0, str(layer / "adws"))
    try:
        from adw_modules import integrate, ir
        loaded = ir.load(layer)
        d.write_settlement(layer, fx.ACCEPTANCE, as_="abandoned", who="Ada Example", via="dashboard",
                           status="handed-over")
        written = integrate.write_settlement(loaded, fx.ACCEPTANCE, NEW, "0123456789abcdef", "main")
        integrate.write_record(loaded, fx.ACCEPTANCE, run=NEW, status="landed", commit="0123456789abcdef",
                               target="main", by="cli")
        assert integrate.REASONS == d.INTEGRATE_REASONS, "the dashboard mirrors the runtime's refusal reasons"
        assert integrate.SETTINGS_FILE.as_posix() == d.INTEGRATE_SETTINGS and integrate.RECORD_FILE == d.INTEGRATION_FILE
        assert integrate.SETTLEMENT_FILE == d.SETTLEMENT_FILE and integrate.SCRIPT.as_posix() == d.INTEGRATE_SCRIPT
    finally:
        sys.path.remove(str(layer / "adws"))
        for name in [n for n in sys.modules if n == "adw_modules" or n.startswith("adw_modules.")]:
            del sys.modules[name]
    read = d.read_settlement(layer, fx.ACCEPTANCE)
    assert read == written
    assert (read["as"], read["by"], read["via"], read["who"]) == ("landed", NEW, "integrate", "al integrate")
    assert read["previous"]["as"] == "abandoned" and read["previous"]["who"] == "Ada Example"
    assert d.settled_reason(read).startswith(f"landed ({NEW}) by al integrate")
    assert d.read_integration(layer, fx.ACCEPTANCE) == {
        "run": NEW, "status": "landed", "why": "", "commit": "0123456789abcdef", "target": "main", "by": "cli",
        "at": d.read_integration(layer, fx.ACCEPTANCE)["at"]}
    assert d.read_integration(layer, fx.CLEAN) is None
