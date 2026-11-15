# Contributing to Rota

Rota holds two organisations' staffing data in one database. These rules keep their data apart,
keep the schema history honest and keep every change visible. They apply to people and to agents.

1. **Data access only through `data.py`.** Every query on tenant data goes through a `data.Scope`
   bound to the signed-in user's organisation (`auth.scope()`). Routes, rules and templates never
   run SQL. A new query is a new `Scope` method that filters on `org_id = self.org_id`.
2. **Business rules live in `rules.py`** (pure functions, no database or Flask) and are applied in
   `assignments.py`, so the pages and the JSON API enforce them the same way. A rule change comes
   with tests in `tests/test_rules.py` and, where it shows over HTTP, `tests/test_api.py`.
3. **Every user-visible change is demonstrated in `e2e/scenario.py`**, with an expectation per
   behaviour and a screenshot of each visible state, so the screenshots and the video show it.
4. **Schema changes only through a new migration file.** Add the next numbered file in
   `migrations/` (`003_<what>.sql`, …). Never edit a migration that has been applied: the runner
   (`db.py`) refuses to start when an applied file has changed. Update `seed.py` when the
   synthetic data needs the new column.
5. **Never weaken `tests/test_isolation.py`.** It walks every route in the app's URL map, so new
   routes are covered automatically. If a route cannot pass it, that is a design question for a
   human, not a test to edit.
6. **Never loosen an expectation** in `tests/` or `e2e/scenario.py` to make a change pass, unless
   the change deliberately alters the behaviour the expectation describes.
7. **Protected paths need a human.** `migrations/`, `db.py`, `auth.py`, `tests/test_isolation.py`,
   `e2e/capture.py`, `requirements.txt`, `.gitignore` and this file: a change to any of them is
   merged by a person, never automatically.

All data in this repository is synthetic. Do not add real names, registration numbers or
anything taken from a real organisation.
