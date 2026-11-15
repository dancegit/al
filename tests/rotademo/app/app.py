"""Rota: staffing and shift scheduling for hospital wards, for two (fictional) organisations.

    python app.py            serve on 127.0.0.1:8000 (PORT / HOST override)

ROTA_DB is the SQLite file (default instance/rota.db); it is created, migrated (migrations/) and
seeded with synthetic data (seed.py) at start-up. ROTA_TODAY fixes the date (clock.py).
Routes handle HTTP only: data goes through auth.scope() (data.py), rules through assignments.py.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, url_for

import assignments
import auth
import clock
import data
import db
import seed

ROOT = Path(__file__).resolve().parent


def shift_json(shift: dict[str, Any]) -> dict[str, Any]:
    return {"id": shift["id"], "ward_id": shift["ward_id"], "ward": shift["ward_name"],
            "role_required": shift["role_required"], "starts_at": shift["starts_at"], "ends_at": shift["ends_at"],
            "status": "open" if shift["worker_id"] is None else "filled",
            "worker_id": shift["worker_id"], "worker": shift["worker_name"]}


def worker_json(worker: dict[str, Any]) -> dict[str, Any]:
    return {key: worker[key] for key in
            ("id", "name", "role", "employment", "registration_number", "registration_expires")}


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def create_app(db_path: str | Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(SECRET_KEY=os.environ.get("ROTA_SECRET_KEY", "rota-demo-not-a-secret"),
                      DATABASE=str(db_path or os.environ.get("ROTA_DB") or ROOT / "instance" / "rota.db"))
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(app.config["DATABASE"])
    try:
        db.migrate(conn)
        if seed.is_empty(conn):
            seed.seed(conn)
    finally:
        conn.close()

    def get_db() -> sqlite3.Connection:
        if "db" not in g:
            g.db = db.connect(app.config["DATABASE"])
        return g.db

    app.db = get_db

    @app.teardown_appcontext
    def close_db(_exc: BaseException | None) -> None:
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.context_processor
    def inject_user() -> dict[str, Any]:
        return {"user": auth.current_user(), "today": clock.today()}

    @app.template_filter("day")
    def day_filter(value: str) -> str:
        moment = datetime.fromisoformat(value)
        return f"{moment:%a} {moment.day} {moment:%b}"

    @app.template_filter("hours")
    def hours_filter(shift: dict[str, Any]) -> str:
        start, end = datetime.fromisoformat(shift["starts_at"]), datetime.fromisoformat(shift["ends_at"])
        return f"{start:%H:%M}–{end:%H:%M}" + (" (next day)" if end.date() > start.date() else "")

    # ---------------------------------------------------------------- sign-in (demo-grade)

    @app.get("/")
    def home():
        return redirect(url_for("shifts" if auth.current_user() else "signin"))

    @app.get("/signin")
    def signin():
        return render_template("signin.html", choices=data.sign_in_choices(get_db()))

    @app.post("/signin")
    def signin_post():
        user_id = _int(request.form.get("user_id"))
        user = data.user(get_db(), user_id) if user_id is not None else None
        if user is None:
            flash("Choose a user to sign in as.", "error")
            return redirect(url_for("signin"))
        auth.sign_in(user)
        return redirect(url_for("shifts"))

    @app.post("/signout")
    def signout():
        auth.sign_out()
        return redirect(url_for("signin"))

    # ---------------------------------------------------------------- pages

    @app.get("/shifts")
    @auth.signed_in
    def shifts():
        scope, user = auth.scope(), auth.current_user()
        if user["role"] != auth.MANAGER:
            return render_template("my_shifts.html", shifts=scope.shifts_of_worker(user["worker_id"]))
        rows = scope.shifts(from_day=clock.today())
        options = {shift["id"]: assignments.candidates(scope, shift) for shift in rows if shift["worker_id"] is None}
        return render_template("shifts.html", shifts=rows, options=options, organisation=scope.organisation())

    @app.post("/shifts/<int:shift_id>/assign")
    @auth.manager_only
    def assign(shift_id: int):
        worker_id = _int(request.form.get("worker_id"))
        outcome = assignments.assign(auth.scope(), shift_id, worker_id if worker_id is not None else -1, clock.today())
        if outcome.status == 404 and outcome.reason == assignments.SHIFT_NOT_FOUND:
            abort(404)
        if outcome.ok:
            flash(f"Assigned {outcome.worker['name']} to {outcome.shift['ward_name']}, "
                  f"{day_filter(outcome.shift['starts_at'])} {hours_filter(outcome.shift)}.", "success")
        else:
            who = outcome.worker["name"] if outcome.worker else "that worker"
            flash(f"Cannot assign {who}: {outcome.reason}.", "error")
        return redirect(url_for("shifts"))

    @app.post("/shifts/<int:shift_id>/unassign")
    @auth.manager_only
    def unassign(shift_id: int):
        outcome = assignments.unassign(auth.scope(), shift_id)
        if outcome.status == 404:
            abort(404)
        flash("Shift is open again." if outcome.ok else f"Cannot unassign: {outcome.reason}.",
              "success" if outcome.ok else "error")
        return redirect(url_for("shifts"))

    @app.get("/workers")
    @auth.manager_only
    def workers():
        scope = auth.scope()
        return render_template("workers.html", workers=scope.workers(), organisation=scope.organisation())

    # ---------------------------------------------------------------- JSON API (mirrors the UI)

    @app.get("/api/shifts")
    @auth.signed_in
    def api_shifts():
        scope, user = auth.scope(), auth.current_user()
        rows = scope.shifts() if user["role"] == auth.MANAGER else scope.shifts_of_worker(user["worker_id"])
        return jsonify(shifts=[shift_json(shift) for shift in rows])

    @app.post("/api/shifts/<int:shift_id>/assign")
    @auth.manager_only
    def api_assign(shift_id: int):
        body = request.get_json(silent=True) or {}
        worker_id = _int(body.get("worker_id"))
        if worker_id is None:
            return jsonify(error="worker_id (integer) is required"), 400
        outcome = assignments.assign(auth.scope(), shift_id, worker_id, clock.today())
        if not outcome.ok:
            return jsonify(error=outcome.reason), outcome.status
        return jsonify(shift=shift_json(outcome.shift))

    @app.post("/api/shifts/<int:shift_id>/unassign")
    @auth.manager_only
    def api_unassign(shift_id: int):
        outcome = assignments.unassign(auth.scope(), shift_id)
        if not outcome.ok:
            return jsonify(error=outcome.reason), outcome.status
        return jsonify(shift=shift_json(outcome.shift))

    @app.get("/api/workers")
    @auth.manager_only
    def api_workers():
        return jsonify(workers=[worker_json(worker) for worker in auth.scope().workers()])

    return app


if __name__ == "__main__":
    create_app().run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
