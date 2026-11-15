"""The app's notion of "today". Injectable so tests, screenshots and videos are stable.

ROTA_TODAY=YYYY-MM-DD fixes the date (the tests and e2e/capture.py set 2026-03-02, the date the
seed data is written for). Without it the real date is used. Code never calls date.today()
directly: it asks clock.today().
"""
from __future__ import annotations

import os
from datetime import date


def today() -> date:
    fixed = os.environ.get("ROTA_TODAY", "").strip()
    return date.fromisoformat(fixed) if fixed else date.today()
