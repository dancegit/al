# Rota

Staffing and shift scheduling for hospital wards. A ward manager sees the shifts of their own
organisation, assigns an eligible worker to an open shift, and checks each worker's professional
registration. Two fictional organisations share the app, **Northfield Hospital** and **Riverside
Care Group**, and neither may ever see or change the other's data.

**Demo app.** All data is synthetic (`seed.py`), and sign-in is demo-grade: pick a seeded user,
no password. No clinical or patient data of any kind.

## Rules the app enforces

A worker can be assigned to a shift only when their role is the role the shift requires, their
registration has not expired (it is valid up to and including its expiry date), and they are not
already working at an overlapping time. The rules are in `rules.py` and hold the same way on the
shifts board and in the JSON API.

## Layout

| Path | What it is |
|---|---|
| `app.py` | `create_app()` and the routes (HTTP only) |
| `auth.py` | demo sign-in, roles (`manager`, `worker`), the organisation scope of the signed-in user |
| `data.py` | the only module that runs SQL on tenant data: `Scope`, bound to one organisation |
| `rules.py` | pure business rules |
| `assignments.py` | applies the rules to an organisation's data, for the pages and the API alike |
| `db.py` | SQLite connection and the migration runner |
| `migrations/NNN_*.sql` | the schema, applied in order at start-up; applied files are never edited |
| `seed.py` | deterministic synthetic data for a fixed today, 2026-03-02 |
| `clock.py` | "today"; `ROTA_TODAY=YYYY-MM-DD` fixes it |
| `templates/`, `static/style.css` | the pages: sign-in, shifts board, workers, my shifts |
| `tests/` | pytest: `test_rules.py`, `test_api.py`, `test_journeys.py`, `test_isolation.py` |
| `e2e/scenario.py` | the user scenario walked in a real browser |
| `e2e/capture.py` | runs the scenario in headless Chromium, saves screenshots and a video to `e2e-results/` |

## Pages and API

| Route | Who | What |
|---|---|---|
| `GET /signin`, `POST /signin`, `POST /signout` | anyone | demo sign-in |
| `GET /shifts` | manager | the shifts board, with an assign dropdown per open shift |
| `GET /shifts` | worker | my shifts |
| `POST /shifts/<id>/assign`, `POST /shifts/<id>/unassign` | manager | assign / unassign from the board |
| `GET /workers` | manager | workers with registration number and expiry |
| `GET /api/shifts` | signed in | shifts as JSON (a worker gets their own) |
| `POST /api/shifts/<id>/assign` | manager | body `{"worker_id": n}`; 200, 404 (not in your organisation) or 409 `{"error": "<reason>"}` |
| `POST /api/shifts/<id>/unassign` | manager | 200 or 409 |
| `GET /api/workers` | manager | workers as JSON |

Signed out, the API answers 401 and pages redirect to `/signin`; a worker gets 403 on manager routes.

## Commands

```bash
pip install -r requirements.txt && playwright install chromium   # once
python app.py                                  # http://127.0.0.1:8000 (instance/rota.db)
python -m pytest -q tests                      # all tests
python -m pytest -q tests/test_isolation.py    # tenant isolation only
python e2e/capture.py                          # e2e-results/NN-*.png and e2e-results/session.webm
```

See `CONTRIBUTING.md` for the rules every change follows.
