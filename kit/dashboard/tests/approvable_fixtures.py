"""Runs of the demo-shop fixture layer that wait (or do not wait) for a person's approval, with the
agents/<id>/approvable.json the layer's runtime writes (adw_modules/approvable.py; JSON only here):

- PROOF: failed, stopped at `prove` for an approver; a record of kind `proof` with the item `vocabulary`;
- PATHS: failed, stopped at `evidence` for an approver; a record of kind `protected-paths` (two paths);
- STALE: as PROOF, but its record was derived from another state of the run (another basis);
- DECLARATION: failed, stopped for the layer author (needs-declaration); no record.

`record` computes the basis from the same files as dashboard.approval_basis (and the runtime's
approvable.basis); all names are invented."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import fixtures
from fixtures import rec, state, write

PROOF, PATHS, STALE, DECLARATION = "c2000001", "c2000002", "c2000003", "c2000004"
PROTECTED = ["docs/pantry/SHELVES.md", "docs/pantry/TILL.md"]
PROOF_ITEM = {"id": "vocabulary", "kind": "vocabulary", "text": "vocabulary: needed by C2: every screen is shown",
              "what": ["vocabulary"], "paths": [], "decisions": []}
PATHS_ITEM = {"id": "protected-paths", "kind": "protected-path",
              "text": f"protected paths: 2 files: {', '.join(PROTECTED)}", "what": [f"edit:{p}" for p in PROTECTED],
              "paths": list(PROTECTED), "decisions": []}


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def basis(layer: Path, run: str) -> str:
    """The same string as dashboard.approval_basis, from the run's files."""
    folder = layer / "agents" / run
    run_state, given, integration = (_read(folder / name) for name in ("state.json", "assign.json", "integration.json"))
    decision = run_state.get("decision") if isinstance(run_state.get("decision"), dict) else {}
    count = lambda value: str(len(value)) if isinstance(value, list) else "0"  # noqa: E731
    return "|".join([str(run_state.get("status") or ""), str(run_state.get("ended") or ""),
                     count(run_state.get("approvals")), count(given.get("approvals")), count(given.get("attempts")),
                     str(decision.get("status") or ""), str(integration.get("status") or "")])


def record(layer: Path, run: str, kind: str, items: list[dict[str, Any]], from_step: str, after: str,
           basis_: str | None = None) -> dict[str, Any]:
    """Write agents/<run>/approvable.json as approvable.write does; returns the record."""
    found = basis(layer, run) if basis_ is None else basis_
    token = hashlib.sha256(json.dumps([run, found, from_step, [[item["id"], item["text"]] for item in items]])
                           .encode("utf-8")).hexdigest()[:16]
    data = {"version": 1, "run": run, "basis": found, "waiting": True, "kind": kind, "items": items,
            "from_step": from_step, "after": after, "why": "", "token": token, "at": "2026-11-18T10:00:00+00:00"}
    (layer / "agents" / run / "approvable.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                                                            encoding="utf-8")
    return data


def _rows(approvals: list[str], party: str, outcome: str) -> list[dict[str, Any]]:
    base = {"tests": [], "writes": [], "proof": [], "needs": [], "why": "", "fix": "", "question": ""}
    return [{**base, "id": "C1", "criterion": "the cart total is right", "command": "test", "outcome": "proven-here",
             "party": "", "approvals": [], "state": "planned"},
            {**base, "id": "C2", "criterion": "every screen is shown", "command": "e2e", "outcome": outcome,
             "party": party, "approvals": list(approvals), "state": "planned"}]


def _stopped_run(layer: Path, run: str, base: float, at: str, party: str, outcome: str, approvals: list[str],
                 reason: str) -> None:
    records, t = fixtures.plan_records(base, build=at != "prove")
    if at == "prove":
        records += [rec("prove", "prove", "failed", t, t + 1, f"prove: {reason}")]
    else:
        records += [rec("prove", "prove", "passed", t, t + 1, "ok"),
                    rec("evidence", "evidence", "failed", t + 60, t + 70, f"evidence: {reason}")]
    plan = {"plan": "specs/p.md", "table": "parsed", "rows": _rows(approvals if at == "prove" else [], party, outcome),
            "stopped": {"at": at, "outcome": outcome, "party": party, "reason": reason, "approvals": list(approvals),
                        "question": f"may this run {', '.join(approvals)}?" if approvals else ""}}
    folder = layer / "agents" / run
    write(folder / "state.json", json.dumps(state(run, "zte", f"Approval case {run}", "failed", base, base + 400,
                                                  records, proof_plan=plan)), base + 400)
    write(folder / "run.log", f"[adw {run}] zte start: class=chore dry_run=False\n", base + 400)


def add_approvable_runs(layer: Path) -> dict[str, dict[str, Any]]:
    """The four runs of the module docstring → {run id: its approvable.json record} for those that have one."""
    person = "proof:needs-person: 1 criterion(s) cannot be proven here; party: approver"
    _stopped_run(layer, PROOF, 40000, "prove", "approver", "needs-person", ["vocabulary"], person)
    _stopped_run(layer, PATHS, 41000, "evidence", "approver", "needs-person", [f"edit:{p}" for p in PROTECTED],
                 f"evidence:needs-person: the run changed 2 protected path(s) without an approval: "
                 f"{', '.join(PROTECTED)}; party: approver")
    _stopped_run(layer, STALE, 42000, "prove", "approver", "needs-person", ["vocabulary"], person)
    _stopped_run(layer, DECLARATION, 43000, "prove", "layer author", "needs-declaration", [],
                 "proof:needs-declaration: 1 criterion(s) cannot be proven here; party: layer author")
    return {PROOF: record(layer, PROOF, "proof", [dict(PROOF_ITEM)], "prove", "resume from prove"),
            PATHS: record(layer, PATHS, "protected-paths", [dict(PATHS_ITEM)], "evidence", "resume from evidence"),
            STALE: record(layer, STALE, "proof", [dict(PROOF_ITEM)], "prove", "resume from prove",
                          basis_="failed|2026-01-01T00:00:00+00:00|1|0|0||")}
