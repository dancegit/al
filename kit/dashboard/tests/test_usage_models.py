"""Subscription usage on the dashboard, from runs the real runtime made (kit/runtime, the mock agent
replaying the stream-json events of the runtime's fixture transcripts, real git): the "Subscription
usage" panel of the Now view with its three states (known / API key / no data yet), the limit events
on a run's timeline, the run a subscription limit stopped, and "API-equivalent" next to the dollars
of a subscription run. What the status file contains is shown as text, never as markup.

With AL_DASHBOARD_SHOTS=<directory> the browser test saves subscription-usage-panel.png and
subscription-usage-run.png there."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import dashboard
from conftest import SSEReader, start_server, stop_server

RUNTIME = Path(__file__).resolve().parents[2] / "runtime"
DATA = RUNTIME / "tests" / "data" / "usage"
PASSED, STOPPED = "ab000001", "ab000002"
WEEKLY_RESET = 1795096800            # 2026-11-19T14:00:00+00:00
HOSTILE = "<img src=x onerror=alert(1)>"
TEXT = {"output": "text", "description": "", "file": "", "focus": [], "fabric": [], "relevant": [], "validation": []}
REFUSED = ("limit:seven_day: the weekly subscription limit is reached (the CLI refused the call); "
           "resets 2026-11-19 14:00 UTC")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True, timeout=30)


def _events(name: str) -> list[dict]:
    lines = [json.loads(line) for line in (DATA / name).read_text().splitlines() if line.strip()]
    return [line for line in lines if line.get("type") != "result"]


def _ir(name: str, app: Path) -> dict:
    return {
        "schemaVersion": "agentic-layer-v1", "name": name, "description": "subscription usage",
        "codebase": {"root": str(app), "defaultBranch": "main", "paths": {"protected": []}, "commands": []},
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "environments": [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 28200,
                          "portsPerRun": 0, "keep": "never", "status": "stable"}],
        "reviews": [{"id": "human", "kind": "human", "status": "stable", "reviewers": [], "draft": False, "bodyCmd": ""}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "storage": {"id": "local", "kind": "local", "status": "stable", "dir": "artefacts"},
        "promptInputs": [{"id": "cli", "kind": "cli", "status": "stable", "allowStdin": False}],
        "budget": {"id": "limits", "kind": "limits", "status": "stable", "runUsd": None, "stepUsd": None, "dayUsd": None,
                   "limitStopPercent": 0, "onLimit": "stop", "limitWaitMinutes": 300},
        "commands": [{"name": "plan", "template": "plan-chore", "modelSet": "heavy", **TEXT},
                     {"name": "build", "template": "build", "modelSet": "base", **TEXT}],
        "hops": [], "artefacts": [], "voice": None,
        "adws": [{"name": "work", "agent": "mock", "review": "human", "environment": ["wt"], "input": ["cli"],
                  "trigger": ["manual"], "classes": [],
                  "steps": [{"name": "plan", "kind": "run", "command": "plan", "modelSet": "inherit"},
                            {"name": "build", "kind": "run", "command": "build", "modelSet": "inherit"},
                            {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}]}],
    }


def _layer(root: Path, name: str, app: Path) -> Path:
    layer = root / f"{name}.al"
    (layer / "commands").mkdir(parents=True)
    for command in ("plan", "build"):
        (layer / "commands" / f"{command}.md").write_text(f"# {command}\n$ARGUMENTS\n")
    (layer / "agentic-layer.json").write_text(json.dumps(_ir(name, app), indent=1))
    return layer


def build_site(root: Path) -> dict[str, Path]:
    app = root / "app"
    app.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(app)], check=True)
    (app / "app.py").write_text("print('hello')\n")
    _git(app, "add", ".")
    _git(app, "commit", "-qm", "init")
    sub = _layer(root, "sub", app)
    (sub / "mock").mkdir()
    subscription = _events("subscription.jsonl")
    scripts = {
        PASSED: {"plan": {"output": "planned", "events": subscription, "cost": 0.5},
                 "build": {"files": {"feature.txt": "x\n"}, "output": "built", "events": subscription, "cost": 0.5}},
        STOPPED: {"plan": {"output": "planned", "events": subscription, "cost": 0.5},
                  "build": {"files": {"half.txt": "x\n"}, "events": _events("subscription-rejected.jsonl"),
                            "fail": "agent exited 1"}},
    }
    env = {k: v for k, v in os.environ.items() if not k.startswith("AL_")}
    for run_id, steps in scripts.items():
        for step, script in steps.items():
            (sub / "mock" / f"{step}.json").write_text(json.dumps(script))
        code = (f"import sys; sys.path.insert(0, {str(RUNTIME)!r}); from adw_modules.runner import main; "
                f"sys.exit(main('work', __import__('pathlib').Path({str(sub)!r}), ['Add a thing', '--adw-id', {run_id!r}]))")
        subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=120)
    key = _layer(root, "key", app)
    (key / "agents").mkdir()
    (key / "agents" / dashboard.USAGE_FILE).write_text(json.dumps(
        {"version": 1, "billing": "api-key", "api_key_source": "ANTHROPIC_API_KEY", "windows": {}}))
    fresh = _layer(root, "fresh", app)
    return {"sub": sub, "key": key, "fresh": fresh}


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    layers = build_site(tmp_path_factory.mktemp("usage"))
    running = start_server(["--layer", str(layers["sub"]), "--layer", str(layers["key"]), "--layer", str(layers["fresh"])])
    try:
        yield running, layers
    finally:
        stop_server(running)


@pytest.fixture()
def usage_file(site):
    """The sub layer's status file; whatever a test writes into it is undone afterwards."""
    _running, layers = site
    path = layers["sub"] / "agents" / dashboard.USAGE_FILE
    original = path.read_bytes()
    try:
        yield path
    finally:
        path.write_bytes(original)


def _snapshot(running) -> dict:
    for _ in range(50):
        snap = running.json("/api/snapshot")
        if len(snap["runs"]) == 2 and len(snap["layers"]) == 3:
            return snap
        time.sleep(0.1)
    raise AssertionError(snap)


def _usage(running, name: str = "sub") -> dict:
    return next(layer for layer in _snapshot(running)["layers"] if layer["name"] == name)["usage"]


def _hostile(path: Path) -> None:
    path.write_text(json.dumps({"version": 1, "billing": "subscription", "windows": {
        HOSTILE: {"window": HOSTILE, "status": "allowed", "used": "lots", "resets_at": "2020-01-01T00:00:00+00:00",
                  "at": "2026-11-15T18:57:41.294Z", "run": "../../etc", "step": "build"}}}))


def test_runs_were_made_by_the_runtime(site):
    _running, layers = site
    passed = json.loads((layers["sub"] / "agents" / PASSED / "state.json").read_text())
    assert passed["status"] == "passed" and passed["cost"]["billing"] == "subscription"
    stopped = json.loads((layers["sub"] / "agents" / STOPPED / "state.json").read_text())
    assert stopped["usage"]["stopped"]["window"] == "seven_day"
    assert stopped["ship"]["action"] == "handoff"


def test_each_layer_reports_its_usage_state(site):
    running, _layers = site
    usage = _usage(running, "sub")
    assert (usage["state"], usage["billing"], usage["threshold"]) == ("known", "subscription", None)
    windows = {w["window"]: w for w in usage["windows"]}
    assert [w["window"] for w in usage["windows"]] == ["seven_day", "seven_day_overage_included"]
    weekly = windows["seven_day"]
    assert (weekly["label"], weekly["status"], weekly["used"], weekly["resets_at"], weekly["run"], weekly["step"]) == (
        "weekly", "rejected", 1.0, WEEKLY_RESET, STOPPED, "build")
    assert weekly["at"] is not None and weekly["expired"] is (time.time() >= WEEKLY_RESET)
    assert windows["seven_day_overage_included"]["label"] == "seven day overage included"
    assert windows["seven_day_overage_included"]["used"] == 0.6
    key = _usage(running, "key")
    assert (key["state"], key["billing"], key["windows"], key["api_key_source"]) == (
        "api-key", "api-key", [], "ANTHROPIC_API_KEY")
    fresh = _usage(running, "fresh")
    assert fresh == {"state": "none", "billing": "", "windows": [], "threshold": None}


def test_the_declared_threshold_is_part_of_the_layer_usage(tmp_path):
    layer = dashboard.Layer("x", tmp_path, {"budget": {"limitStopPercent": 90, "onLimit": "wait", "limitWaitMinutes": 120}}, None)
    assert layer.usage_threshold() == {"percent": 90, "on_limit": "wait", "wait_minutes": 120}
    assert dashboard.Model.layer_usage(layer, time.time())["threshold"]["percent"] == 90
    for budget in (None, {}, {"limitStopPercent": 0}, {"limitStopPercent": True}, {"limitStopPercent": "90"}):
        assert dashboard.Layer("x", tmp_path, {"budget": budget}, None).usage_threshold() is None
    assert dashboard.Layer("x", tmp_path, {"budget": {"limitStopPercent": 80}}, None).usage_threshold() == {
        "percent": 80, "on_limit": "stop", "wait_minutes": 300}


def test_summary_and_detail_carry_billing_limit_events_and_the_stop(site):
    running, _layers = site
    snap = _snapshot(running)
    key = next(layer for layer in snap["layers"] if layer["name"] == "sub")["key"]
    runs = {r["id"]: r for r in snap["runs"]}
    assert runs[PASSED]["billing"] == "subscription" and runs[PASSED]["usage"]["stopped"] is None
    assert runs[PASSED]["usage"] == {"billing": "subscription", "stopped": None, "waits": 0}
    assert runs[STOPPED]["billing"] == "subscription"
    assert runs[STOPPED]["usage"]["stopped"]["window"] == "seven_day"
    detail = running.json(f"/api/run?layer={key}&run={STOPPED}")
    stop = detail["usage"]["stopped"]
    assert (stop["cause"], stop["window"], stop["used"], stop["resets_at"], stop["step"], stop["reason"]) == (
        "reached", "seven_day", 1.0, WEEKLY_RESET, "build", REFUSED)
    limits = [b for b in detail["timeline"]["bars"] if b["kind"] == "limit"]
    assert {b["step"] for b in limits} == {"plan", "build"} and all(b["level"] == 1 for b in limits)
    build = [(b["label"], b["status"]) for b in limits if b["step"] == "build"]
    assert ("stopped: weekly limit", "failed") in build
    assert ("weekly limit: 100% used · rejected", "done") in build
    assert [b["label"] for b in limits if b["step"] == "plan"] == [
        "weekly limit: 57% used · allowed", "seven day overage included limit: 60% used · allowed"]
    assert detail["handoff"]["reasons"][0].startswith("run-stopped:build: limit:seven_day:")
    assert {a["dir"]: a["billing"] for a in detail["agents"]} == {"plan": "subscription", "build": "subscription"}
    # limit bars are marks, not work: they are in no total
    totals = detail["timeline"]["totals"]
    agent_bars = [b for b in detail["timeline"]["bars"] if b["level"] == 1 and b["kind"] == "agent"]
    assert totals["agent"] == pytest.approx(sum(b["end"] - b["start"] for b in agent_bars))


def test_billing_comes_from_the_transcript_for_runs_from_before_this_feature(site, tmp_path):
    _running, layers = site
    assert dashboard.transcript_rows(DATA / "subscription.jsonl")[1].billing == "subscription"
    assert dashboard.transcript_rows(DATA / "api-key.jsonl")[1].billing == "api-key"
    assert dashboard.transcript_rows(DATA / "api-key.jsonl")[1].as_dict()["billing"] == "api-key"
    for source, expected in (("none", "subscription"), ("apiKeyHelper", "api-key"), ("/login managed key", "api-key"),
                             (None, ""), ("oauth", "")):
        assert dashboard.billing_of(source) == expected
    old = tmp_path / "old.al"
    shutil.copytree(layers["sub"], old, ignore=shutil.ignore_patterns("trees"))
    run = old / "agents" / PASSED
    state = json.loads((run / "state.json").read_text())
    del state["usage"]
    del state["cost"]["billing"]
    for call in state["agent_calls"]:
        del call["billing"]
    (run / "state.json").write_text(json.dumps(state))
    (run / "events.jsonl").unlink()                      # a run from before run events, too
    layer = dashboard.load_layer(old, set())
    summary = dashboard.RunModel(layer, PASSED, dashboard.AggCache()).summary()
    assert summary["billing"] == "subscription" and summary["usage"] is None


def test_a_changed_usage_file_is_published_to_the_page(site, usage_file):
    running, _layers = site
    _snapshot(running)
    reader = SSEReader(running.port, "/api/events")
    try:
        reader.wait_for("snapshot")
        time.sleep(0.5)                                  # the watcher has seen the file as it is
        data = json.loads(usage_file.read_text())
        data["windows"]["seven_day"].update(used=0.42, status="allowed")
        usage_file.write_text(json.dumps(data))

        def changed(payload: str) -> bool:
            entry = next(layer for layer in json.loads(payload) if layer["name"] == "sub")
            return any(w["window"] == "seven_day" and w["used"] == 0.42 for w in entry["usage"]["windows"])
        reader.wait_for("layers", changed)
    finally:
        reader.close()


def test_a_corrupt_or_hostile_usage_file_is_data_not_markup(site, usage_file):
    running, _layers = site
    usage_file.write_text("not json")
    assert _usage(running) == {"state": "none", "billing": "", "windows": [], "threshold": None}
    usage_file.write_text(json.dumps(["a", "list"]))
    assert _usage(running)["state"] == "none"
    _hostile(usage_file)
    usage = _usage(running)
    assert usage["state"] == "known"
    window = usage["windows"][0]
    assert window["window"] == HOSTILE and window["label"] == HOSTILE     # a plain string; the page escapes it
    assert window["used"] is None and window["run"] == "" and window["expired"] is True
    status, _headers, body = running.get("/api/snapshot")
    assert status == 200 and b"<img" not in body                           # and the JSON itself carries no markup
    usage_file.write_text(json.dumps({"windows": {"seven_day": {"used": 7, "resets_at": 12, "at": None, "run": 5},
                                                  "five_hour": "soon", "a": {"used": True}}}))
    listed = _usage(running)["windows"]
    assert [(w["window"], w["used"], w["resets_at"], w["run"], w["expired"]) for w in listed] == [
        ("seven_day", None, None, "", False), ("a", None, None, "", False)]


@pytest.mark.browser
def test_the_page_shows_the_subscription_usage_panel(site, usage_file):
    from test_browser import CHROME, open_, until, playwright_api  # noqa: F401 — skips without Chrome
    running, _layers = site
    snap = _snapshot(running)
    keys = {layer["name"]: layer["key"] for layer in snap["layers"]}
    shots = os.environ.get("AL_DASHBOARD_SHOTS")
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, headless=True)
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.base = running.base  # type: ignore[attr-defined]
        open_(page, "/now")
        weekly = f".usage-layer[data-layer='{keys['sub']}'] .usage-window[data-window='seven_day']"
        until(page, f"!!document.querySelector(\"{weekly}\")")
        assert page.locator("h2", has_text="Subscription usage").count() == 1
        assert "not a live meter" in page.locator("main").inner_text()
        text = page.locator(weekly).inner_text()
        for needle in ("weekly", "100%", "rejected", "as of", STOPPED):
            assert needle in text, (needle, text)
        assert "resets" in text or "reset time passed" in text          # the fixture's reset is a fixed date
        assert page.evaluate(f"document.querySelector(\"{weekly} .usage-bar i\").getBoundingClientRect().width") > 0
        assert page.locator(f"{weekly} .badge.limit").inner_text() == "rejected"
        assert page.locator(f"{weekly} a").get_attribute("href") == f"#/run/{keys['sub']}/{STOPPED}"
        other = page.locator(f".usage-layer[data-layer='{keys['sub']}'] .usage-window[data-window='seven_day_overage_included']")
        assert "seven day overage included" in other.inner_text() and "60%" in other.inner_text()
        assert "API key: not applicable" in page.locator(f".usage-layer[data-layer='{keys['key']}']").inner_text()
        assert "no data yet" in page.locator(f".usage-layer[data-layer='{keys['fresh']}']").inner_text()
        assert "API-equivalent" in page.locator(".strip").inner_text()
        if shots:
            page.screenshot(path=str(Path(shots) / "subscription-usage-panel.png"), full_page=True)

        open_(page, f"/run/{keys['sub']}/{STOPPED}")
        until(page, "!!document.querySelector('.limit-block') && !!document.querySelector('.tl-bar.k-limit')")
        assert "cost (api-equivalent)" in page.locator(".figures").first.inner_text().lower()   # the label is upper-cased by CSS
        assert page.locator(".badge.limit").first.inner_text() == "limit stop: weekly"
        block = page.locator(".limit-block").inner_text()
        assert "stopped by the weekly limit" in block and REFUSED in block and "100%" in block
        assert "API-equivalent: these calls ran on a subscription" in page.locator("main").inner_text()
        if shots:
            page.screenshot(path=str(Path(shots) / "subscription-usage-run.png"), full_page=True)

        # hostile content in the status file is shown as text
        open_(page, "/now")
        until(page, f"!!document.querySelector(\"{weekly}\")")
        _hostile(usage_file)
        until(page, "document.querySelector('.usage').innerText.includes('<img src=x onerror=alert(1)>')")
        assert page.evaluate("document.querySelector('.usage img') === null")
        hostile = page.locator(f".usage-layer[data-layer='{keys['sub']}'] .usage-window").inner_text()
        assert "share used: not reported by the CLI" in hostile and "reset time passed; no newer report" in hostile
        assert page.locator(f".usage-layer[data-layer='{keys['sub']}'] .usage-window a").count() == 0
        browser.close()
        assert not errors, errors
