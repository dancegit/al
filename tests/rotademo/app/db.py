"""SQLite connection and the migration runner.

The schema is built ONLY by the numbered files in migrations/ (NNN_description.sql), applied in
order. Each applied file is recorded in schema_migrations with a checksum; a recorded migration
whose file has changed since it was applied stops the app, because an applied migration must never
be edited: add a new numbered file instead.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    pass


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migration_files(directory: Path = MIGRATIONS) -> list[Path]:
    files = sorted(directory.glob("*.sql"))
    for path in files:
        if not _NAME.match(path.name):
            raise MigrationError(f"migration file name must look like 003_add_thing.sql: {path.name}")
    versions = [path.name[:3] for path in files]
    if len(versions) != len(set(versions)):
        raise MigrationError(f"two migration files share a version number: {versions}")
    return files


def migrate(conn: sqlite3.Connection, directory: Path = MIGRATIONS) -> list[str]:
    """Apply every migration not yet recorded; returns the versions applied now."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                 "version TEXT PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL)")
    applied = {row["version"]: row for row in conn.execute("SELECT * FROM schema_migrations")}
    done = []
    for path in migration_files(directory):
        version, sql = path.name[:3], path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        if version in applied:
            if applied[version]["checksum"] != checksum:
                raise MigrationError(f"applied migration {path.name} was edited; add a new migration instead")
            continue
        with conn:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations (version, name, checksum) VALUES (?, ?, ?)",
                         (version, path.name, checksum))
        done.append(version)
    return done
