"""Subscription usage: what the agent CLI says about its usage windows is recorded (events, the
layer-level status file, the billing mode of each call), a call the CLI refused is handed to a
person without a retry, and the layer's threshold stops or waits before a new agent step. Git is
real; the agent is the mock replaying stream-json events, or a fake `claude` that prints a
transcript. The clock is a fake: nothing here ever really sleeps.

Fixture transcripts (data/usage/, ids replaced by placeholders): subscription.jsonl and
api-key.jsonl have the shapes observed with Claude Code 2.1.322; subscription-rejected.jsonl has
the observed shape with the status `rejected` taken from the public SDK reference (not observed);
subscription-five-hour.jsonl is derived (the key `five_hour` and the status `allowed_warning` were
not observed); documented-minimal.jsonl is the shape of the public SDK reference."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from adw_modules import budget, events, ir, models, usage
from adw_modules.runner import main

ADW_ID = "5ab5c001"
DATA = Path(__file__).parent / "data" / "usage"
T0 = 1795000000 - 3600 * 24          # before both reset times of the fixtures
WEEKLY_RESET = 1795096800            # 2026-11-19T14:00:00+00:00
WEEKLY_ISO = "2026-11-19T14:00:00+00:00"
TIERS = [{"tier": "base", "model": "claude-opus-5-5", "effort": "default"},
         {"tier": "heavy", "model": "claude-fable-5-2", "effort": "high"}]
CLAUDE = {"id": "claude", "kind": "claude-code", "status": "stable", "models": TIERS, "permissionMode": "acceptEdits",
          "safeMode": True, "allowedTools": [], "maxTurns": 5, "timeout": 20}
MOCK = {"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}
TEXT = {"template": "build", "output": "text", "description": "", "file": "", "focus": [], "fabric": [],
        "relevant": [], "validation": []}
PLAN_BUILD = [{"name": "plan", "kind": "run", "command": "plan", "modelSet": "inherit"},
              {"name": "build", "kind": "run", "command": "build", "modelSet": "inherit"}]


def _layer(make_layer, *, agents=None, steps_=None, budget_=None) -> Path:
    ir_ = {
        "agents": agents or [MOCK],
        "commands": [{"name": "plan", "modelSet": "heavy", **TEXT}, {"name": "build", "modelSet": "base", **TEXT}],
        "environments": [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 27950,
                          "portsPerRun": 0, "keep": "never", "status": "stable"}],
        "reviews": [{"id": "human", "kind": "human", "status": "stable", "reviewers": [], "draft": False, "bodyCmd": ""}],
        "adws": [{"name": "demo", "agent": (agents or [MOCK])[0]["id"], "review": "human",
                  "environment": ["wt"], "input": ["cli"], "trigger": ["manual"], "classes": [],
                  "steps": steps_ or [*PLAN_BUILD, {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}]}],
        "promptInputs": [{"id": "cli", "kind": "cli", "status": "stable", "allowStdin": False}],
        "budget": budget_,
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
    }
    return make_layer(ir_)


def _limits(percent=0, on="stop", minutes=300, **usd) -> dict:
    return {"id": "limits", "kind": "limits", "status": "stable", "runUsd": usd.get("run"), "stepUsd": usd.get("step"),
            "dayUsd": usd.get("day"), "limitStopPercent": percent, "onLimit": on, "limitWaitMinutes": minutes}


def _mock(layer: Path, step: str, **script) -> None:
    path = layer / "mock" / f"{step}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(script), encoding="utf-8")


def _state(layer: Path, adw_id: str = ADW_ID) -> dict:
    return json.loads((layer / "agents" / adw_id / "state.json").read_text())


def _events(layer: Path, adw_id: str = ADW_ID) -> list[dict]:
    return events.read(layer / "agents" / adw_id / "events.jsonl")


def _fixture(name: str, drop_result: bool = False) -> list[dict]:
    lines = [json.loads(line) for line in (DATA / name).read_text().splitlines() if line.strip()]
    return [line for line in lines if not (drop_result and line.get("type") == "result")]


def _status_file(layer: Path) -> dict:
    return json.loads((layer / "agents" / usage.FILE_NAME).read_text())


def _write_status(layer: Path, windows: dict, billing: str = "subscription") -> None:
    path = layer / "agents" / usage.FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    listed = {name: {"window": name, "status": "allowed", "at": "2026-11-15T18:57:41.294Z", "run": "aaaa0001",
                     "step": "build", **record} for name, record in windows.items()}
    path.write_text(json.dumps({"version": 1, "billing": billing, "windows": listed}))


class Clock:
    def __init__(self) -> None:
        self.t = float(T0)
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(usage, "now", fake.now)
    monkeypatch.setattr(usage, "sleep", fake.sleep)
    for key in (models.ENV_TIER, models.ENV_MODELS, models.ENV_MAX_COST):
        monkeypatch.delenv(key, raising=False)
    return fake


@pytest.fixture
def transcript_claude(tmp_path, monkeypatch):
    """A `claude` on PATH that prints the transcript named by $FAKE_TRANSCRIPT and exits $FAKE_EXIT."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "claude"
    exe.write_text("#!/usr/bin/env python3\nimport os, sys\nsys.stdin.read()\n"
                   "sys.stdout.write(open(os.environ['FAKE_TRANSCRIPT'], encoding='utf-8').read())\n"
                   "sys.stdout.flush()\nsys.exit(int(os.environ.get('FAKE_EXIT', '0')))\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def use(name: str, exit_code: int = 0) -> None:
        monkeypatch.setenv("FAKE_TRANSCRIPT", str(DATA / name))
        monkeypatch.setenv("FAKE_EXIT", str(exit_code))
    return use


# ------------------------------------------------------------------------------------ the mock agent

def test_mock_events_are_written_to_the_transcript_and_validated(make_layer):
    layer_dir = _layer(make_layer)
    replay = _fixture("subscription.jsonl", drop_result=True)
    _mock(layer_dir, "plan", output="planned", events=replay, cost=0.5)
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID]) == 0
    lines = [json.loads(line) for line in (layer_dir / "agents" / ADW_ID / "plan" / "raw.jsonl").read_text().splitlines()]
    assert "script" in lines[0] and "events" not in lines[0]         # each event appears once
    assert lines[1:-1] == replay
    assert lines[-1]["type"] == "result" and lines[-1]["total_cost_usd"] == 0.5
    _mock(layer_dir, "plan", events="x")
    assert main("demo", layer_dir, ["x", "--adw-id", "5ab5c002"]) == 1
    plan = next(r for r in _state(layer_dir, "5ab5c002")["steps"] if r["name"] == "plan")
    assert "mock events must be a list of JSON objects (stream-json events)" in plan["detail"]


# ------------------------------------------------------------------------------------ reading a transcript

def test_scan_reads_billing_windows_and_times_from_a_real_transcript():
    found = usage.scan(DATA / "subscription.jsonl")
    assert (found["billing"], found["api_key_source"]) == ("subscription", "none")
    assert len(found["reports"]) == 1
    report = found["reports"][0]
    assert report["at"] == "2026-11-15T18:57:41.294Z"       # the event has no time: the next line that has one
    assert usage.windows(report["info"]) == [
        {"window": "seven_day", "status": "allowed", "used": 0.57, "resets_at": WEEKLY_ISO},
        {"window": "seven_day_overage_included", "status": "allowed", "used": 0.6, "resets_at": WEEKLY_ISO}]
    assert usage.rejection(found) is None
    five = usage.scan(DATA / "subscription-five-hour.jsonl")
    assert [(w["window"], w["used"], w["status"]) for w in usage.windows(five["reports"][0]["info"])] == [
        ("five_hour", 0.93, "allowed_warning"), ("seven_day", 0.41, "allowed")]
    rejected = usage.scan(DATA / "subscription-rejected.jsonl")
    assert rejected["reports"][0]["at"] is None             # no line of that transcript has a time
    assert usage.rejection(rejected) == {"window": "seven_day", "status": "rejected", "used": 1.0,
                                         "resets_at": WEEKLY_ISO}
    assert usage.scan(DATA / "missing.jsonl") == {"billing": "", "api_key_source": None, "reports": []}
    assert (usage.label("five_hour"), usage.label("seven_day"), usage.label("seven_day_overage_included")) == (
        "5-hour", "weekly", "seven day overage included")
    assert usage.when(WEEKLY_ISO) == "resets 2026-11-19 14:00 UTC"
    assert usage.when(None) == "the CLI reported no reset time"
    assert usage.epoch("2026-11-19T14:00:00Z") == WEEKLY_RESET and usage.epoch("soon") is None


def test_windows_from_the_documented_minimal_shape():
    found = usage.scan(DATA / "documented-minimal.jsonl")
    assert usage.windows(found["reports"][0]["info"]) == [
        {"window": "unknown", "status": "allowed_warning", "used": 0.8, "resets_at": "2026-11-18T11:06:40+00:00"}]
    # a status and a reset time but no share: no number is invented
    assert usage.windows({"status": "allowed", "resetsAt": 1795000000}) == [
        {"window": "unknown", "status": "allowed", "used": None, "resets_at": "2026-11-18T11:06:40+00:00"}]
    assert usage.windows({"status": "allowed", "utilization": True})[0]["used"] is None
    # a refusal that names only the top-level status, with the documented error code
    refused = {"billing": "subscription", "api_key_source": "none", "reports": [
        {"at": None, "info": {"status": "rejected", "resetsAt": 1795000000, "errorCode": "credits_required"}}]}
    assert usage.rejection(refused) == {"window": "unknown", "status": "rejected", "used": None,
                                        "resets_at": "2026-11-18T11:06:40+00:00", "error_code": "credits_required"}
    top_only = {"reports": [{"at": None, "info": {"status": "rejected", "rateLimitType": "five_hour", "unifiedWindows": {
        "five_hour": {"utilization": 0.99, "resetsAt": 1795000000, "status": "allowed_warning"}}}}]}
    assert usage.rejection(top_only)["window"] == "five_hour" and usage.rejection(top_only)["status"] == "rejected"


def test_billing_of_names_only_what_the_cli_says():
    assert usage.billing_of("none") == "subscription"
    for source in ("ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key"):
        assert usage.billing_of(source) == "api-key"
    for source in (None, "", "oauth", 0, ["none"]):
        assert usage.billing_of(source) == ""


# ------------------------------------------------------------------------------------ recording

def test_a_subscription_call_records_events_the_status_file_and_billing(make_layer):
    layer_dir = _layer(make_layer, steps_=[*PLAN_BUILD, {"name": "polish", "kind": "run", "command": "build",
                                                         "modelSet": "inherit"}])
    replay = _fixture("subscription.jsonl", drop_result=True)
    _mock(layer_dir, "plan", output="planned", events=replay, cost=0.5)
    _mock(layer_dir, "build", output="built", files={"feature.txt": "x\n"})
    # a `rejected` report in a call that nevertheless succeeded: recorded, and the run continues
    _mock(layer_dir, "polish", output="polished", events=_fixture("subscription-rejected.jsonl"), cost=0.25)
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID]) == 0
    recorded = _events(layer_dir)
    info = replay[1]["rate_limit_info"]
    plan = [e for e in recorded if e["type"] == "limit.status" and e["step"] == "plan"]
    assert [(e["window"], e["status"], e["used"], e["resets_at"], e["owner"], e["billing"]) for e in plan] == [
        ("seven_day", "allowed", 0.57, WEEKLY_ISO, "plan", "subscription"),
        ("seven_day_overage_included", "allowed", 0.6, WEEKLY_ISO, "plan", "subscription")]
    assert all(e["raw"] == info and e["at"] == "2026-11-15T18:57:41.294Z" for e in plan)
    assert not [e for e in recorded if e["type"] == "limit.status" and e["step"] == "build"]
    ends = {e["step"]: e["billing"] for e in recorded if e["type"] == "agent.end"}
    assert ends == {"plan": "subscription", "build": "", "polish": "subscription"}
    assert not [e for e in recorded if e["type"] == "limit.stop"]

    data = _state(layer_dir)
    assert data["status"] == "passed"
    assert data["usage"]["billing"] == "subscription"
    assert data["usage"]["windows"]["seven_day_overage_included"] == {
        "window": "seven_day_overage_included", "status": "allowed", "used": 0.6, "resets_at": WEEKLY_ISO,
        "at": "2026-11-15T18:57:41.294Z", "step": "plan"}
    assert (data["usage"]["windows"]["seven_day"]["status"], data["usage"]["windows"]["seven_day"]["step"]) == (
        "rejected", "polish")

    status = _status_file(layer_dir)
    assert (status["version"], status["billing"], status["api_key_source"], status["billing_run"]) == (
        1, "subscription", "none", ADW_ID)
    assert status["billing_at"] == usage.iso(T0)
    assert status["windows"]["seven_day_overage_included"] == {
        "window": "seven_day_overage_included", "status": "allowed", "used": 0.6, "resets_at": WEEKLY_ISO,
        "at": "2026-11-15T18:57:41.294Z", "run": ADW_ID, "step": "plan"}
    weekly = status["windows"]["seven_day"]
    assert (weekly["status"], weekly["used"], weekly["run"], weekly["step"]) == ("rejected", 1.0, ADW_ID, "polish")
    assert weekly["at"]                                     # no time in that transcript: the end of the call
    assert not list((layer_dir / "agents").glob(".usage-*"))   # no temp file is left behind


def test_the_status_file_keeps_the_latest_report_per_window_across_runs(make_layer):
    layer_dir = _layer(make_layer)
    _mock(layer_dir, "plan", events=_fixture("subscription.jsonl", drop_result=True), cost=0.5)
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID]) == 0
    _mock(layer_dir, "plan", events=_fixture("subscription-five-hour.jsonl", drop_result=True), cost=0.5)
    assert main("demo", layer_dir, ["x", "--adw-id", "5ab5c002"]) == 0
    listed = _status_file(layer_dir)["windows"]
    assert {name: (w["used"], w["run"]) for name, w in listed.items()} == {
        "five_hour": (0.93, "5ab5c002"), "seven_day": (0.41, "5ab5c002"),
        "seven_day_overage_included": (0.6, ADW_ID)}
    assert listed["five_hour"]["status"] == "allowed_warning"


def test_old_transcripts_and_agents_without_usage_record_nothing(make_layer, capsys):
    layer_dir = _layer(make_layer)
    _mock(layer_dir, "build", files={"feature.txt": "x\n"}, cost=0.5)
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID]) == 0
    data = _state(layer_dir)
    assert data["usage"] == {}
    assert {c["billing"] for c in data["agent_calls"]} == {""}
    assert "billing" not in data["cost"] and "api_equivalent" not in data["cost"]
    assert not [e for e in _events(layer_dir) if e["type"].startswith("limit.")]
    assert not (layer_dir / "agents" / usage.FILE_NAME).exists()
    captured = capsys.readouterr()
    assert "[usage]" not in captured.err and "API-equivalent" not in captured.out
    # a state.json from before this feature still loads (the field has a default)
    path = layer_dir / "agents" / ADW_ID / "state.json"
    old = json.loads(path.read_text())
    del old["usage"]
    path.write_text(json.dumps(old))
    from adw_modules import state as state_module
    assert state_module.load(layer_dir, ADW_ID).usage == {}


def test_an_api_key_call_is_labelled_and_records_no_window(make_layer, transcript_claude, capsys):
    layer_dir = _layer(make_layer, agents=[CLAUDE], steps_=PLAN_BUILD)
    transcript_claude("api-key.jsonl")
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID]) == 0
    data = _state(layer_dir)
    assert [c["billing"] for c in data["agent_calls"]] == ["api-key", "api-key"]
    assert data["cost"]["billing"] == "api-key" and "api_equivalent" not in data["cost"]
    status = _status_file(layer_dir)
    assert (status["billing"], status["api_key_source"], status["windows"]) == ("api-key", "ANTHROPIC_API_KEY", {})
    out = capsys.readouterr().out
    assert "cost:      $1.00\n" in out and "API-equivalent" not in out


def test_a_corrupt_or_unwritable_status_file_never_fails_a_run(make_layer, capsys):
    layer_dir = _layer(make_layer, budget_=_limits(percent=50))
    replay = _fixture("subscription.jsonl", drop_result=True)
    _mock(layer_dir, "plan", events=replay, cost=0.5)
    _mock(layer_dir, "build", events=replay, files={"feature.txt": "x\n"}, cost=0.5)
    target = layer_dir / "agents" / usage.FILE_NAME
    target.mkdir(parents=True)                               # the file cannot be written: it is a directory
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID]) == 0
    assert capsys.readouterr().err.count("[usage] warning") == 1     # once, not once per call
    assert _state(layer_dir)["usage"]["windows"]["seven_day"]["used"] == 0.57   # the run's own record is kept
    target.rmdir()
    target.write_text("not json")
    assert usage.load(layer_dir) == {} and usage.over(layer_dir, 50) == []
    _mock(layer_dir, "build", files={"feature.txt": "x\n"})
    assert main("demo", layer_dir, ["x", "--adw-id", "5ab5c002", "--max-cost", "50"]) == 1   # 60% >= 50%: stopped at build
    assert "[usage] warning" not in capsys.readouterr().err
    assert _status_file(layer_dir)["windows"]["seven_day"]["used"] == 0.57   # replaced by a valid file


# ------------------------------------------------------------------------------------ dollars on a subscription

def test_the_summary_and_state_label_dollars_api_equivalent_for_subscription_calls(make_layer, capsys):
    layer_dir = _layer(make_layer)
    subscription = _fixture("subscription.jsonl", drop_result=True)
    _mock(layer_dir, "plan", events=subscription, cost=0.5)
    _mock(layer_dir, "build", events=subscription, files={"feature.txt": "x\n"}, cost=0.75)
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID]) == 0
    data = _state(layer_dir)
    assert [c["billing"] for c in data["agent_calls"]] == ["subscription", "subscription"]
    assert data["cost"]["billing"] == "subscription"
    assert data["cost"]["api_equivalent"] == data["cost"]["total"] == 1.25
    assert ("cost:      $1.25 API-equivalent; subscription calls: an estimate at API prices, not what you pay\n"
            in capsys.readouterr().out)

    # a subscription call, then one whose transcript says nothing: still a subscription run
    _mock(layer_dir, "build", files={"feature.txt": "x\n"}, cost=0.75)
    assert main("demo", layer_dir, ["x", "--adw-id", "5ab5c002"]) == 0
    cost = _state(layer_dir, "5ab5c002")["cost"]
    assert (cost["billing"], cost["api_equivalent"], cost["total"]) == ("subscription", 0.5, 1.25)
    assert "cost:      $1.25 API-equivalent; subscription calls" in capsys.readouterr().out

    # a subscription call and an API-key call in one run
    _mock(layer_dir, "build", events=_fixture("api-key.jsonl", drop_result=True), files={"feature.txt": "x\n"}, cost=0.75)
    assert main("demo", layer_dir, ["x", "--adw-id", "5ab5c003"]) == 0
    cost = _state(layer_dir, "5ab5c003")["cost"]
    assert (cost["billing"], cost["api_equivalent"], cost["total"]) == ("mixed", 0.5, 1.25)
    assert ("cost:      $1.25 ($0.50 of it API-equivalent); subscription calls: an estimate at API prices, "
            "not what you pay\n") in capsys.readouterr().out

    # no transcript says how the calls were billed: the line is what it was before this feature
    _mock(layer_dir, "plan", cost=0.5)
    _mock(layer_dir, "build", files={"feature.txt": "x\n"})
    assert main("demo", layer_dir, ["x", "--adw-id", "5ab5c004"]) == 0
    assert "cost:      $0.50; reports no cost, counted as $0: mock (mock)\n" in capsys.readouterr().out


# ------------------------------------------------------------------------------------ a refused call

REFUSED = ("limit:seven_day: the weekly subscription limit is reached (the CLI refused the call); "
           "resets 2026-11-19 14:00 UTC")


def _refused_run(make_layer, *flags: str) -> Path:
    layer_dir = _layer(make_layer, steps_=[
        {"name": "plan", "kind": "run", "command": "plan", "modelSet": "inherit"},
        {"name": "build", "kind": "run", "command": "build", "modelSet": "inherit", "retries": 2},
        {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}])
    _mock(layer_dir, "plan", events=_fixture("subscription.jsonl", drop_result=True), cost=0.5)
    _mock(layer_dir, "build", files={"feature.txt": "half done\n"}, events=_fixture("subscription-rejected.jsonl"),
          fail="agent exited 1")
    assert main("demo", layer_dir, ["Add a feature", "--adw-id", ADW_ID, *flags]) == 1
    return layer_dir


def test_a_refused_call_stops_through_the_handover_and_is_not_retried(make_layer, capsys):
    layer_dir = _refused_run(make_layer)
    data = _state(layer_dir)
    assert data["status"] == "failed"
    assert [c["step"] for c in data["agent_calls"]] == ["plan", "build"]       # retries: 2, but a limit stop is final
    assert data["agent_calls"][1]["status"] == "failed" and data["agent_calls"][1]["billing"] == "subscription"
    build = next(r for r in data["steps"] if r["name"] == "build")
    assert build["status"] == "failed" and REFUSED in build["detail"]
    assert data["ship"]["action"] == "handoff" and data["ship"]["stopped_at"] == "build"
    assert data["ship"]["reasons"] == [f"run-stopped:build: {REFUSED}"]
    assert REFUSED in (layer_dir / data["ship"]["target"]).read_text()         # PR.md
    assert data["usage"]["stopped"] == {"window": "seven_day", "cause": "reached", "status": "rejected", "used": 1.0,
                                        "percent": None, "resets_at": WEEKLY_ISO, "step": "build", "reason": REFUSED}
    assert data["cost"]["calls"] == 2                                          # the refused call is still counted
    recorded = _events(layer_dir)
    stop = next(e for e in recorded if e["type"] == "limit.stop")
    assert (stop["cause"], stop["window"], stop["step"], stop["reason"]) == ("reached", "seven_day", "build", REFUSED)
    assert not [e for e in recorded if e["type"] == "budget.stop"]
    assert next(e for e in recorded if e["type"] == "handoff")["reason"].startswith("limit:seven_day:")
    out = capsys.readouterr().out
    assert f"; STOPPED by {REFUSED}" in out and "retrying" not in out


def test_a_claude_call_cut_off_at_the_limit_is_a_limit_stop(make_layer, transcript_claude):
    layer_dir = _layer(make_layer, agents=[CLAUDE], steps_=[
        {"name": "plan", "kind": "run", "command": "plan", "modelSet": "inherit", "retries": 1},
        {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}])
    transcript_claude("subscription-rejected.jsonl", exit_code=1)
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID]) == 1
    data = _state(layer_dir)
    plan = next(r for r in data["steps"] if r["name"] == "plan")
    assert REFUSED in plan["detail"] and "agent exited 1" not in plan["detail"]
    assert len(data["agent_calls"]) == 1
    assert data["usage"]["stopped"]["cause"] == "reached"
    # a failed call whose transcript has no `rejected` report stays an ordinary agent error
    transcript_claude("subscription.jsonl", exit_code=1)
    assert main("demo", layer_dir, ["x", "--adw-id", "5ab5c002"]) == 1
    other = _state(layer_dir, "5ab5c002")
    assert "agent exited 1" in next(r for r in other["steps"] if r["name"] == "plan")["detail"]
    assert "stopped" not in other["usage"] and len(other["agent_calls"]) == 2   # retried once


def test_resume_after_a_limit_stop_is_judged_afresh(make_layer, clock):
    layer_dir = _refused_run(make_layer, "--keep-worktree")
    assert _state(layer_dir)["usage"]["stopped"]["window"] == "seven_day"
    _mock(layer_dir, "build", files={"feature.txt": "done\n"}, cost=0.5,
          events=_fixture("subscription-five-hour.jsonl", drop_result=True))
    clock.t = WEEKLY_RESET + 60
    assert main("demo", layer_dir, ["--resume", "--adw-id", ADW_ID]) == 0
    data = _state(layer_dir)
    assert data["status"] == "passed" and "stopped" not in data["usage"]
    assert data["usage"]["windows"]["seven_day"]["used"] == 0.41


# ------------------------------------------------------------------------------------ the threshold

OVER_50 = ("limit:seven_day_overage_included: 60% of the seven day overage included subscription limit is used "
           "(threshold 50%, #:limit-stop-percent); resets 2026-11-19 14:00 UTC")


def _threshold_layer(make_layer, **limits) -> Path:
    layer_dir = _layer(make_layer, budget_=_limits(**limits))
    _mock(layer_dir, "plan", files={"plan.txt": "the plan\n"}, cost=0.5,
          events=_fixture("subscription.jsonl", drop_result=True))
    _mock(layer_dir, "build", files={"feature.txt": "x\n"}, output="built")
    return layer_dir


def test_threshold_stop_starts_no_new_agent_step_and_hands_over(make_layer, clock):
    layer_dir = _threshold_layer(make_layer, percent=50)
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID]) == 1
    data = _state(layer_dir)
    assert data["budget"]["limits"]["limit_percent"] == 50 and data["budget"]["limits"]["on_limit"] == "stop"
    assert [c["step"] for c in data["agent_calls"]] == ["plan"]                 # build never started
    assert not (layer_dir / "agents" / ADW_ID / "build" / "raw.jsonl").exists()
    assert data["ship"]["action"] == "handoff" and data["ship"]["stopped_at"] == "build"
    # the handover lists the reason clipped to 160 characters; the step's detail has all of it
    assert data["ship"]["reasons"][0].startswith(f"run-stopped:build: {OVER_50[:150]}")
    assert next(r for r in data["steps"] if r["name"] == "build")["detail"].endswith(OVER_50)
    assert OVER_50[:150] in (layer_dir / data["ship"]["target"]).read_text()
    assert data["usage"]["stopped"]["cause"] == "threshold" and data["usage"]["stopped"]["used"] == 0.6
    assert data["usage"]["stopped"]["reason"] == OVER_50
    recorded = _events(layer_dir)
    stop = next(e for e in recorded if e["type"] == "limit.stop")
    assert (stop["cause"], stop["percent"], stop["window"], stop["reason"]) == (
        "threshold", 50, "seven_day_overage_included", OVER_50)
    assert [e["step"] for e in recorded if e["type"] == "agent.start"] == ["plan"]
    assert next(e for e in recorded if e["type"] == "run.start")["limits"]["limit_percent"] == 50
    assert clock.slept == []
    # at the threshold counts ("at or above"), just below it does not
    assert [h["window"] for h in usage.over(layer_dir, 57)] == ["seven_day", "seven_day_overage_included"]
    assert [h["window"] for h in usage.over(layer_dir, 58)] == ["seven_day_overage_included"]
    assert usage.over(layer_dir, 61) == []


def test_threshold_wait_waits_for_the_reset_on_a_fake_clock_then_continues(make_layer, clock, capsys):
    layer_dir = _threshold_layer(make_layer, percent=50, on="wait", minutes=100000)
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID]) == 0
    assert clock.slept == [WEEKLY_RESET - T0]                                   # one wait, to the reset
    data = _state(layer_dir)
    assert data["status"] == "passed" and [c["step"] for c in data["agent_calls"]] == ["plan", "build"]
    assert data["usage"]["waits"] == [{"step": "build", "window": "seven_day_overage_included",
                                       "seconds": float(WEEKLY_RESET - T0), "until": WEEKLY_ISO}]
    assert "stopped" not in data["usage"]
    recorded = _events(layer_dir)
    wait = next(e for e in recorded if e["type"] == "limit.wait")
    assert (wait["step"], wait["window"], wait["used"], wait["percent"], wait["resets_at"], wait["seconds"]) == (
        "build", "seven_day_overage_included", 0.6, 50, WEEKLY_ISO, WEEKLY_RESET - T0)
    kinds = [e["type"] for e in recorded]
    assert kinds.index("limit.wait") < len(kinds) - 1 - kinds[::-1].index("agent.start")   # the wait, then build's call
    assert not [e for e in recorded if e["type"] == "limit.stop"]
    assert ("step build: 60% of the seven day overage included subscription limit is used (threshold 50%); "
            f"waiting {WEEKLY_RESET - T0}s for its reset at 2026-11-19 14:00 UTC") in capsys.readouterr().out


@pytest.mark.parametrize("case", ["too-far", "no-reset-time"])
def test_wait_hands_over_when_the_reset_is_too_far_or_unknown(make_layer, clock, case):
    if case == "too-far":
        layer_dir = _threshold_layer(make_layer, percent=50, on="wait", minutes=5)
        stopped_at, ending = "build", OVER_50 + "; that is more than the 5 minutes #:on-limit wait may wait"
    else:
        layer_dir = _threshold_layer(make_layer, percent=90, on="wait")
        init, report = _fixture("subscription.jsonl")[:2]
        report["rate_limit_info"] = {"status": "allowed", "rateLimitType": "seven_day", "unifiedWindows": {
            "seven_day": {"utilization": 0.95, "status": "allowed"}}}             # a share, but no reset time
        _mock(layer_dir, "plan", files={"plan.txt": "the plan\n"}, events=[init, report], cost=0.5)
        stopped_at = "build"
        ending = ("limit:seven_day: 95% of the weekly subscription limit is used (threshold 90%, #:limit-stop-percent); "
                  "the CLI reported no reset time; without a reset time there is nothing to wait for")
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID]) == 1
    data = _state(layer_dir)
    assert clock.slept == [] and "waits" not in data["usage"]
    if case == "no-reset-time":
        assert _status_file(layer_dir)["windows"]["seven_day"]["resets_at"] is None
    assert data["ship"]["action"] == "handoff" and data["ship"]["stopped_at"] == stopped_at
    assert data["ship"]["reasons"][0].startswith(f"run-stopped:{stopped_at}: {ending[:150]}")
    assert data["usage"]["stopped"]["cause"] == "threshold" and data["usage"]["stopped"]["reason"] == ending
    assert next(r for r in data["steps"] if r["name"] == stopped_at)["detail"].endswith(ending)


def test_a_window_whose_reset_has_passed_no_longer_counts(make_layer, clock):
    layer_dir = _threshold_layer(make_layer, percent=90)
    _write_status(layer_dir, {"seven_day": {"used": 0.95, "resets_at": WEEKLY_ISO}})
    assert [h["window"] for h in usage.over(layer_dir, 90)] == ["seven_day"]
    clock.t = WEEKLY_RESET                                   # the window has reset: the old share says nothing
    assert usage.over(layer_dir, 90) == []
    assert budget.refuse_start(ir.load(layer_dir), {"day": None, "limit_percent": 90, "on_limit": "stop"}) is None
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID]) == 0
    assert [c["step"] for c in _state(layer_dir)["agent_calls"]] == ["plan", "build"]


@pytest.mark.parametrize("budget_", [None, {"id": "limits", "kind": "limits", "status": "stable", "runUsd": None,
                                            "stepUsd": None, "dayUsd": None}, _limits(percent=0),
                                     {**_limits(), "limitStopPercent": True, "limitWaitMinutes": "long"}])
def test_the_threshold_is_off_when_absent_or_zero(make_layer, clock, budget_):
    layer_dir = _layer(make_layer, budget_=budget_)
    _write_status(layer_dir, {"seven_day": {"used": 0.99, "resets_at": WEEKLY_ISO}})
    lim = budget.limits(ir.load(layer_dir), None)
    assert (lim["limit_percent"], lim["on_limit"], lim["wait_minutes"]) == (0, "stop", 300)
    assert budget.refuse_start(ir.load(layer_dir), lim) is None
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID]) == 0
    assert clock.slept == [] and _state(layer_dir)["usage"] == {}


def test_no_run_starts_at_the_threshold_with_on_limit_stop(make_layer, capsys):
    layer_dir = _layer(make_layer, budget_=_limits(percent=90))
    _write_status(layer_dir, {"five_hour": {"used": 0.91, "resets_at": "2026-11-18T11:06:40+00:00"},
                              "seven_day": {"used": 0.95, "resets_at": WEEKLY_ISO}})
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID]) == budget.EXIT_REFUSED
    err = capsys.readouterr().err
    assert ("refused: limit:seven_day: 95% of the weekly subscription limit is used (threshold 90%, "
            "#:limit-stop-percent); resets 2026-11-19 14:00 UTC; no run starts until then, or raise "
            "#:limit-stop-percent in (budget limits …)") in err
    assert not (layer_dir / "agents" / ADW_ID).exists()
    layer = ir.load(layer_dir)
    assert budget.refuse_start(layer, budget.limits(layer, None)).startswith("limit:seven_day: 95%")
    waiting = _layer(make_layer, budget_=_limits(percent=90, on="wait"))
    layer = ir.load(waiting)
    assert budget.refuse_start(layer, budget.limits(layer, None)) is None      # that run starts, and waits


def test_api_key_billing_switches_the_threshold_off(make_layer, transcript_claude):
    layer_dir = _layer(make_layer, agents=[CLAUDE], steps_=PLAN_BUILD, budget_=_limits(percent=90))
    _write_status(layer_dir, {"seven_day": {"used": 0.95, "resets_at": WEEKLY_ISO}}, billing="api-key")
    assert usage.over(layer_dir, 90) == []                   # stale windows of a subscription no longer in use
    transcript_claude("api-key.jsonl")
    assert main("demo", layer_dir, ["x", "--adw-id", ADW_ID]) == 0
    assert len(_state(layer_dir)["agent_calls"]) == 2
    status = _status_file(layer_dir)
    assert status["billing"] == "api-key" and status["windows"]["seven_day"]["used"] == 0.95
