"""A layer for the dashboard's "Ship without review" tests, written as code so every byte is reviewable:

  agentic-layer.json              fixtures.IR: the auto review option zero-touch (max diff lines 300, ships by
                                  merge, requires lint and test, allows chore and bug) and the human option
  agentic.rkt                     the definition the sources are read from: it writes #:ship, #:require-checks
                                  and #:max-diff-lines of zero-touch and leaves #:allowed-classes to al's default
  adws/adw_modules/review.py      a stand-in for the generated gate: only the line the dashboard looks for
  agents/d1ff0001/                a zte run of class bug that reached ship and was handed to a person because
                                  its diff of 2500 lines in 12 files is larger than the limit of 300
  agents/settings/review.json     only with `override` (written by the dashboard's own write_review_setting)

Nothing here is real: invented names, invented paths, no run data copied.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import dashboard
import fixtures

NAME = "demo-shop"
RUN = "d1ff0001"
OPTION = "zero-touch"
DECLARED = 300
DIFF = 2500
FILES = 12
REASON = f"diff-too-large:{DIFF}>{DECLARED}"
TEXT = f"diff {DIFF} lines > limit {DECLARED} (review option {OPTION}), {FILES} files changed"

DEFINITION = """#lang al
;; demo-shop: the fixture layer of the dashboard's "Ship without review" tests.
;; Only the review clauses are read by the dashboard (which keywords are written here).
(agentic-layer #:name "demo-shop"
  (review human)
  (review auto #:id zero-touch #:reviewers 2 #:ship merge
          #:require-checks (lint test) #:max-diff-lines 300)
  )
"""

GATE = "# a stand-in for the generated gate (kit/runtime/adw_modules/review.py): the dashboard reads one line of it\n"
GATE_READS = "READS_LOCAL_LIMIT = True   # the gate applies agents/settings/review.json (reviewpolicy.py)\n"


def stopped_state(run_id: str = RUN, *, reasons: list[str] | None = None, files: int | None = FILES,
                  start: float = 1000) -> dict:
    """state.json of a zte run that passed every step and was handed over at the gate for `reasons`."""
    reasons = [REASON] if reasons is None else reasons
    steps, t = fixtures.plan_records(start, "bug")
    for name, kind in (("lint", "check"), ("test", "check"), ("commit", "run"), ("review", "review"), ("ship", "ship")):
        steps.append(fixtures.rec(name, kind, "passed", t, t + 20, "handed over" if name == "ship" else ""))
        t += 20
    gate = {"action": "pr", "ship": False, "reasons": list(reasons), "diff_lines": DIFF}
    if files is not None:
        gate["changed_files"] = [f"src/module_{i}.py" for i in range(files)]
    return fixtures.state(
        run_id, "zte", "Rework the price rules of the cart", "handed-over", start, t, steps, klass="bug",
        branch=f"bug/{run_id}-price-rules", checks={"lint": {"exit": 0}, "test": {"exit": 0}},
        review={"verdict": {"success": True, "issues": []}},
        ship={"action": "pr", "shipped": False, "reasons": list(reasons), "handed_over": list(reasons), "gate": gate})


def write_run(layer: Path, run_id: str = RUN, **changes) -> Path:
    run = layer / "agents" / run_id
    data = stopped_state(run_id, **changes)
    fixtures.write(run / "state.json", json.dumps(data, indent=2))
    reasons = ", ".join(data["ship"]["reasons"])
    fixtures.write(run / "run.log", f"[adw {run_id}] zte start: class=bug dry_run=False\n"
                                    f"[adw {run_id}] gate: action pr (fallback: {reasons})\n"
                                    f"[adw {run_id}] handed over: {reasons}\n")
    return run


def build_review_site(root: Path, *, gate_reads: bool = True, definition: bool = True,
                      override: int | None = None) -> dict[str, Path]:
    """root/demo-shop.al: the layer, its definition, the gate stand-in and the run stopped for size."""
    layer = Path(root) / "demo-shop.al"
    fixtures.write(layer / "agentic-layer.json", json.dumps(copy.deepcopy(fixtures.IR), indent=2))
    if definition:
        fixtures.write(layer / "agentic.rkt", DEFINITION)
    fixtures.write(layer / "adws" / "adw_modules" / "review.py", GATE + (GATE_READS if gate_reads else ""))
    run = write_run(layer)
    if override is not None:
        dashboard.write_review_setting(layer, OPTION, override, who="Ada", via="dashboard", at="2026-11-23T10:00:00Z")
    return {"layer": layer, "run": run}
