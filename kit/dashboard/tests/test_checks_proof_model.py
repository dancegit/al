"""The dashboard on a run whose checks were decided, not all run (fixtures_events.add_proof_run):
`check.decision` events, the step status `not-affected`, and the ledger behind "What the checks
proved". A check that was not run is never shown as passed."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import dashboard
import fixtures
import fixtures_events
from test_events_model import bar, detail, model_for

T0 = fixtures.T0.timestamp()
RUN = "e1000007"


@pytest.fixture()
def proof(tmp_path: Path) -> dict[str, Path]:
    paths = fixtures_events.build(tmp_path)
    fixtures_events.add_proof_run(paths["layer"])
    return paths


def test_decision_events_become_attempts_with_an_outcome(proof):
    data = detail(model_for(proof["layer"]), RUN)
    [attempt] = data["checks"]["test"]
    assert (attempt["outcome"], attempt["why"], attempt["paths"], attempt["paths_total"]) == (
        "not-affected", "no covered path changed", ["README.md"], 1)
    assert (attempt["file"], attempt["exit"], attempt["running"]) == ("test.not-affected.log", None, False)
    assert (attempt["start"], attempt["end"]) == (T0 + 35.5, T0 + 35.5)
    assert attempt["tree"] == fixtures_events.PROOF_TREE and attempt["from_step"] == ""
    decided = bar(data, "check:test.decision.1")
    assert (decided["status"], decided["label"], decided["outcome"]) == ("not-affected", "test not affected", "not-affected")
    [ran] = data["checks"]["lint"]                      # a real attempt carries no outcome of a decision
    assert ran["exit"] == 0 and "outcome" not in ran
    assert bar(data, "check:lint.1.log")["status"] == "passed"


def test_gate_decision_is_a_gate_activity(proof):
    data = detail(model_for(proof["layer"]), RUN)
    gates = [b for b in data["timeline"]["bars"] if b["kind"] == "gate"]
    assert [(b["label"], b["status"], b["step"], b["exit"]) for b in gates] == [
        ("gate lint reused", "reused", "ship", None), ("gate test not affected", "not-affected", "ship", None)]
    reused, unaffected = gates
    assert (reused["from_step"], reused["tree"], reused["ref"]) == ("lint", fixtures_events.PROOF_TREE, "gate-lint.log")
    assert (unaffected["why"], unaffected["paths"]) == ("no covered path changed", ["README.md"])
    assert not [b for b in gates if b["status"] == "passed"]


def test_step_status_not_affected_reaches_the_graph_node(proof):
    data = detail(model_for(proof["layer"]), RUN)
    node = next(n for n in data["graph"]["nodes"] if n["id"] == "test")
    assert (node["status"], node["outcome"], node["attempts"]) == ("not-affected", "not-affected", 0)
    assert node["detail"] == "not affected: no covered path changed (1 paths compared)"
    step = data["steps_detail"]["test"]
    assert (step["status"], step["outcome"], step["why"]) == ("not-affected", "not-affected", "no covered path changed")
    lint = next(n for n in data["graph"]["nodes"] if n["id"] == "lint")
    assert (lint["status"], lint["outcome"]) == ("passed", "passed")
    assert bar(data, "step:test")["status"] == "not-affected"


def test_proof_rows_from_state(proof):
    data = detail(model_for(proof["layer"]), RUN)
    rows = data["proof"]
    assert [(r["check"], r["where"], r["outcome"]) for r in rows] == [
        ("lint", "lint", "passed"), ("lint", "gate", "reused"),
        ("test", "test", "not-affected"), ("test", "gate", "not-affected")]
    assert rows[1]["from"] == {"step": "lint", "phase": "check", "log": f"agents/{RUN}/checks/lint.1.log"}
    assert rows[1]["tree"] == fixtures_events.PROOF_TREE and rows[0]["exit"] == 0
    assert (rows[2]["why"], rows[2]["paths"], rows[2]["paths_total"], rows[2]["exit"]) == (
        "no covered path changed", ["README.md"], 1, None)
    # the latest entry per check and place wins, a review round is named, and bad entries are dropped
    state_path = proof["agents"] / RUN / "state.json"
    state = json.loads(state_path.read_text())
    state["check_results"] += [
        {"check": "test", "step": "test", "phase": "recheck", "outcome": "passed", "tree": "ab" * 20, "exit": 0,
         "log": f"agents/{RUN}/checks/test.1.log", "paths": [f"/home/someone/x-{i}" for i in range(80)], "paths_total": 80},
        {"check": "test", "step": "gate", "phase": "gate", "outcome": "failed", "tree": "", "exit": 2, "log": 7},
        {"check": "lint", "outcome": "<b>made up</b>", "phase": "gate"}, "not an entry", {"check": 3}]
    rows = dashboard.proof_view(state, lambda text: text)
    assert [(r["check"], r["where"], r["outcome"], r["exit"]) for r in rows] == [
        ("lint", "lint", "passed", 0), ("lint", "gate", "reused", 0),
        ("test", "test (review round)", "passed", 0), ("test", "gate", "failed", 2)]
    assert len(rows[2]["paths"]) == 50 and rows[2]["paths_total"] == 80 and rows[3]["from"] is None


def test_runs_without_a_ledger_have_an_empty_proof(proof):
    data = detail(model_for(proof["layer"]), "e1000001")           # a run from before the ledger
    assert data["proof"] == []
    assert all("outcome" not in attempt for attempts in data["checks"].values() for attempt in attempts)
    assert {(n["id"], n["outcome"]) for n in data["graph"]["nodes"] if n["type"] == "check"} == {("lint", ""), ("test", "")}
    assert data["steps_detail"]["lint"]["outcome"] == "" and data["steps_detail"]["lint"]["status"] == "passed"
    assert [b["status"] for b in data["timeline"]["bars"] if b["kind"] == "gate"] == ["passed", "passed"]
    assert dashboard.proof_view({"check_results": "nonsense"}, str) == [] and dashboard.proof_view(None, str) == []
