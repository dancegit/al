"""Pure business rules for staffing: no database, no Flask, no clock. Easy to test.

A worker may be assigned to a shift when:
  - the worker's role is the role the shift requires;
  - the worker's professional registration has not expired (it is valid up to and including
    its expiry date);
  - the worker is not already working at an overlapping time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

WRONG_ROLE = "Wrong role for this shift"
REGISTRATION_EXPIRED = "Registration expired"
ALREADY_WORKING = "Already working at that time"


@dataclass(frozen=True)
class Worker:
    id: int
    name: str
    role: str
    registration_expires: date


@dataclass(frozen=True)
class Shift:
    id: int
    role_required: str
    starts_at: datetime
    ends_at: datetime
    worker_id: int | None = None


def registration_expired(worker: Worker, today: date) -> bool:
    return worker.registration_expires < today


def overlaps(first: Shift, second: Shift) -> bool:
    return first.starts_at < second.ends_at and second.starts_at < first.ends_at


def assignment_problem(worker: Worker, shift: Shift, workers_shifts: list[Shift], today: date) -> str | None:
    """Why `worker` cannot take `shift`, or None when they can. `workers_shifts` are the
    worker's other assigned shifts (the shift itself, if listed, is ignored)."""
    if worker.role != shift.role_required:
        return WRONG_ROLE
    if registration_expired(worker, today):
        return REGISTRATION_EXPIRED
    for other in workers_shifts:
        if other.id != shift.id and overlaps(other, shift):
            return ALREADY_WORKING
    return None
