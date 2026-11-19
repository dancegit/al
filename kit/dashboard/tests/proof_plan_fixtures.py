"""Runs of the demo-shop fixture layer whose plan has a Proof plan table (state.json `proof_plan`, as
the runtime's proofplan.py, evidence.py and convergence.py write it): planned (the build is running),
stopped before the build for a layer author, converged in one review round with debt filed, stopped
in evidence for no progress, and stopped for an approver (the Approve button)."""
from __future__ import annotations

import json
from pathlib import Path

import fixtures
from fixtures import rec, state, write

PLANNED, BEFORE_BUILD, CONVERGED, NO_PROGRESS, NEEDS_PERSON = (
    "b1000001", "b1000002", "b1000003", "b1000004", "b1000005")
HOSTILE = "<img src=x onerror=alert(1)> the cart total is right"
DECLARATION = "#:max-files 30 on (artefact image #:id shots …), or write them under proof: (proof:*.png)"


def _rows(states: tuple[str, ...], outcome: str = "proven-here", **extra) -> list[dict]:
    texts = [("C1", HOSTILE, "test"), ("C2", "every screen is shown", "e2e")]
    return [{"id": ident, "criterion": criterion, "command": command, "tests": [], "writes": [], "proof": [],
             "needs": [], "outcome": outcome, "why": "", "fix": "", "party": "", "approvals": [], "question": "",
             "state": row_state, **extra} for (ident, criterion, command), row_state in zip(texts, states)]


def _summary(**fields) -> dict:
    base = {"criteria": 2, "proven": 0, "rounds": 0, "repeated": [], "idle_seconds": 0.0, "seconds": 0.0,
            "stopped": "", "party": "", "would_have_stopped": 0, "evidence_patches": 0, "declarations": [], "debt": 0}
    return {**base, **fields}


def _run(agents: Path, rid: str, base: float, status: str, records: list[dict], proof_plan: dict, **extra) -> None:
    run = agents / rid
    end = None if status == "running" else base + 400
    write(run / "state.json", json.dumps(state(rid, "zte", f"Proof case {rid}", status, base, end, records,
                                               proof_plan=proof_plan, **extra)), base + 400)
    write(run / "run.log", f"[adw {rid}] zte start: class=chore dry_run=False\n", base + 400)


def add_proof_runs(layer: Path) -> None:
    agents = layer / "agents"
    # planned: the plan exists, the build is running
    records, t = fixtures.plan_records(30000, build=False)
    records += [rec("prove", "prove", "passed", t, t + 1, "ok"), rec("build", "run", "running", t + 1, None)]
    _run(agents, PLANNED, 30000, "running", records,
         {"plan": "specs/p.md", "table": "parsed", "rows": _rows(("planned", "planned")),
          "summary": _summary()})
    # stopped before the build: an image limit below the screenshots the plan writes
    reason = ("proof:needs-declaration: 1 criterion(s) cannot be proven here; party: layer author\n"
              f"C2 | needs-declaration | shots/*.png writes 30 files but artefact shots hands at most 12 images "
              f"to the reviewers | {DECLARATION}")
    records, t = fixtures.plan_records(31000, build=False)
    records += [rec("prove", "prove", "failed", t, t + 1, f"prove: {reason}")]
    rows = _rows(("planned", "planned"))
    rows[1].update(outcome="needs-declaration", why="shots/*.png writes 30 files but artefact shots hands at most "
                   "12 images to the reviewers", fix=DECLARATION, party="layer author")
    _run(agents, BEFORE_BUILD, 31000, "failed", records,
         {"plan": "specs/p.md", "table": "parsed", "rows": rows,
          "stopped": {"at": "prove", "outcome": "needs-declaration", "party": "layer author", "reason": reason,
                      "approvals": [], "question": ""},
          "summary": _summary(stopped="prove:needs-declaration", party="layer author", declarations=[DECLARATION])})
    # converged in one round, with debt filed to the task list
    records, t = fixtures.plan_records(32000, build=False)
    records += [rec("prove", "prove", "passed", t, t + 1, "ok"), rec("build", "run", "passed", t + 1, t + 60, "ok"),
                rec("test", "check", "passed", t + 60, t + 70, "exit 0"),
                rec("review", "review", "passed", t + 70, t + 130, "pass after 1 round(s): the evidence shows it"),
                rec("evidence", "evidence", "passed", t + 65, t + 70, "ok"),
                rec("ship", "ship", "passed", t + 130, t + 140, "ok")]
    verdict = {"success": True, "summary": "the evidence shows it", "issues": [
        {"severity": "tech_debt", "description": "cart.py: a module constant", "resolution": "read settings"}],
        "mode": "auto"}
    _run(agents, CONVERGED, 32000, "passed", records,
         {"plan": "specs/p.md", "table": "parsed", "rows": _rows(("proven", "proven")),
          "pack": {"tree": "a" * 40, "rows": [
              {"id": "C1", "state": "proven", "check": "test", "exit": 0, "tree": "current", "passed": 4, "failed": 0,
               "skipped": 0, "tests": {"tests/test_cart.py::test_total": "named in log"}, "files": [], "note": ""},
              {"id": "C2", "state": "proven", "check": "e2e", "exit": 0, "tree": "current", "passed": 2, "failed": 0,
               "skipped": 0, "tests": {}, "files": ["artefacts/b1000003/proof-image/cart.png"], "note": ""}]},
          "rounds": [{"round": 1, "success": True, "blockers": [], "resolved": [], "open": [], "new": [],
                      "seconds": 60.0}],
          "why_stopped": "passed",
          "debt": [{"fingerprint": "cart.py|fixable", "where": "tasklist:tasks.md:12",
                    "text": "debt from run b1000003: cart.py: a module constant — read settings"}],
          "summary": _summary(proven=2, rounds=1, seconds=60.0, stopped="passed", debt=1)},
         review={"mode": "auto", "verdict": verdict, "rounds": [{"round": 1, "reviewers": [verdict], "verdict": verdict}]})
    # stopped in evidence: the run-scoped proof never written, after two patches
    reason = "evidence:no-progress: C2 still files-missing after 2 patches; party: person"
    records, t = fixtures.plan_records(33000, build=False)
    records += [rec("prove", "prove", "passed", t, t + 1, "ok"), rec("build", "run", "passed", t + 1, t + 60, "ok"),
                rec("evidence", "evidence", "failed", t + 60, t + 200, f"evidence: {reason}")]
    _run(agents, NO_PROGRESS, 33000, "failed", records,
         {"plan": "specs/p.md", "table": "parsed", "rows": _rows(("proven", "files-missing")),
          "stopped": {"at": "evidence", "outcome": "no-progress", "party": "person", "reason": reason,
                      "approvals": [], "question": ""},
          "gaps": [{"round": 0, "gaps": [{"id": "C2", "state": "files-missing"}], "patch": {"number": 1}},
                   {"round": 1, "gaps": [{"id": "C2", "state": "files-missing"}], "patch": {"number": 2}},
                   {"round": 2, "gaps": [{"id": "C2", "state": "files-missing"}]}],
          "summary": _summary(proven=1, stopped="evidence:no-progress", party="person", evidence_patches=2,
                              repeated=[{"fingerprint": "C2|fixable", "cause": "fixable"}])})
    # stopped for an approver: a layer word only a person may approve
    question = f"may this run vocabulary? answer: al approve LAYER {NEEDS_PERSON} vocabulary"
    reason = ("proof:needs-person: 1 criterion(s) cannot be proven here; party: approver\n"
              f"C2 | needs-person | vocabulary is a decision only a person may take | {question}")
    records, t = fixtures.plan_records(34000, build=False)
    records += [rec("prove", "prove", "failed", t, t + 1, f"prove: {reason}")]
    rows = _rows(("planned", "planned"))
    rows[1].update(outcome="needs-person", why="vocabulary is a decision only a person may take", fix=question,
                   party="approver", approvals=["vocabulary"], question=question)
    _run(agents, NEEDS_PERSON, 34000, "failed", records,
         {"plan": "specs/p.md", "table": "parsed", "rows": rows,
          "stopped": {"at": "prove", "outcome": "needs-person", "party": "approver", "reason": reason,
                      "approvals": ["vocabulary"], "question": question},
          "summary": _summary(stopped="prove:needs-person", party="approver")})
