import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import MAX_NOTE_LENGTH, SEED_NOTES, create_app  # noqa: E402


@pytest.fixture
def client():
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


def test_index_lists_seed_notes(client):
    response = client.get("/")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    for note in SEED_NOTES:
        assert note in page
    assert f"{len(SEED_NOTES)} notes" in page


def test_index_links_the_stylesheet(client):
    page = client.get("/").get_data(as_text=True)
    assert "/static/style.css" in page
    assert client.get("/static/style.css").status_code == 200


def test_add_note_appends_and_redirects(client):
    response = client.post("/notes", data={"text": "  Buy oat milk  "})
    assert response.status_code == 302
    page = client.get("/").get_data(as_text=True)
    assert "Buy oat milk" in page
    assert f"{len(SEED_NOTES) + 1} notes" in page


def test_empty_note_is_ignored(client):
    client.post("/notes", data={"text": "   "})
    page = client.get("/").get_data(as_text=True)
    assert f"{len(SEED_NOTES)} notes" in page


def test_long_note_is_truncated(client):
    client.post("/notes", data={"text": "x" * (MAX_NOTE_LENGTH + 50)})
    page = client.get("/").get_data(as_text=True)
    assert "x" * MAX_NOTE_LENGTH in page
    assert "x" * (MAX_NOTE_LENGTH + 1) not in page


def test_notes_are_html_escaped(client):
    client.post("/notes", data={"text": "<b>bold</b>"})
    page = client.get("/").get_data(as_text=True)
    assert "&lt;b&gt;bold&lt;/b&gt;" in page


def test_apps_do_not_share_notes():
    first, second = create_app().test_client(), create_app().test_client()
    first.post("/notes", data={"text": "only in the first app"})
    assert "only in the first app" not in second.get("/").get_data(as_text=True)
