"""The step page's step-discipline line: a run step the runtime checked shows "Stayed in its step"
or the violation with what was restored; a record from before the check has none."""
from __future__ import annotations

import json
from pathlib import Path

import dashboard as d
import fixtures
from fixtures import rec, state, write

RID = "d15c0001"
OK = {"ok": True, "violations": [], "restored": [], "not_restored": []}
VIOLATION = {"ok": False, "violations": ["committed"],
             "texts": ["the build step committed: HEAD moved from 1111111 to 2222222"],
             "restored": ["HEAD reset to 1111111; the changes stay in the worktree, uncommitted"],
             "not_restored": ["refs/heads/side was created and left in place"]}


def _run(layer: Path, discipline: dict | None, *, rid: str = RID) -> None:
    run = layer / "agents" / rid
    records, t = fixtures.plan_records(30000)
    build = rec("build", "run", "passed" if discipline is None or discipline["ok"] else "failed", t, t + 30, "built")
    if discipline is not None:
        build["discipline"] = discipline
    records.append(build)
    write(run / "state.json", json.dumps(state(rid, "zte", "Discipline case", build["status"], 30000, t + 30, records)),
          t + 30)
    write(run / "run.log", f"[adw {rid}] zte start: class=chore dry_run=False\n", t + 30)


def _build_detail(layer: Path) -> dict:
    model = d.Model([layer], [])
    model.discover()
    model.tick()
    return model.run_detail("demo-shop", RID)["steps_detail"]["build"]


def test_a_step_that_stayed_in_its_step_says_so(fx) -> None:
    _run(fx["layer"], OK)
    assert _build_detail(fx["layer"])["discipline"] == {
        "ok": True, "text": "Stayed in its step: HEAD unchanged, no refs changed, nothing outside the worktree"}


def test_a_violation_is_shown_with_what_was_restored(fx) -> None:
    _run(fx["layer"], VIOLATION)
    view = _build_detail(fx["layer"])["discipline"]
    assert view["ok"] is False and view["text"].startswith("Left its step:")
    assert "the build step committed" in view["text"] and "HEAD reset to 1111111" in view["text"]
    assert "Not restored: refs/heads/side" in view["text"]


def test_records_without_discipline_have_none(fx) -> None:
    _run(fx["layer"], None)
    assert _build_detail(fx["layer"])["discipline"] is None
    assert d.discipline_view({"name": "build", "kind": "run", "status": "passed"}) is None
