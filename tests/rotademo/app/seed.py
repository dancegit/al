"""Deterministic SYNTHETIC demo data: two fictional organisations, their wards, workers and shifts.

Every name, number and date here is invented for the demo. The data is written for a fixed
"today" of 2026-03-02 (see clock.py), so pages and screenshots are stable.

Ids are disjoint on purpose: Northfield Hospital's rows use ids below 100, Riverside Care
Group's rows ids from 101 up, so a leak of an id across organisations is easy to spot.

    python seed.py rota.db        create (migrate) and seed a database file
"""
from __future__ import annotations

import sqlite3
import sys

ORGANISATIONS = [
    (1, "Northfield Hospital"),
    (101, "Riverside Care Group"),
]

WARDS = [
    (1, 1, "Ash Ward"),
    (2, 1, "Birch Ward"),
    (101, 101, "Willow Ward"),
    (102, 101, "Cedar Ward"),
]

RN, HCA = "Registered nurse", "Healthcare assistant"

# (id, org_id, name, role, employment, registration_number, registration_expires)
WORKERS = [
    (1, 1, "Amara Jones", RN, "staff", "NF-RN-4471", "2027-01-31"),
    (2, 1, "Ben Clarke", HCA, "staff", "NF-HC-2208", "2026-09-30"),
    (3, 1, "Chloe Martin", RN, "staff", "NF-RN-3915", "2026-02-14"),   # expired before "today"
    (4, 1, "Daniel Reyes", RN, "staff", "NF-RN-5150", "2026-12-31"),
    (5, 1, "Sam Okafor", RN, "agency", "NF-RN-6034", "2026-08-31"),
    (101, 101, "Elena Novak", RN, "staff", "RC-RN-8802", "2026-11-30"),
    (102, 101, "Farah Haddad", HCA, "staff", "RC-HC-7316", "2027-04-30"),
    (103, 101, "George Mensah", RN, "staff", "RC-RN-9047", "2026-01-10"),  # expired
    (104, 101, "Hannah Lee", HCA, "agency", "RC-HC-6621", "2026-10-31"),
]

# (id, org_id, name, role, worker_id)
USERS = [
    (1, 1, "Priya Shah", "manager", None),
    (2, 1, "Sam Okafor", "worker", 5),
    (101, 101, "Tom Becker", "manager", None),
]

# (id, org_id, ward_id, role_required, starts_at, ends_at, worker_id)
SHIFTS = [
    (1, 1, 1, RN, "2026-03-02T07:30", "2026-03-02T19:30", 4),
    (2, 1, 1, RN, "2026-03-02T19:30", "2026-03-03T07:30", 1),
    (3, 1, 1, RN, "2026-03-03T07:30", "2026-03-03T19:30", None),
    (4, 1, 2, HCA, "2026-03-03T07:00", "2026-03-03T15:00", 2),
    (5, 1, 2, RN, "2026-03-03T14:00", "2026-03-03T22:00", None),
    (6, 1, 2, RN, "2026-03-04T07:00", "2026-03-04T15:00", 5),
    (7, 1, 1, HCA, "2026-03-04T07:30", "2026-03-04T19:30", None),
    (8, 1, 1, RN, "2026-03-04T19:30", "2026-03-05T07:30", None),
    (101, 101, 101, RN, "2026-03-02T08:00", "2026-03-02T20:00", 101),
    (102, 101, 101, RN, "2026-03-03T08:00", "2026-03-03T20:00", None),
    (103, 101, 102, HCA, "2026-03-03T07:00", "2026-03-03T15:00", 102),
    (104, 101, 102, HCA, "2026-03-04T15:00", "2026-03-04T23:00", None),
    (105, 101, 101, RN, "2026-03-04T20:00", "2026-03-05T08:00", None),
]


def seed(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executemany("INSERT INTO organisations (id, name) VALUES (?, ?)", ORGANISATIONS)
        conn.executemany("INSERT INTO wards (id, org_id, name) VALUES (?, ?, ?)", WARDS)
        conn.executemany("INSERT INTO workers (id, org_id, name, role, employment, registration_number, "
                         "registration_expires) VALUES (?, ?, ?, ?, ?, ?, ?)", WORKERS)
        conn.executemany("INSERT INTO users (id, org_id, name, role, worker_id) VALUES (?, ?, ?, ?, ?)", USERS)
        conn.executemany("INSERT INTO shifts (id, org_id, ward_id, role_required, starts_at, ends_at, worker_id) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", SHIFTS)


def is_empty(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT COUNT(*) FROM organisations").fetchone()[0] == 0


if __name__ == "__main__":
    import db

    connection = db.connect(sys.argv[1] if len(sys.argv) > 1 else "rota.db")
    db.migrate(connection)
    if is_empty(connection):
        seed(connection)
    print("seeded" if connection.execute("SELECT COUNT(*) FROM shifts").fetchone()[0] else "empty")
