"""Pure business rules (rules.py): no database, no HTTP."""
from datetime import date, datetime

import rules
from rules import Shift, Worker

TODAY = date(2026, 3, 2)
RN, HCA = "Registered nurse", "Healthcare assistant"


def nurse(expires=date(2027, 1, 31)):
    return Worker(id=1, name="Test Nurse", role=RN, registration_expires=expires)


def shift(shift_id, start, end, role=RN):
    return Shift(id=shift_id, role_required=role, starts_at=datetime.fromisoformat(start),
                 ends_at=datetime.fromisoformat(end))


DAY = shift(10, "2026-03-03T07:30", "2026-03-03T19:30")


def test_valid_worker_can_take_a_free_shift():
    assert rules.assignment_problem(nurse(), DAY, [], TODAY) is None


def test_role_must_match():
    assert rules.assignment_problem(nurse(), shift(11, "2026-03-03T07:30", "2026-03-03T19:30", HCA), [], TODAY) == rules.WRONG_ROLE


def test_expired_registration_is_refused():
    assert rules.assignment_problem(nurse(expires=date(2026, 3, 1)), DAY, [], TODAY) == rules.REGISTRATION_EXPIRED


def test_registration_is_valid_on_its_expiry_day():
    assert not rules.registration_expired(nurse(expires=TODAY), TODAY)
    assert rules.registration_expired(nurse(expires=date(2026, 3, 1)), TODAY)


def test_overlapping_shift_is_refused():
    other = shift(12, "2026-03-03T14:00", "2026-03-03T22:00")
    assert rules.assignment_problem(nurse(), DAY, [other], TODAY) == rules.ALREADY_WORKING


def test_back_to_back_shifts_do_not_overlap():
    before = shift(13, "2026-03-02T19:30", "2026-03-03T07:30")
    assert not rules.overlaps(before, DAY)


def test_the_shift_itself_is_ignored():
    assert rules.assignment_problem(nurse(), DAY, [DAY], TODAY) is None
