-- 001: organisations, users, wards, workers and shifts. Every tenant-owned table carries org_id.
CREATE TABLE organisations (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE wards (
    id     INTEGER PRIMARY KEY,
    org_id INTEGER NOT NULL REFERENCES organisations(id),
    name   TEXT NOT NULL
);

CREATE TABLE workers (
    id                   INTEGER PRIMARY KEY,
    org_id               INTEGER NOT NULL REFERENCES organisations(id),
    name                 TEXT NOT NULL,
    role                 TEXT NOT NULL,             -- 'Registered nurse' | 'Healthcare assistant'
    employment           TEXT NOT NULL DEFAULT 'staff' CHECK (employment IN ('staff', 'agency')),
    registration_number  TEXT NOT NULL UNIQUE,
    registration_expires TEXT NOT NULL              -- ISO date; valid up to and including this day
);

CREATE TABLE users (
    id        INTEGER PRIMARY KEY,
    org_id    INTEGER NOT NULL REFERENCES organisations(id),
    name      TEXT NOT NULL,
    role      TEXT NOT NULL CHECK (role IN ('manager', 'worker')),
    worker_id INTEGER REFERENCES workers(id)        -- set for role 'worker'
);

CREATE TABLE shifts (
    id            INTEGER PRIMARY KEY,
    org_id        INTEGER NOT NULL REFERENCES organisations(id),
    ward_id       INTEGER NOT NULL REFERENCES wards(id),
    role_required TEXT NOT NULL,
    starts_at     TEXT NOT NULL,                    -- ISO 'YYYY-MM-DDTHH:MM', local ward time
    ends_at       TEXT NOT NULL,
    worker_id     INTEGER REFERENCES workers(id)    -- NULL = open
);
