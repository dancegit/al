"""POST /api/settle behind every write guard, and the counts the snapshot carries, on the scenario of
settle_fixtures (real dashboard server, no browser)."""
from __future__ import annotations

import collections
import json
import shutil
import time
from pathlib import Path

import pytest

import settle_fixtures as sf
from conftest import start_server, stop_server
from test_start import headers, post

RUN = sf.OPEN[0]


@pytest.fixture(scope="module")
def scenario_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("settle-http") / "s"
    sf.build_scenario(root)
    return root


@pytest.fixture()
def layer(tmp_path: Path, scenario_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("AL_APP_ROOT", raising=False)
    root = tmp_path / "s"
    shutil.copytree(scenario_root, root, symlinks=True)
    return root / "orchard.al"


def serve(layer: Path, *flags: str):
    return start_server(["--layer", str(layer), *flags])


def settle_body(**changes) -> dict:
    return {"layer": "orchard", "run": RUN, "as": "abandoned", "note": "the orchard plan changed", "who": "Ada",
            **changes}


def snapshot_when(running, predicate, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    data: dict = {}
    while time.monotonic() < deadline:
        data = running.json("/api/snapshot")
        if predicate(data):
            return data
        time.sleep(0.2)
    raise AssertionError(f"the snapshot never matched: {data.get('layers')}")


def counts(data: dict) -> dict[str, int]:
    return dict(collections.Counter(r.get("bucket") for r in data["runs"] if r["layer"] == "orchard"))


def settled_on_disk(layer: Path) -> bool:
    return (layer / "agents" / RUN / "settlement.json").exists()


GUARDS = [
    ("no X-AL-Dashboard", [], lambda r: post(r, settle_body(), "/api/settle", X_AL_Dashboard=None), 403),
    ("cross-origin", [], lambda r: post(r, settle_body(), "/api/settle", Origin="http://evil.example"), 403),
    ("token in the URL", ["--token", "t0k"], lambda r: post(r, settle_body(), "/api/settle?token=t0k"), 401),
    ("not JSON", [], lambda r: post(r, b"layer=orchard", "/api/settle",
                                    Content_Type="application/x-www-form-urlencoded"), 415),
]


@pytest.mark.parametrize("why, flags, call, code", GUARDS, ids=[g[0] for g in GUARDS])
def test_settle_needs_every_write_guard(layer, why, flags, call, code):
    running = serve(layer, *flags)
    try:
        status, data = call(running)
    finally:
        stop_server(running)
    assert status == code, (why, data)
    assert not settled_on_disk(layer)


def test_settle_refused_read_only(layer):
    running = serve(layer, "--read-only")
    try:
        status, data = post(running, settle_body(), "/api/settle")
    finally:
        stop_server(running)
    assert status == 403 and "--read-only" in data["error"]
    assert not settled_on_disk(layer)


@pytest.mark.parametrize("change, code", [
    ({"as": "merged"}, 400),
    ({"by": "not-a-commit"}, 400),
    ({"by": "deadbeefdeadbeef"}, 400),
    ({"note": "x" * 501}, 400),
    ({"who": "a\nb"}, 400),
    ({"extra": 1}, 400),
    ({"layer": "elsewhere"}, 404),
    ({"run": "0badc0de"}, 404),
    ({"run": "../etc"}, 404),
])
def test_settle_validates_body(layer, change, code):
    running = serve(layer)
    try:
        status, data = post(running, settle_body(**change), "/api/settle")
    finally:
        stop_server(running)
    assert status == code, (change, data)
    assert data.get("error")
    assert not settled_on_disk(layer)


def test_settle_running_run_is_409(layer):
    running = serve(layer)
    try:
        status, data = post(running, settle_body(run=sf.RESTARTS[0]), "/api/settle")
    finally:
        stop_server(running)
    assert status == 409 and "running" in data["error"]
    assert not (layer / "agents" / sf.RESTARTS[0] / "settlement.json").exists()


def test_settle_writes_record_and_snapshot_moves_run_to_settled(layer):
    running = serve(layer)
    try:
        before = snapshot_when(running, lambda d: d["layers"] and d["layers"][0].get("needs_you") == 6)
        assert next(r for r in before["runs"] if r["id"] == RUN)["bucket"] == "needs-you"
        status, data = post(running, settle_body(), "/api/settle")
        assert status == 200, data
        assert data["status"] == "settled" and data["record"]["via"] == "dashboard"
        record = json.loads((layer / "agents" / RUN / "settlement.json").read_text())
        assert record == data["record"]
        assert (record["as"], record["who"], record["note"]) == ("abandoned", "Ada", "the orchard plan changed")
        after = snapshot_when(running, lambda d: d["layers"][0].get("needs_you") == 5)
        row = next(r for r in after["runs"] if r["id"] == RUN)
        assert row["bucket"] == "settled" and row["settled"]["as"] == "abandoned"
        assert row["reason"] == "abandoned by Ada: the orchard plan changed"
    finally:
        stop_server(running)


def test_get_settle_is_405(layer):
    running = serve(layer)
    try:
        assert running.get("/api/settle")[0] == 405
        assert running.get("/api/settle", headers(running), method="PUT", body=b"{}")[0] == 405
    finally:
        stop_server(running)


def test_snapshot_counts_on_the_scenario(layer):
    running = serve(layer)
    try:
        data = snapshot_when(running, lambda d: d["layers"] and d["layers"][0].get("needs_you") == 6
                             and not any(r.get("landing", {}) and r["landing"].get("state") == "checking"
                                         for r in d["runs"]))
    finally:
        stop_server(running)
    assert data["layers"][0]["needs_you"] == 6
    assert counts(data) == {"needs-you": 6, "running": 5, "landed": 8, "settled": 7, "done": 1}
    assert len(data["runs"]) == 27
