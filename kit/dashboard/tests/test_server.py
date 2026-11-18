"""HTTP-level tests: SSE framing and streaming, incremental tails, read-only file serving,
path traversal and symlink refusals, tokens, Host checks and escaping. Real sockets, no network."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest

import dashboard as d
import fixtures
from conftest import SSEReader, start_server, stop_server


# ------------------------------------------------------------------------------ SSE framing

def test_sse_frame_single_and_multi_line():
    assert d.sse_frame("run", b'{"a":1}', 7) == b'id: 7\nevent: run\ndata: {"a":1}\n\n'
    assert d.sse_frame("text", "one\ntwo") == b"event: text\ndata: one\ndata: two\n\n"


def test_json_payloads_cannot_close_a_script_or_break_a_frame():
    payload = d.json_bytes({"x": "</script><b>&\nline"})
    assert b"<" not in payload and b">" not in payload and b"\n" not in payload
    assert json.loads(payload) == {"x": "</script><b>&\nline"}


# ------------------------------------------------------------------------------ snapshot + stream

def test_late_joiner_gets_snapshot_then_deltas(server, fx):
    reader = SSEReader(server.port, "/api/events")
    try:
        assert reader.status == 200 and b"text/event-stream" in reader.headers
        data, _ = reader.wait_for("snapshot")
        snapshot = json.loads(data)
        assert {r["id"] for r in snapshot["runs"]} >= {"a1000001", "a1000006"}
        assert snapshot["layers"][0]["key"] == "demo-shop"
        # the running run finishes its build step: the stream says so without a reload
        state_path = fx["agents"] / "a1000006" / "state.json"
        state = json.loads(state_path.read_text())
        state["steps"][-1].update(status="passed", ended="2026-11-01T11:24:30+00:00")
        state.update(status="passed", ended="2026-11-01T11:24:31+00:00")
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, state_path)
        data, event_id = reader.wait_for("run", lambda raw: json.loads(raw)["id"] == "a1000006")
        assert json.loads(data)["status"] == "passed" and event_id
    finally:
        reader.close()


def test_new_run_appears_on_the_stream(server, fx):
    reader = SSEReader(server.port, "/api/events")
    try:
        reader.wait_for("snapshot")
        new = fx["agents"] / "a2000000"
        new.mkdir()
        (new / "state.json").write_text(json.dumps({"adw_id": "a2000000", "adw": "zte", "prompt": "fresh", "status": "running",
                                                    "started": "2026-11-01T12:00:00+00:00", "steps": []}))
        data, _ = reader.wait_for("run", lambda raw: json.loads(raw)["id"] == "a2000000")
        assert json.loads(data)["title"] == "fresh"
        import shutil
        shutil.rmtree(new)
        data, _ = reader.wait_for("run-removed")
        assert json.loads(data) == {"key": "demo-shop/a2000000"}
    finally:
        reader.close()


def test_transcript_tail_streams_only_the_delta_and_resumes(server, fx):
    path = fx["agents"] / "a1000001" / "build" / "raw.jsonl"
    reader = SSEReader(server.port, "/api/tail?layer=demo-shop&run=a1000001&dir=build&what=transcript")
    try:
        data, first_id = reader.wait_for("rows")
        rows = json.loads(data)
        assert rows[0]["k"] == "init" and rows[-1]["k"] == "final"
        count = len(rows)
        with path.open("a") as handle:
            handle.write(json.dumps({"type": "assistant", "message": {"id": "m-new", "content": [{"type": "text", "text": "appended later"}]}}) + "\n")
        data, second_id = reader.wait_for("rows")
        delta = json.loads(data)
        assert [r["text"] for r in delta] == ["appended later"]       # only the new line
        assert delta[0]["n"] == int(first_id.split(":")[1]) + 1
    finally:
        reader.close()
    # a reconnect with Last-Event-ID gets nothing it already had
    with path.open("a") as handle:
        handle.write(json.dumps({"type": "assistant", "message": {"id": "m-2", "content": [{"type": "text", "text": "after reconnect"}]}}) + "\n")
    again = SSEReader(server.port, "/api/tail?layer=demo-shop&run=a1000001&dir=build&what=transcript",
                      {"Last-Event-ID": second_id})
    try:
        data, _ = again.wait_for("rows")
        assert [r["text"] for r in json.loads(data)] == ["after reconnect"]
    finally:
        again.close()
    assert count > 10


def test_tail_reports_truncation_as_reset(server, fx):
    path = fx["agents"] / "a1000003" / "checks" / "lint.1.log"
    reader = SSEReader(server.port, "/api/tail?layer=demo-shop&run=a1000003&what=check&file=lint.1.log")
    try:
        data, _ = reader.wait_for("text")
        assert "F401" in "".join(s[0] for s in json.loads(data)["segments"])
        path.write_text("x\n")
        reader.wait_for("reset")
        data, _ = reader.wait_for("text")
        assert json.loads(data)["segments"][0][0] == "x\n"
    finally:
        reader.close()


def test_check_tail_renders_ansi_as_segments(server):
    reader = SSEReader(server.port, "/api/tail?layer=demo-shop&run=a1000001&what=check&file=test.1.log")
    try:
        data, _ = reader.wait_for("text")
        segments = json.loads(data)["segments"]
        assert ["2 passed", "#4ade80", "", "b"] in segments
        assert not any("\x1b" in s[0] for s in segments)
    finally:
        reader.close()


def test_growing_transcript_partial_line_then_completion(server, fx):
    path = fx["agents"] / "a1000006" / "build" / "raw.jsonl"
    os.utime(path)
    reader = SSEReader(server.port, "/api/tail?layer=demo-shop&run=a1000006&dir=build&what=transcript")
    try:
        data, _ = reader.wait_for("rows")
        assert not any(r["k"] == "corrupt" for r in json.loads(data))
        with path.open("a") as handle:
            handle.write(fixtures.PARTIAL_EDIT[fixtures.PARTIAL_SPLIT:] + "\n")
        data, _ = reader.wait_for("rows")
        rows = json.loads(data)
        assert rows[-1]["k"] == "tool" and rows[-1]["title"] == "edit shop/catalog.py"
    finally:
        reader.close()


# ------------------------------------------------------------------------------ JSON endpoints

def test_run_detail_endpoint(server):
    detail = server.json("/api/run?layer=demo-shop&run=a1000004")
    assert detail["summary"]["id"] == "a1000004" and len(detail["review"]["rounds"]) == 2
    assert {"graph", "timeline", "agents", "checks", "artefacts", "runlog"} <= set(detail)


def test_step_endpoint_and_raw_line(server):
    step = server.json("/api/step?layer=demo-shop&run=a1000001&dir=build")
    assert step["prompt"].startswith("---") and step["raw_url"].endswith("/agents/a1000001/build/raw.jsonl")
    status, _h, body = server.get("/api/rawline?layer=demo-shop&run=a1000001&dir=build&line=1")
    assert status == 200 and json.loads(body)["subtype"] == "init"


def test_layer_endpoint(server):
    detail = server.json("/api/layer?layer=demo-shop")
    assert [a["name"] for a in detail["adws"]] == ["zte", "candidate", "race", "ask"]


def test_unknown_things_are_404(server):
    for path in ("/api/run?layer=demo-shop&run=zzzzzzzz", "/api/run?layer=nope&run=a1000001",
                 "/api/step?layer=demo-shop&run=a1000001&dir=..", "/api/step?layer=demo-shop&run=a1000001&dir=checks%2F..",
                 "/api/rawline?layer=demo-shop&run=a1000001&dir=build&line=99999", "/nope"):
        assert server.get(path)[0] == 404, path


# ------------------------------------------------------------------------------ read-only file serving

def test_serves_allowed_files(server):
    for path in ("/files/demo-shop/agents/a1000001/build/raw.jsonl", "/files/demo-shop/specs/chore-a1000001.md",
                 "/files/demo-shop/artefacts/a1000001/screenshots/e2e/01-cart.png", "/files/demo-shop/agentic-layer.json",
                 "/files/demo-shop/EXPLAIN.md"):
        status, headers, _ = server.get(path)
        assert status == 200, path
        assert headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in headers["content-security-policy"]


def test_video_supports_ranges(server):
    status, headers, body = server.get("/files/demo-shop/artefacts/a1000001/recording/e2e/session.webm", {"Range": "bytes=0-99"})
    assert status == 206 and len(body) == 100 and headers["content-type"] == "video/webm"
    assert headers["content-range"].startswith("bytes 0-99/")


@pytest.mark.parametrize("path", [
    "/files/demo-shop/../outside-secret.txt",
    "/files/demo-shop/agents/../../outside-secret.txt",
    "/files/demo-shop/agents/%2e%2e/%2e%2e/outside-secret.txt",
    "/files/demo-shop/agents/a1000001/..%2f..%2f..%2foutside-secret.txt",
    "/files/demo-shop//etc/passwd",
    "/files/demo-shop/%2Fetc%2Fpasswd",
    "/files/demo-shop/agents/a1000001/escape.txt",                 # symlink to a file outside the layer
    "/files/demo-shop/artefacts/a1000001/linked/outside-secret.txt",  # symlinked directory
    "/files/demo-shop/commands/secret-template.md",                # inside the layer, but not served
    "/files/demo-shop/agents\\..\\..\\outside-secret.txt",
    "/files/demo-shop/agents/a1000001/build/raw.jsonl%00.png",
])
def test_traversal_and_symlink_escapes_are_refused(server, path):
    status, _headers, body = server.get(path)
    assert status in {403, 404}, (path, status)
    assert b"SECRET" not in body and b"root:" not in body


def test_symlinked_layer_tree_is_refused(fx):
    outside = fx["root"] / "elsewhere"
    outside.mkdir()
    (outside / "x.txt").write_text("SECRET")
    specs = fx["layer"] / "specs"
    import shutil
    shutil.rmtree(specs)
    os.symlink(outside, specs)
    layer = d.load_layer(fx["layer"].resolve(), set())
    with pytest.raises(d.Refused):
        d.safe_layer_file(layer, "specs/x.txt")


def test_nothing_can_be_changed(server):
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        assert server.get("/api/snapshot", method=method)[0] == 405
    for method in ("PUT", "DELETE", "PATCH"):   # the one write is POST /api/models
        assert server.get("/api/models", method=method)[0] == 405


def test_dns_rebinding_host_is_refused(server):
    assert server.get("/api/snapshot", {"Host": "evil.example:8765"})[0] == 403
    assert server.get("/api/snapshot", {"Host": f"localhost:{server.port}"})[0] == 200


# ------------------------------------------------------------------------------ tokens and binding

def test_non_loopback_bind_needs_a_token():
    with pytest.raises(SystemExit):
        d.parse_args(["--layer", ".", "--bind", "0.0.0.0"])
    args = d.parse_args(["--layer", ".", "--bind", "0.0.0.0", "--token", "t0k"])
    assert args.token == "t0k"
    assert d.parse_args(["--layer", ".", "--bind", "::1"]).bind == "::1"


def test_token_is_enforced_on_every_request(fx):
    running = start_server(["--layer", str(fx["layer"]), "--token", "s3cret-token"])
    try:
        for path in ("/", "/api/snapshot", "/api/events", "/files/demo-shop/agentic-layer.json", "/static/app.js"):
            assert running.get(path)[0] == 401, path
        assert running.get("/api/snapshot?token=wrong")[0] == 401
        status, headers, _ = running.get("/?token=s3cret-token")
        assert status == 200 and "al_dashboard_token=s3cret-token" in headers["set-cookie"]
        assert "HttpOnly" in headers["set-cookie"] and "SameSite=Strict" in headers["set-cookie"]
        assert running.get("/api/snapshot", {"Cookie": "al_dashboard_token=s3cret-token"})[0] == 200
        assert running.get("/api/snapshot", {"Authorization": "Bearer s3cret-token"})[0] == 200
    finally:
        stop_server(running)


# ------------------------------------------------------------------------------ escaping

def test_hostile_content_is_escaped_in_every_json_response(server):
    bodies = [server.get("/api/snapshot")[2], server.get("/api/run?layer=demo-shop&run=a1000007")[2],
              server.get("/api/step?layer=demo-shop&run=a1000007&dir=plan-chore")[2]]
    for body in bodies:
        assert b"<script" not in body.lower() and b"<img" not in body.lower()
    detail = json.loads(bodies[1])
    assert "<script>window.__pwned=1</script>" in detail["summary"]["title"]   # kept as text, not dropped


def test_page_is_static_with_a_strict_csp(server):
    status, headers, body = server.get("/")
    csp = headers["content-security-policy"]
    assert status == 200 and "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert b"<script src=\"/static/app.js\"" in body
    assert re.search(rb"<script>[^<]", body) is None          # no inline script
    assert not re.search(rb"https?://", body.replace(b"http://www.w3.org/2000/svg", b""))  # nothing from a CDN


@pytest.mark.parametrize("name", ["app.js", "voice.js", "voice-worklet.js"])
def test_page_script_never_parses_data_as_html(name):
    script = (d.STATIC_DIR / name).read_text()
    assert "innerHTML" not in script and "outerHTML" not in script and "insertAdjacentHTML" not in script
    assert "document.write" not in script and "eval(" not in script and "new Function" not in script


def test_watcher_is_cheap_when_idle(fx):
    model = d.Model([fx["layer"]], [])
    model.discover()
    model.tick()
    started = time.process_time()
    for _ in range(50):
        model.tick()
    assert (time.process_time() - started) / 50 < 0.02    # well under 20 ms CPU per idle poll
