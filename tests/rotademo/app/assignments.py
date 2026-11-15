"""Assigning workers to shifts: applies rules.py to an organisation's data (data.Scope).

The UI routes and the JSON API both call assign() and unassign(), so the rules hold the same way
in both. Outcome.status is the HTTP status the API answers with.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import rules
from data import Scope, to_rule_shift, to_rule_worker

SHIFT_NOT_FOUND = "Shift not found"
WORKER_NOT_FOUND = "Worker not found"
ALREADY_FILLED = "Shift is already filled"
NOT_FILLED = "Shift is not filled"


@dataclass(frozen=True)
class Outcome:
    ok: bool
    status: int
    reason: str = ""
    shift: dict[str, Any] | None = None
    worker: dict[str, Any] | None = None


def problem_for(scope: Scope, worker: dict[str, Any], shift: dict[str, Any], today: date) -> str | None:
    """Why this worker of the organisation cannot take this shift, or None."""
    others = [to_rule_shift(other) for other in scope.shifts_of_worker(worker["id"])]
    return rules.assignment_problem(to_rule_worker(worker), to_rule_shift(shift), others, today)


def candidates(scope: Scope, shift: dict[str, Any]) -> list[dict[str, Any]]:
    """Workers offered in the shift's assign dropdown: the organisation's workers with the right role."""
    return scope.workers_with_role(shift["role_required"])


def assign(scope: Scope, shift_id: int, worker_id: int, today: date) -> Outcome:
    shift = scope.shift(shift_id)
    if shift is None:
        return Outcome(False, 404, SHIFT_NOT_FOUND)
    worker = scope.worker(worker_id)
    if worker is None:
        return Outcome(False, 404, WORKER_NOT_FOUND, shift=shift)
    if shift["worker_id"] is not None:
        return Outcome(False, 409, ALREADY_FILLED, shift=shift, worker=worker)
    problem = problem_for(scope, worker, shift, today)
    if problem:
        return Outcome(False, 409, problem, shift=shift, worker=worker)
    scope.set_worker(shift_id, worker_id)
    return Outcome(True, 200, shift=scope.shift(shift_id), worker=worker)


def unassign(scope: Scope, shift_id: int) -> Outcome:
    shift = scope.shift(shift_id)
    if shift is None:
        return Outcome(False, 404, SHIFT_NOT_FOUND)
    if shift["worker_id"] is None:
        return Outcome(False, 409, NOT_FILLED, shift=shift)
    scope.set_worker(shift_id, None)
    return Outcome(True, 200, shift=scope.shift(shift_id))
