"""The Testing writes on a real server: POST /api/testing (the layer's local level, like the Models
panel's setting), POST /api/batch ("Run the full suites now", through the layer's own
adws/verify_batch.py) and POST /api/run-testing ("Full before it ships": agents/<id>/testing-request.json),
each behind every write guard; and the start form's testing choice reaching the launcher as --testing."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Iterator

import pytest

import dashboard as d
import testing_fixtures as tf
from conftest import Running, start_server, stop_server
from test_model_settings import post


@pytest.fixture()
def site(tmp_path: Path) -> dict:
    return tf.build_testing_site(tmp_path / "site")


@pytest.fixture()
def server(site) -> Iterator[Running]:
    running = start_server(["--layer", str(site["layer"])])
    try:
        yield running
    finally:
        stop_server(running)


def setting(site: dict) -> Path:
    return site["layer"] / d.TESTING_FILE


def panel(running: Running) -> dict:
    return running.json("/api/snapshot")["layers"][0]["testing"]


def test_set_and_reset_testing_level(server, site):
    status, data = post(server, {"layer": tf.NAME, "level": "heavy"}, path="/api/testing")
    assert status == 200 and data["ok"] is True
    assert json.loads(setting(site).read_text()) == {"version": 1, "level": "heavy"}
    assert (data["testing"]["level"], data["testing"]["source"]) == ("heavy", "local setting")
    assert (panel(server)["level"], panel(server)["source"]) == ("heavy", "local setting")
    assert not list(setting(site).parent.glob(".testing.*"))
    status, data = post(server, {"layer": tf.NAME, "reset": True}, path="/api/testing")
    assert status == 200 and not setting(site).exists()
    assert (data["testing"]["level"], data["testing"]["source"]) == ("standard", "layer file")


def test_refused_level_409(tmp_path):
    trunk = tf.build_testing_site(tmp_path / "trunk", stages=False, batch=False, runs=False)
    running = start_server(["--layer", str(trunk["layer"])])
    try:
        status, data = post(running, {"layer": tf.NAME, "level": "standard"}, path="/api/testing")
        assert status == 409 and "no batch verification is declared" in data["error"]
        assert not setting(trunk).exists()
        assert post(running, {"layer": tf.NAME, "level": "heavy"}, path="/api/testing")[0] == 200
    finally:
        stop_server(running)


@pytest.mark.parametrize("body", [{"layer": tf.NAME, "level": "medium"}, {"layer": tf.NAME, "level": "full"},
                                  {"layer": tf.NAME}, {"layer": tf.NAME, "level": 3},
                                  {"layer": tf.NAME, "level": "heavy", "extra": 1}, [1]])
def test_bad_level_400(server, site, body):
    assert post(server, body, path="/api/testing")[0] == 400
    assert not setting(site).exists()


def test_unknown_layer_404(server):
    assert post(server, {"layer": "nope", "level": "heavy"}, path="/api/testing")[0] == 404
    assert post(server, {"layer": "nope"}, path="/api/batch")[0] == 404
    assert post(server, {"layer": "nope", "adw_id": tf.RUNNING, "depth": "full"}, path="/api/run-testing")[0] == 404
    assert post(server, {"layer": tf.NAME, "adw_id": "0badc0de", "depth": "full"}, path="/api/run-testing")[0] == 404


def test_read_only_and_origin_and_token_guard(site):
    paths = (("/api/testing", {"layer": tf.NAME, "level": "heavy"}), ("/api/batch", {"layer": tf.NAME}),
             ("/api/run-testing", {"layer": tf.NAME, "adw_id": tf.RUNNING, "depth": "full"}))
    running = start_server(["--layer", str(site["layer"]), "--read-only"])
    try:
        for path, body in paths:
            status, data = post(running, body, path=path)
            assert status == 403 and data["error"] == "the dashboard was started with --read-only"
            assert running.get(path)[0] == 405                   # GET is refused like the other writing paths
        assert panel(running)["writable"] is False and panel(running)["batch"]["enabled"] is False
    finally:
        stop_server(running)
    running = start_server(["--layer", str(site["layer"]), "--token", "t0k"])
    try:
        for path, body in paths:
            assert post(running, body, path=path)[0] == 401
            assert post(running, body, path=f"{path}?token=t0k")[0] == 401
            assert post(running, body, path=path, Authorization="Bearer t0k", Origin="http://evil.example")[0] == 403
            assert post(running, body, path=path, Authorization="Bearer t0k", X_AL_Dashboard=None)[0] == 403
            assert post(running, body, path=path, Authorization="Bearer t0k",
                        Content_Type="application/x-www-form-urlencoded")[0] == 415
            assert post(running, {**body, "pad": "x" * 5000} if isinstance(body, dict) else body, path=path,
                        Authorization="Bearer t0k")[0] == 413
        assert not setting(site).exists() and not (site["layer"] / tf.FIX_FILE).exists()
        assert not (site["layer"] / "agents" / tf.RUNNING / "testing-request.json").exists()
        assert post(running, paths[0][1], path="/api/testing", Authorization="Bearer t0k")[0] == 200
    finally:
        stop_server(running)


def calls(site: dict) -> list[list[str]]:
    path = site["layer"] / tf.FIX_FILE
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_batch_start_202_and_no_start_refusal(server, site):
    status, data = post(server, {"layer": tf.NAME}, path="/api/batch")
    assert status == 202 and data == {"status": "started", "run_id": "7e5700b1", "url": f"#/run/{tf.NAME}/7e5700b1"}
    status, data = post(server, {"layer": tf.NAME, "stage": "sprint"}, path="/api/batch")
    assert status == 202
    assert calls(site) == [["run", "--detach", "--json"], ["run", "--detach", "--json", "--stage", "sprint"]]
    assert post(server, {"layer": tf.NAME, "stage": "nope"}, path="/api/batch")[0] == 400        # not a stage
    assert post(server, {"layer": tf.NAME, "stage": "-x"}, path="/api/batch")[0] == 400
    assert post(server, {"layer": tf.NAME, "branch": "main"}, path="/api/batch")[0] == 400       # BATCH_KEYS only
    assert len(calls(site)) == 2
    (site["layer"] / "adws" / "verify_batch.py").unlink()
    status, data = post(server, {"layer": tf.NAME}, path="/api/batch")
    assert status == 409 and "run `al gen" in data["error"]
    for flags, error in ((["--no-start"], "the dashboard was started with --no-start"),
                         (["--in-container"], "the dashboard runs in a container")):
        running = start_server(["--layer", str(site["layer"]), *flags])
        try:
            status, data = post(running, {"layer": tf.NAME}, path="/api/batch")
            assert status == 403 and data["error"].startswith(error)
        finally:
            stop_server(running)
    assert len(calls(site)) == 2


def _row(running: Running, run: str, timeout: float = 30.0, bucket: str = "") -> dict:
    """The snapshot's row of `run` (once it is in `bucket`, when one is named)."""
    deadline = time.monotonic() + timeout
    row: dict = {}
    while time.monotonic() < deadline:
        rows = [r for r in running.json("/api/snapshot")["runs"] if r["layer"] == tf.NAME and r["id"] == run]
        row = rows[0] if rows else {}
        if row and (not bucket or row.get("bucket") == bucket):
            return row
        time.sleep(0.2)
    raise AssertionError(f"run {run} never reached {bucket or 'the snapshot'}: {row.get('bucket')}")


def test_failed_batch_run_is_not_started_again_and_can_be_settled(server, site):
    """A failed batch run (kit workflow verify-batch) is not a request: Start again is refused with the
    entry point's name and starts nothing; settling it works and takes it out of Needs you."""
    row = _row(server, tf.BATCH, bucket="needs-you")
    assert row["next"] == "run the full suites again: `al batch LAYER run --stage sprint`, or `al settle`"
    assert row["acts_on"] == {"kind": "batch", "branch": tf.SPRINT, "stage": "sprint"}
    assert row["assign"]["script"] is False and row.get("group") is None
    agents = site["layer"] / "agents"
    before = sorted(p.name for p in agents.iterdir())
    status, data = post(server, {"layer": tf.NAME, "again": tf.BATCH, "request_key": f"again-{tf.BATCH}"}, path="/api/start")
    assert status == 400, data
    assert "al batch LAYER run --stage sprint" in data["error"] and "batch run (kit workflow verify-batch)" in data["error"]
    assert "no longer declares" not in data["error"]
    assert sorted(p.name for p in agents.iterdir()) == before, "nothing was started"
    assert not (agents / tf.BATCH / "assign.json").exists() and calls(site) == []
    status, data = post(server, {"layer": tf.NAME, "run": tf.BATCH, "as": "abandoned", "by": "", "note": "",
                                 "who": "Ada Example"}, path="/api/settle")
    assert status == 200 and data["record"]["as"] == "abandoned", data
    assert _row(server, tf.BATCH, bucket="settled")["next"] == ""


def test_batch_refusal_of_the_layer_is_409(server, site):
    ir = json.loads((site["layer"] / "agentic-layer.json").read_text())
    ir["stages"].append({**ir["stages"][0], "name": "busy", "branch": "busy"})
    (site["layer"] / "agentic-layer.json").write_text(json.dumps(ir))
    server.server.model.discover()
    status, data = post(server, {"layer": tf.NAME, "stage": "busy"}, path="/api/batch")
    assert status == 409 and data["error"] == "already-running: a batch of this branch is running"


def test_run_testing_request_and_ended_run_409(server, site):
    request = site["layer"] / "agents" / tf.RUNNING / "testing-request.json"
    status, data = post(server, {"layer": tf.NAME, "adw_id": tf.RUNNING, "depth": "full"}, path="/api/run-testing")
    assert status == 200 and data["status"] == "requested"
    written = json.loads(request.read_text())
    assert (written["depth"], written["by"]) == ("full", "dashboard") and written["at"]
    assert data["request"] == written
    assert request.stat().st_mode & 0o777 == 0o600
    detail = server.json(f"/api/run?layer={tf.NAME}&run={tf.RUNNING}")
    assert detail["testing"]["requested_full"]["by"] == "dashboard"
    status, data = post(server, {"layer": tf.NAME, "adw_id": tf.TARGETED, "depth": "full"}, path="/api/run-testing")
    assert status == 409 and "not running" in data["error"]
    assert not (site["layer"] / "agents" / tf.TARGETED / "testing-request.json").exists()
    for body in ({"layer": tf.NAME, "adw_id": tf.RUNNING, "depth": "targeted"},
                 {"layer": tf.NAME, "adw_id": "../x", "depth": "full"},
                 {"layer": tf.NAME, "adw_id": tf.RUNNING}, {"layer": tf.NAME, "adw_id": tf.RUNNING, "depth": "full", "x": 1}):
        assert post(server, body, path="/api/run-testing")[0] == 400


def test_start_passes_testing_flag(server, site, monkeypatch):
    seen: list[list[str]] = []
    real = subprocess.run

    def fake_run(argv, **kwargs):   # only the layer's launcher is replaced: it answers like a detached start
        if not (isinstance(argv, list) and len(argv) > 1 and str(argv[1]).endswith(d.LAUNCHER)):
            return real(argv, **kwargs)
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, json.dumps({"status": "started", "run_id": "7e5700d1"}).encode(), b"")

    monkeypatch.setattr(d.subprocess, "run", fake_run)
    base = {"layer": tf.NAME, "target": {"kind": "workflow", "name": "plan-build"}, "prompt": "Tidy the readme",
            "issue": "none"}
    status, data = post(server, {**base, "request_key": "testing-key-1", "testing": "heavy"}, path="/api/start")
    assert status == 202 and data["run_id"] == "7e5700d1"
    argv = seen[-1]
    assert argv[argv.index("--testing") + 1] == "heavy"
    assert post(server, {**base, "request_key": "testing-key-2"}, path="/api/start")[0] == 202
    assert "--testing" not in seen[-1]
    status, data = post(server, {**base, "request_key": "testing-key-3", "testing": "full"}, path="/api/start")
    assert status == 400 and "testing" in data["error"] and len(seen) == 2
