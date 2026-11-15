"""The JSON API: behaviour, the assignment rules over HTTP, and permission checks."""
from conftest import RIVERSIDE_MANAGER, sign_in


def shifts_by_id(client):
    return {shift["id"]: shift for shift in client.get("/api/shifts").get_json()["shifts"]}


# ---------------------------------------------------------------- reading

def test_manager_lists_own_organisations_shifts(manager):
    shifts = shifts_by_id(manager)
    assert set(shifts) == {1, 2, 3, 4, 5, 6, 7, 8}
    assert shifts[3] == {"id": 3, "ward_id": 1, "ward": "Ash Ward", "role_required": "Registered nurse",
                         "starts_at": "2026-03-03T07:30", "ends_at": "2026-03-03T19:30",
                         "status": "open", "worker_id": None, "worker": None}
    assert shifts[1]["status"] == "filled" and shifts[1]["worker"] == "Daniel Reyes"


def test_manager_lists_workers_with_registration_expiry(manager):
    workers = {worker["name"]: worker for worker in manager.get("/api/workers").get_json()["workers"]}
    assert set(workers) == {"Amara Jones", "Ben Clarke", "Chloe Martin", "Daniel Reyes", "Sam Okafor"}
    assert workers["Chloe Martin"]["registration_expires"] == "2026-02-14"
    assert workers["Sam Okafor"]["employment"] == "agency"


def test_worker_sees_only_own_shifts(worker):
    assert set(shifts_by_id(worker)) == {6}


# ---------------------------------------------------------------- assigning

def test_assign_eligible_worker(manager):
    response = manager.post("/api/shifts/3/assign", json={"worker_id": 4})
    assert response.status_code == 200
    assert response.get_json()["shift"]["worker"] == "Daniel Reyes"
    assert shifts_by_id(manager)[3]["status"] == "filled"


def test_assign_expired_worker_is_refused_with_409(manager):
    response = manager.post("/api/shifts/8/assign", json={"worker_id": 3})
    assert response.status_code == 409
    assert response.get_json() == {"error": "Registration expired"}
    assert shifts_by_id(manager)[8]["status"] == "open"


def test_assign_wrong_role_is_refused(manager):
    response = manager.post("/api/shifts/7/assign", json={"worker_id": 4})
    assert response.status_code == 409
    assert response.get_json()["error"] == "Wrong role for this shift"


def test_assign_overlapping_shift_is_refused(manager):
    assert manager.post("/api/shifts/3/assign", json={"worker_id": 4}).status_code == 200
    response = manager.post("/api/shifts/5/assign", json={"worker_id": 4})
    assert response.status_code == 409
    assert response.get_json()["error"] == "Already working at that time"


def test_assign_filled_shift_is_refused(manager):
    response = manager.post("/api/shifts/1/assign", json={"worker_id": 1})
    assert response.status_code == 409
    assert response.get_json()["error"] == "Shift is already filled"


def test_assign_needs_a_worker_id(manager):
    assert manager.post("/api/shifts/3/assign", json={}).status_code == 400


def test_unassign_opens_the_shift(manager):
    response = manager.post("/api/shifts/1/unassign")
    assert response.status_code == 200
    assert response.get_json()["shift"]["status"] == "open"
    assert manager.post("/api/shifts/1/unassign").status_code == 409


# ---------------------------------------------------------------- permissions

def test_signed_out_api_answers_401(client):
    for path in ("/api/shifts", "/api/workers"):
        assert client.get(path).status_code == 401
    assert client.post("/api/shifts/3/assign", json={"worker_id": 4}).status_code == 401


def test_worker_cannot_assign_or_list_workers(worker):
    assert worker.post("/api/shifts/3/assign", json={"worker_id": 5}).status_code == 403
    assert worker.post("/api/shifts/6/unassign").status_code == 403
    assert worker.get("/api/workers").status_code == 403


def test_other_organisations_shift_is_not_found(manager):
    assert manager.post("/api/shifts/102/assign", json={"worker_id": 4}).status_code == 404


def test_other_organisations_worker_cannot_be_assigned(manager):
    response = manager.post("/api/shifts/3/assign", json={"worker_id": 101})
    assert response.status_code == 404
    assert shifts_by_id(manager)[3]["status"] == "open"


def test_riverside_manager_sees_only_riverside(client):
    sign_in(client, RIVERSIDE_MANAGER)
    assert set(shifts_by_id(client)) == {101, 102, 103, 104, 105}
