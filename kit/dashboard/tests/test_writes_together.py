"""The two writing features in one server: starting work (POST /api/start) and the workspace actions
(POST /api/action), beside the Models write (POST /api/models). One write guard with a body limit per
write; --read-only turns every write off, --no-start and --no-actions each only their own. The voice
helper's one write (POST /api/voice/approve) follows the same flags per kind of card, while its
conversation (POST /api/voice/turn) keeps working."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

import dashboard as d
import voice_fixtures as vf
import workspace_fixtures as wf
from conftest import Running
from test_start import run_dirs, wait_state

READ_ONLY = {"error": "the dashboard was started with --read-only"}


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    wf.build(root)
    return root


@pytest.fixture()
def al(tmp_path: Path) -> Path:
    return wf.fake_al(tmp_path / "bin" / "al", out="# LLM.txt draft")


def headers(running: Running) -> dict[str, str]:
    return {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{running.port}",
            "Host": f"127.0.0.1:{running.port}", "X-AL-Dashboard": "1"}


def post(running: Running, path: str, body: Any) -> tuple[int, dict]:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    status, _headers, data = running.get(path, headers(running), method="POST", body=raw)
    try:
        return status, json.loads(data) if data else {}
    except ValueError:
        return status, {"raw": data.decode("utf-8", "replace")}


def plan_build(prompt: str = "Add a word count", **extra: Any) -> dict:
    return {"layer": "notes", "target": {"kind": "workflow", "name": "plan-build"}, "prompt": prompt,
            "request_key": uuid.uuid4().hex, **extra}


def scout() -> dict:
    return {"repo": "fern", "action": "scout"}


def models() -> dict:
    return {"layer": "notes", "tier": "base", "model": "claude-sonnet-5-5"}


def both(workspace_server, start_site, ws: Path, al: Path, *flags: str) -> Running:
    return workspace_server(ws, "--layer", str(start_site["notes"]), *flags, al=al)


def test_read_only_refuses_models_start_and_actions(start_site, ws, al, workspace_server):
    running = both(workspace_server, start_site, ws, al, "--read-only")
    assert post(running, "/api/models", models()) == (403, READ_ONLY)
    assert post(running, "/api/start", plan_build()) == (403, READ_ONLY)
    assert post(running, "/api/action", scout()) == (403, READ_ONLY)
    assert wf.calls(al) == []


def test_no_start_keeps_actions_on(start_site, ws, al, workspace_server):
    running = both(workspace_server, start_site, ws, al, "--no-start")
    assert post(running, "/api/start", plan_build()) == (403, {"error": "the dashboard was started with --no-start"})
    status, body = post(running, "/api/action", scout())
    assert status == 202, body
    assert running.json("/api/snapshot")["actions"]["enabled"] is True


def test_no_actions_keeps_starting_on(start_site, ws, al, workspace_server):
    running = both(workspace_server, start_site, ws, al, "--no-actions")
    assert post(running, "/api/action", scout()) == (403, {"error": "actions are off (--no-actions)"})
    assert wf.calls(al) == []
    startable = running.json("/api/startable?layer=notes")
    assert [w["name"] for w in startable["workflows"]] == ["plan-build", "ship-it"]
    layers = {layer["key"]: layer["start"] for layer in running.json("/api/snapshot")["layers"]}
    assert layers["notes"]["enabled"] is True, layers["notes"]


def test_each_write_has_its_body_limit(start_site, ws, al, workspace_server):
    running = both(workspace_server, start_site, ws, al)
    over = {"error": f"request body over {d.MAX_WRITE_BODY} bytes"}
    assert d.MAX_WRITE_BODY == 4096 and d.MAX_START_BODY > 5 * 1024
    assert post(running, "/api/action", {**scout(), "confirm": "x" * 5 * 1024}) == (413, over)
    assert post(running, "/api/models", {**models(), "model": "x" * 5 * 1024}) == (413, over)
    # a 5 KiB prompt passes the guard of /api/start; the bad class then stops it before anything starts
    status, body = post(running, "/api/start", plan_build("x" * 5 * 1024, **{"class": "epic"}))
    assert status == 400 and "over" not in body.get("error", ""), body
    assert wf.calls(al) == []


def test_get_routes_of_both_features(start_site, ws, al, workspace_server):
    running = both(workspace_server, start_site, ws, al)
    assert running.get("/api/startable?layer=notes")[0] == 200
    assert running.get("/api/workspace")[0] == 200
    assert running.get("/api/start")[0] == 405


def test_banner_states_of_both(tmp_path: Path):
    layer = ["--layer", str(tmp_path)]
    assert d.start_banner(d.parse_args(layer)) == "work can be started from the Layers view"
    assert d.start_banner(d.parse_args([*layer, "--no-start"])) == "off (--no-start)"
    assert d.start_banner(d.parse_args([*layer, "--read-only"])) == "off (--read-only)"
    args = d.parse_args([*layer, "--no-start", "--workspace", str(tmp_path), "--no-actions",
                         "--jobs-dir", str(tmp_path / "jobs")])
    assert args.no_start and args.no_actions and args.workspace == [tmp_path] and args.jobs_dir == tmp_path / "jobs"
    assert d.start_banner(args) == "off (--no-start)"


@pytest.mark.parametrize("flag,start_refused,job_refused", [
    ("--read-only", READ_ONLY["error"], READ_ONLY["error"]),
    ("--no-start", "the dashboard was started with --no-start", None),
    ("--no-actions", None, "actions are off (--no-actions)"),
])
def test_voice_approve_follows_each_flag(start_site, ws, al, workspace_server, tmp_path, flag, start_refused, job_refused):
    fake = vf.fake_script(tmp_path / "voice" / "fake.json", [])
    running = workspace_server(ws, "--layer", str(start_site["talk"]), "--voice-fake", str(fake),
                               "--voice-dir", str(tmp_path / "voice" / "conversations"), flag, al=al)
    try:
        voice = vf.VoiceClient(running)
        voice.session(repo="fern")
        start = voice.turn("start a plan for adding a search box")["card"]   # the conversation keeps working
        assert start["kind"] == "start" and start["approvable"] is (start_refused is None)
        assert start["why_not"] == (start_refused or "")
        start = voice.turn("yes")["card"]
        code, body = voice.approve(start)
        if start_refused:
            assert (code, body["error"]) == (403, start_refused)
            assert run_dirs(start_site["talk"]) == []
        else:
            assert code == 202, body
            assert wait_state(start_site["talk"], body["run_id"])["status"] == "passed"
        job = voice.turn("scout it")["card"]
        assert job["kind"] == "job" and job["approvable"] is (job_refused is None) and job["why_not"] == (job_refused or "")
        job = voice.turn("yes")["card"]
        code, body = voice.approve(job)
        if job_refused:
            assert (code, body["error"]) == (403, job_refused)
            assert wf.calls(al) == []
        else:
            assert code == 202 and body["job"], body
    finally:
        running.server.model.voice.stop_all()
