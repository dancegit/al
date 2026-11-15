"""Key user journeys through the pages, with the Flask test client (no browser)."""


def page(response):
    assert response.status_code == 200, response.status_code
    return response.get_data(as_text=True)


def test_signed_out_user_is_sent_to_sign_in(client):
    response = client.get("/shifts")
    assert response.status_code == 302 and response.headers["Location"].endswith("/signin")
    text = page(client.get("/signin"))
    assert "Priya Shah" in text and "Tom Becker" in text and "Demo sign-in" in text


def test_manager_signs_in_and_sees_the_board(client):
    text = page(client.post("/signin", data={"user_id": 1}, follow_redirects=True))
    assert "Shifts · Northfield Hospital" in text
    assert "Priya Shah · Ward manager · Northfield Hospital" in text
    assert "Ash Ward" in text and "Birch Ward" in text
    assert "4 open of 8" in text
    assert "19:30–07:30 (next day)" in text


def test_manager_assigns_a_worker_from_the_dropdown(manager):
    board = page(manager.get("/shifts"))
    assert 'id="assign-3"' in board and '<option value="4">Daniel Reyes</option>' in board
    text = page(manager.post("/shifts/3/assign", data={"worker_id": 4}, follow_redirects=True))
    assert "Assigned Daniel Reyes to Ash Ward, Tue 3 Mar 07:30–19:30." in text
    assert "3 open of 8" in text


def test_expired_worker_is_refused_on_the_board(manager):
    text = page(manager.post("/shifts/8/assign", data={"worker_id": 3}, follow_redirects=True))
    assert "Cannot assign Chloe Martin: Registration expired." in text
    assert "4 open of 8" in text


def test_manager_unassigns_a_worker(manager):
    text = page(manager.post("/shifts/1/unassign", follow_redirects=True))
    assert "Shift is open again." in text
    assert "5 open of 8" in text


def test_workers_page_shows_registration_expiry(manager):
    text = page(manager.get("/workers"))
    assert "Workers · Northfield Hospital" in text
    assert "Chloe Martin" in text and "2026-02-14" in text and "NF-RN-3915" in text


def test_worker_sees_my_shifts_and_cannot_open_workers(worker):
    text = page(worker.get("/shifts"))
    assert "My shifts" in text and "Birch Ward" in text
    assert worker.get("/workers").status_code == 403


def test_sign_out(manager):
    manager.post("/signout")
    assert manager.get("/shifts").status_code == 302
