"""Tenant isolation: Northfield Hospital's manager must never see or change Riverside Care Group's data.

This test does not list the routes. It walks the app's URL map, so a route added later is checked
without anyone remembering to add it here. For every route and method, signed in as Northfield's
ward manager on a fresh database:

  - GET routes are called with Northfield's ids and with Riverside's ids in the path, plus
    Riverside's ids in the query string;
  - POST/PUT/PATCH/DELETE routes are called with Riverside's ids in the path ("foreign": must be
    refused with a 4xx) and with Northfield's ids in the path but Riverside's ids in the body.

After every request: the response contains no Riverside name or identifier (every distinctive text
value of every Riverside row, in every table that has an org_id column, found by inspecting the
schema), JSON contains no Riverside row id under an "id"/"*_id" key, every Riverside row is
unchanged, and no row references a row of another organisation.

Never weaken this file (CONTRIBUTING.md). A route that cannot honour it needs a human decision.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from conftest import NORTHFIELD_MANAGER, sign_in
from app import create_app

OWN_ORG, OTHER_ORG = 1, 101
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# The demo-grade sign-in lists every seeded user of both organisations by design, and signing in
# or out changes who you are, not tenant data. Nothing else is exempt.
EXEMPT_ENDPOINTS = {"static", "signin", "signin_post", "signout"}
BODY_KEYS = ("id", "shift_id", "worker_id", "ward_id", "user_id", "org_id", "organisation_id")


# ---------------------------------------------------------------- the schema, inspected

def tenant_tables(conn: sqlite3.Connection) -> list[str]:
    names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return [name for name in names if any(col[1] == "org_id" for col in conn.execute(f"PRAGMA table_info({name})"))]


def rows_of(conn: sqlite3.Connection, org_id: int) -> dict[str, list[tuple]]:
    snapshot = {"organisations": [tuple(row) for row in conn.execute("SELECT * FROM organisations WHERE id = ?", (org_id,))]}
    for table in tenant_tables(conn):
        snapshot[table] = [tuple(row) for row in conn.execute(f"SELECT * FROM {table} WHERE org_id = ? ORDER BY rowid", (org_id,))]
    return snapshot


def text_values(snapshot: dict[str, list[tuple]]) -> set[str]:
    return {value for rows in snapshot.values() for row in rows for value in row if isinstance(value, str)}


def foreign_markers(conn: sqlite3.Connection) -> set[str]:
    """Every text value of a Riverside row that no Northfield row has (names, ward names,
    registration numbers, ...). Shared values such as a role title are not identifying."""
    own, other = text_values(rows_of(conn, OWN_ORG)), text_values(rows_of(conn, OTHER_ORG))
    return {value for value in other - own if len(value.strip()) >= 4}


def foreign_ids(conn: sqlite3.Connection) -> set[int]:
    ids = {OTHER_ORG}
    for table in tenant_tables(conn):
        ids |= {row[0] for row in conn.execute(f"SELECT rowid FROM {table} WHERE org_id = ?", (OTHER_ORG,))}
    return ids


def first_id(conn: sqlite3.Connection, table: str, org_id: int) -> int:
    row = conn.execute(f"SELECT rowid FROM {table} WHERE org_id = ? ORDER BY rowid LIMIT 1", (org_id,)).fetchone()
    return row[0] if row else 999999


def cross_tenant_references(conn: sqlite3.Connection) -> list[str]:
    """Rows of one organisation that point at a row of another (e.g. a shift with a foreign worker)."""
    tenants = set(tenant_tables(conn))
    problems = []
    for table in tenants:
        for fk in conn.execute(f"PRAGMA foreign_key_list({table})"):
            target, column = fk[2], fk[3]
            if target not in tenants:
                continue
            bad = conn.execute(f"SELECT COUNT(*) FROM {table} a JOIN {target} b ON b.rowid = a.{column} "
                               f"WHERE a.org_id != b.org_id").fetchone()[0]
            if bad:
                problems.append(f"{bad} row(s) of {table}.{column} reference another organisation's {target}")
    return problems


# ---------------------------------------------------------------- the routes, enumerated

def _routes():
    with tempfile.TemporaryDirectory() as directory:
        application = create_app(Path(directory) / "enumerate.db")
        rules = [rule for rule in application.url_map.iter_rules() if rule.endpoint not in EXEMPT_ENDPOINTS]
    cases = []
    for rule in sorted(rules, key=lambda rule: rule.rule):
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            variants = ("own", "foreign") if rule.arguments else ("plain",)
            cases += [pytest.param(rule.rule, rule.endpoint, method, variant, id=f"{method} {rule.rule} [{variant}]")
                      for variant in variants]
    return cases


ROUTES = _routes()


def _id_for(conn: sqlite3.Connection, argument: str, org_id: int) -> int:
    tables = tenant_tables(conn)
    stem = argument[:-3] if argument.endswith("_id") else argument
    for table in (stem, stem + "s", stem + "es"):
        if table in tables:
            return first_id(conn, table, org_id)
    return first_id(conn, "shifts", org_id)


def _leaks(text: str, payload, markers: set[str], ids: set[int]) -> list[str]:
    found = [f"response contains {marker!r}" for marker in sorted(markers) if marker in text]

    def walk(value, key=""):
        if isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif isinstance(value, int) and not isinstance(value, bool) and (key == "id" or key.endswith("_id")) and value in ids:
            found.append(f"JSON {key}={value} is a row of the other organisation")

    if payload is not None:
        walk(payload)
    return found


def problems_for(app, rule: str, endpoint: str, method: str, variant: str) -> list[str]:
    """Call one route as Northfield's manager; return every isolation problem seen."""
    client = sign_in(app.test_client(), NORTHFIELD_MANAGER)
    conn = sqlite3.connect(app.config["DATABASE"])
    try:
        markers, ids, before = foreign_markers(conn), foreign_ids(conn), rows_of(conn, OTHER_ORG)
        url_rule = next(r for r in app.url_map.iter_rules() if r.rule == rule and r.endpoint == endpoint)
        org = OTHER_ORG if variant == "foreign" else OWN_ORG
        values = {argument: _id_for(conn, argument, org) for argument in url_rule.arguments}
        foreign_body = {key: _id_for(conn, key, OTHER_ORG) for key in BODY_KEYS}
    finally:
        conn.close()
    path = app.url_map.bind("localhost").build(endpoint, values, method=method)
    if method in WRITE_METHODS:
        if path.startswith("/api/"):
            response = client.open(path, method=method, json=foreign_body)
        else:
            response = client.open(path, method=method, data={key: str(value) for key, value in foreign_body.items()})
    else:
        response = client.open(path, method=method, query_string=foreign_body)
    text = response.get_data(as_text=True)
    try:
        payload = json.loads(text) if response.is_json else None
    except ValueError:
        payload = None
    problems = _leaks(text, payload, markers, ids)
    if method in WRITE_METHODS and variant == "foreign" and not 400 <= response.status_code < 500:
        problems.append(f"a write against the other organisation's record answered {response.status_code}, not 4xx")
    conn = sqlite3.connect(app.config["DATABASE"])
    try:
        if rows_of(conn, OTHER_ORG) != before:
            problems.append("the other organisation's rows changed")
        problems += cross_tenant_references(conn)
    finally:
        conn.close()
    return [f"{method} {path}: {problem}" for problem in problems]


# ---------------------------------------------------------------- the tests

@pytest.mark.parametrize("rule, endpoint, method, variant", ROUTES)
def test_route_does_not_cross_organisations(tmp_path, rule, endpoint, method, variant):
    app = create_app(tmp_path / "rota.db")
    app.config.update(TESTING=True)
    assert problems_for(app, rule, endpoint, method, variant) == []


def test_every_route_is_enumerated():
    checked = {param.values[0] for param in ROUTES}
    for rule in ("/shifts", "/workers", "/api/shifts", "/api/workers", "/shifts/<int:shift_id>/assign",
                 "/api/shifts/<int:shift_id>/assign"):
        assert rule in checked


def test_the_markers_cover_the_other_organisation(tmp_path):
    app = create_app(tmp_path / "rota.db")
    conn = sqlite3.connect(app.config["DATABASE"])
    try:
        markers = foreign_markers(conn)
    finally:
        conn.close()
    for value in ("Riverside Care Group", "Willow Ward", "Elena Novak", "RC-RN-8802", "Tom Becker"):
        assert value in markers
    assert "Registered nurse" not in markers   # shared by both organisations: not identifying


def test_the_check_catches_a_leaky_route(tmp_path):
    """Self-test of the harness: a route that skips the organisation scope is caught."""
    app = create_app(tmp_path / "rota.db")
    app.config.update(TESTING=True)

    def leaky_list():
        return {"workers": [dict(row) for row in app.db().execute("SELECT id, name FROM workers")]}

    def leaky_take(shift_id: int):
        with app.db() as conn:
            conn.execute("UPDATE shifts SET worker_id = 1 WHERE id = ?", (shift_id,))
        return {"ok": True}

    app.add_url_rule("/api/leaky-workers", "leaky_list", leaky_list)
    app.add_url_rule("/api/leaky-take/<int:shift_id>", "leaky_take", leaky_take, methods=["POST"])
    read = problems_for(app, "/api/leaky-workers", "leaky_list", "GET", "plain")
    write = problems_for(app, "/api/leaky-take/<int:shift_id>", "leaky_take", "POST", "foreign")
    assert any("Elena Novak" in problem for problem in read)
    assert any("not 4xx" in problem for problem in write)
    assert any("rows changed" in problem for problem in write)
