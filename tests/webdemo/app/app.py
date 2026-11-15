"""Notes: a one-page Flask app. Notes live in memory, one list per app instance.

    python app.py            serve on 127.0.0.1:8000 (PORT / HOST override)
"""
from __future__ import annotations

import os

from flask import Flask, redirect, render_template, request, url_for

SEED_NOTES = [
    "Water the plants",
    "Call the bike shop about the brakes",
    "Read chapter 3 of the Racket guide",
]
MAX_NOTE_LENGTH = 200


def create_app() -> Flask:
    app = Flask(__name__)
    notes: list[str] = list(SEED_NOTES)

    @app.get("/")
    def index():
        return render_template("index.html", notes=notes)

    @app.post("/notes")
    def add_note():
        text = (request.form.get("text") or "").strip()[:MAX_NOTE_LENGTH]
        if text:
            notes.append(text)
        return redirect(url_for("index"))

    return app


if __name__ == "__main__":
    create_app().run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
