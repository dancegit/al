"""Shared fixtures: a fresh, migrated, seeded database per test and a fixed "today"."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["ROTA_TODAY"] = "2026-03-02"   # the date the seed data is written for

from app import create_app  # noqa: E402

NORTHFIELD_MANAGER, AGENCY_WORKER, RIVERSIDE_MANAGER = 1, 2, 101


@pytest.fixture
def app(tmp_path):
    application = create_app(tmp_path / "rota.db")
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def sign_in(client, user_id):
    response = client.post("/signin", data={"user_id": user_id})
    assert response.status_code == 302, response.get_data(as_text=True)
    return client


@pytest.fixture
def manager(client):
    """Signed in as Priya Shah, ward manager at Northfield Hospital."""
    return sign_in(client, NORTHFIELD_MANAGER)


@pytest.fixture
def worker(client):
    """Signed in as Sam Okafor, agency nurse working Northfield shifts (role worker)."""
    return sign_in(client, AGENCY_WORKER)
