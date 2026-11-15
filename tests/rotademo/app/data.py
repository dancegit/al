"""Data access, scoped to ONE organisation. The only module that runs SQL on tenant data.

Routes get a Scope for the signed-in user's organisation (auth.scope()) and call its methods.
Every query below filters on org_id = self.org_id, so a caller cannot read or change another
organisation's rows by passing an id: a foreign id simply is not found. Routes never build SQL.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Any

import rules

_SHIFT_COLUMNS = """
    s.id, s.ward_id, w.name AS ward_name, s.role_required, s.starts_at, s.ends_at,
    s.worker_id, k.name AS worker_name
"""
_SHIFT_FROM = """
    FROM shifts s
    JOIN wards w ON w.id = s.ward_id AND w.org_id = s.org_id
    LEFT JOIN workers k ON k.id = s.worker_id AND k.org_id = s.org_id
"""
_WORKER_COLUMNS = "id, name, role, employment, registration_number, registration_expires"


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def to_rule_worker(worker: dict[str, Any]) -> rules.Worker:
    return rules.Worker(id=worker["id"], name=worker["name"], role=worker["role"],
                        registration_expires=date.fromisoformat(worker["registration_expires"]))


def to_rule_shift(shift: dict[str, Any]) -> rules.Shift:
    return rules.Shift(id=shift["id"], role_required=shift["role_required"],
                       starts_at=datetime.fromisoformat(shift["starts_at"]),
                       ends_at=datetime.fromisoformat(shift["ends_at"]), worker_id=shift["worker_id"])


class Scope:
    """Everything one organisation may see and change."""

    def __init__(self, conn: sqlite3.Connection, org_id: int):
        self.conn = conn
        self.org_id = org_id

    # ---------------------------------------------------------------- reads

    def organisation(self) -> dict[str, Any] | None:
        return _row(self.conn.execute("SELECT id, name FROM organisations WHERE id = ?", (self.org_id,)).fetchone())

    def shifts(self, from_day: date | None = None) -> list[dict[str, Any]]:
        sql = f"SELECT {_SHIFT_COLUMNS} {_SHIFT_FROM} WHERE s.org_id = ?"
        params: list[Any] = [self.org_id]
        if from_day is not None:
            sql += " AND s.ends_at >= ?"
            params.append(from_day.isoformat())
        rows = self.conn.execute(sql + " ORDER BY s.starts_at, s.id", params).fetchall()
        return [dict(row) for row in rows]

    def shift(self, shift_id: int) -> dict[str, Any] | None:
        return _row(self.conn.execute(f"SELECT {_SHIFT_COLUMNS} {_SHIFT_FROM} WHERE s.org_id = ? AND s.id = ?",
                                      (self.org_id, shift_id)).fetchone())

    def shifts_of_worker(self, worker_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(f"SELECT {_SHIFT_COLUMNS} {_SHIFT_FROM} WHERE s.org_id = ? AND s.worker_id = ? "
                                 "ORDER BY s.starts_at, s.id", (self.org_id, worker_id)).fetchall()
        return [dict(row) for row in rows]

    def workers(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(f"SELECT {_WORKER_COLUMNS} FROM workers WHERE org_id = ? ORDER BY name",
                                 (self.org_id,)).fetchall()
        return [dict(row) for row in rows]

    def worker(self, worker_id: int) -> dict[str, Any] | None:
        return _row(self.conn.execute(f"SELECT {_WORKER_COLUMNS} FROM workers WHERE org_id = ? AND id = ?",
                                      (self.org_id, worker_id)).fetchone())

    def workers_with_role(self, role: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(f"SELECT {_WORKER_COLUMNS} FROM workers WHERE org_id = ? AND role = ? ORDER BY name",
                                 (self.org_id, role)).fetchall()
        return [dict(row) for row in rows]

    # ---------------------------------------------------------------- writes

    def set_worker(self, shift_id: int, worker_id: int | None) -> bool:
        """Assign (or with None, unassign) a shift of this organisation. A worker must also
        belong to this organisation. Returns False when nothing matched."""
        if worker_id is not None and self.worker(worker_id) is None:
            return False
        with self.conn:
            cursor = self.conn.execute("UPDATE shifts SET worker_id = ? WHERE org_id = ? AND id = ?",
                                       (worker_id, self.org_id, shift_id))
        return cursor.rowcount == 1


def user(conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    """A user by id, for sign-in: users are looked up before an organisation is known."""
    return _row(conn.execute("SELECT u.id, u.org_id, u.name, u.role, u.worker_id, o.name AS org_name "
                             "FROM users u JOIN organisations o ON o.id = u.org_id WHERE u.id = ?",
                             (user_id,)).fetchone())


def sign_in_choices(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The demo sign-in page lists every seeded user (demo-grade sign-in, no passwords)."""
    rows = conn.execute("SELECT u.id, u.name, u.role, o.name AS org_name, k.employment FROM users u "
                        "JOIN organisations o ON o.id = u.org_id LEFT JOIN workers k ON k.id = u.worker_id "
                        "ORDER BY u.id").fetchall()
    return [dict(row) for row in rows]
