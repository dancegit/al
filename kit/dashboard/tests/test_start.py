"""Starting work from the dashboard: GET /api/startable (the catalog, from the compiled IR) and POST
/api/start (behind the same guard as the Models write, then the layer's own adws/dispatch.py
--start). Real dashboard server, real `uv run` of the layer's scripts, the mock agent, and an
in-memory Forgejo for the issue race with the layer's own poller (trigger_cron.Scheduler)."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from conftest import Running, start_server, stop_server

RUNTIME = Path(__file__).resolve().parents[2] / "runtime"
RUN_TIMEOUT = 180


def _cron():
    spec = importlib.util.spec_from_file_location("trigger_cron_for_start", RUNTIME / "triggers" / "trigger_cron.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def key() -> str:
    return uuid.uuid4().hex


def headers(running: Running, **changes: str | None) -> dict[str, str]:
    out = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{running.port}",
           "Host": f"127.0.0.1:{running.port}", "X-AL-Dashboard": "1"}
    for name, value in changes.items():
        name = name.replace("_", "-")
        if value is None:
            out.pop(name, None)
        else:
            out[name] = value
    return out


def post(running: Running, body: Any, path: str = "/api/start", **changes: str | None) -> tuple[int, dict]:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    status, _headers, data = running.get(path, headers(running, **changes), method="POST", body=raw)
    try:
        return status, json.loads(data) if data else {}
    except ValueError:
        return status, {"raw": data.decode("utf-8", "replace")}


def run_dirs(layer: Path) -> list[str]:
    agents = layer / "agents"
    return sorted(p.name for p in agents.iterdir() if p.is_dir() and len(p.name) == 8) if agents.is_dir() else []


def wait_state(layer: Path, run_id: str, timeout: float = RUN_TIMEOUT) -> dict:
    path = layer / "agents" / run_id / "state.json"
    deadline = time.monotonic() + timeout
    state: dict = {}
    while time.monotonic() < deadline:
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            state = {}
        if state.get("status") in ("passed", "failed") and state.get("ended"):
            return state
        time.sleep(0.3)
    log = (layer / "agents" / run_id / "launch.log")
    raise AssertionError(f"run {run_id} did not finish: {state.get('status')}\n"
                         + (log.read_text()[-3000:] if log.exists() else "no launch.log"))


def wait_api_status(running: Running, layer: str, run_id: str, want: str = "passed") -> dict:
    deadline = time.monotonic() + RUN_TIMEOUT
    detail: dict = {}
    while time.monotonic() < deadline:
        status, _h, body = running.get(f"/api/run?layer={layer}&run={run_id}")
        if status == 200:
            detail = json.loads(body)
            if detail["summary"]["status"] == want:
                return detail
            if detail["summary"]["status"] in ("passed", "failed", "died"):
                break
        time.sleep(0.3)
    raise AssertionError(f"/api/run {run_id}: {detail.get('summary', {}).get('status')}")


@pytest.fixture()
def site(start_site):
    running = start_server(["--layer", str(start_site["notes"]), "--layer", str(start_site["bare"]),
                            "--layer", str(start_site["old"])])
    try:
        yield running, start_site
    finally:
        stop_server(running)


def plan_build(prompt: str = "Add a word count", **extra: Any) -> dict:
    return {"layer": "notes", "target": {"kind": "workflow", "name": "plan-build"}, "prompt": prompt,
            "request_key": key(), **extra}


# ---------------------------------------------------------------- the catalog


def test_startable_lists_workflows_and_commands_from_the_ir(site):
    running, layers = site
    data = running.json("/api/startable?layer=notes")
    workflows = {w["name"]: w for w in data["workflows"]}
    assert list(workflows) == ["plan-build", "ship-it"]
    assert workflows["plan-build"]["summary"] == "plans, builds"
    assert workflows["plan-build"]["ships_unattended"] == "" and workflows["plan-build"]["issue_trigger"] is True
    assert workflows["ship-it"]["summary"] == "plans, checks test, two reviewers, merges without a person"
    assert workflows["ship-it"]["ships_unattended"] == "merge"
    assert workflows["ship-it"]["review"] == {"id": "gate", "kind": "auto", "ship": "merge",
                                              "allowedClasses": ["chore"], "reviewers": 2}
    assert workflows["ship-it"]["steps"] == ["plan", "test", "review", "ship"]
    commands = {c["name"]: c for c in data["commands"]}
    assert list(commands) == ["classify", "plan-chore", "plan-bug", "plan-feature", "review", "implement"]
    assert commands["plan-chore"] == {
        "name": "plan-chore", "kind": "command", "description": "", "argument_hint": "[prompt]", "tier": "heavy",
        "output": "path", "summary": "one step: plan-chore (heavy tier), writes a plan; never ships"}
    assert commands["implement"]["kind"] == "hop"
    assert commands["implement"]["argument_hint"] == "[plan-or-prompt-path]"
    assert commands["classify"]["tier"] == "base"
    assert data["forge"] == "forgejo" and data["issue"] == {"available": True, "trigger": ["plan-build"]}
    assert data["tiers"] == ["base", "heavy"] and data["collisions"] == []
    assert data["start"] == {"enabled": True, "why": ""}
    assert running.json("/api/startable?layer=bare")["issue"] == {"available": False, "trigger": []}
    assert running.get("/api/startable?layer=nope")[0] == 404
    # an ADW added to the compiled definition appears after the next poll, without any code change
    path = layers["notes"] / "agentic-layer.json"
    ir = json.loads(path.read_text())
    ir["adws"].append({**ir["adws"][0], "name": "plan-chore", "description": "same name as a command"})
    path.write_text(json.dumps(ir))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        data = running.json("/api/startable?layer=notes")
        if len(data["workflows"]) == 3:
            break
        time.sleep(0.3)
    assert [w["name"] for w in data["workflows"]] == ["plan-build", "ship-it", "plan-chore"]
    assert data["collisions"] == ["plan-chore"]


# ---------------------------------------------------------------- starting


def test_start_workflow_plan_build_and_see_its_run(site):
    running, layers = site
    status, data = post(running, plan_build())
    assert status == 202, data
    run_id = data["run_id"]
    assert data["url"] == f"#/run/notes/{run_id}" and data["existing"] is False and data["queued"] == ""
    detail = wait_api_status(running, "notes", run_id)
    assert detail["summary"]["adw"] == "plan-build"
    state = wait_state(layers["notes"], run_id)
    assert state["prompt"] == "Add a word count" and [s["status"] for s in state["steps"]] == ["passed", "passed"]


def test_start_slash_plan_chore_is_a_single_command_run_with_its_plan_as_a_document(site):
    running, layers = site
    status, data = post(running, {"layer": "notes", "text": "/plan-chore tidy the readme", "request_key": key()})
    assert status == 202, data
    run_id = data["run_id"]
    detail = wait_api_status(running, "notes", run_id)
    state = wait_state(layers["notes"], run_id)
    assert state["adw"] == "command:plan-chore" and state["prompt"] == "tidy the readme" and state["ship"] == {}
    plan = f"specs/plan-{run_id}-plan-chore.md"
    assert state["plan"].endswith(plan)
    documents = {d["path"]: d for d in detail["documents"]}
    assert documents[plan]["kind"] == "plan"
    assert [n["step"] for n in detail["graph"]["nodes"]] == ["plan-chore"]   # drawn from the recorded definition
    status, _h, body = running.get(f"/api/doc?layer=notes&run={run_id}&path={plan}")
    assert status == 200 and "tidy the readme" in json.loads(body)["text"]


def test_same_request_key_twice_starts_one_run(site):
    running, layers = site
    body = plan_build("Count the notes")
    first = post(running, body)
    again = post(running, body)
    assert first[0] == again[0] == 202
    assert again[1]["run_id"] == first[1]["run_id"] and again[1]["existing"] is True
    concurrent = plan_build("Count the notes twice at once")
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(post(running, concurrent))) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [r[0] for r in results] == [202, 202]
    assert results[0][1]["run_id"] == results[1][1]["run_id"]
    assert sorted({r[1]["existing"] for r in results}) == [False, True]
    assert len(run_dirs(layers["notes"])) == 2
    for run_id in run_dirs(layers["notes"]):
        wait_state(layers["notes"], run_id)


def test_double_start_guard_asks_then_starts_when_confirmed(site):
    running, layers = site
    status, first = post(running, plan_build("Rename the field"))
    assert status == 202
    again = plan_build("Rename the field")
    status, asked = post(running, again)
    assert status == 409 and asked["confirm"] == "duplicate" and asked["previous"] == first["run_id"]
    assert first["run_id"] in asked["message"] and len(run_dirs(layers["notes"])) == 1
    status, confirmed = post(running, {**again, "confirm_duplicate": True})
    assert status == 202 and confirmed["run_id"] != first["run_id"]
    for run_id in (first["run_id"], confirmed["run_id"]):
        wait_state(layers["notes"], run_id)


def test_shipping_workflow_needs_confirm_ships(site):
    running, layers = site
    body = {"layer": "notes", "target": {"kind": "workflow", "name": "ship-it"}, "prompt": "Fix a typo",
            "request_key": key(), "class": "chore"}
    status, asked = post(running, body)
    assert status == 409 and asked["confirm"] == "ships" and "merges without a person" in asked["message"]
    assert run_dirs(layers["notes"]) == []
    status, started = post(running, {**body, "confirm_ships": True})
    assert status == 202, started
    wait_state(layers["notes"], started["run_id"])


def test_run_survives_the_dashboard_stopping(start_site):
    running = start_server(["--layer", str(start_site["notes"])])
    try:
        status, data = post(running, plan_build("Outlive the page"))
    finally:
        stop_server(running)
    assert status == 202, data
    assert wait_state(start_site["notes"], data["run_id"])["status"] == "passed"


# ---------------------------------------------------------------- refusals


def _token_query(running, site_layers):
    return post(running, plan_build(), path="/api/start?token=t0k")


REFUSALS = [
    ("unknown name", [], lambda r, s: post(r, {**plan_build(), "target": {"kind": "workflow", "name": "nope"}}), 400),
    ("unknown slash name", [], lambda r, s: post(r, {"layer": "notes", "text": "/nope x", "request_key": key()}), 400),
    ("unknown layer", [], lambda r, s: post(r, {**plan_build(), "layer": "elsewhere"}), 404),
    ("wrong origin", [], lambda r, s: post(r, plan_build(), Origin="http://evil.example"), 403),
    ("no X-AL-Dashboard", [], lambda r, s: post(r, plan_build(), X_AL_Dashboard=None), 403),
    ("token in the query string", ["--token", "t0k"], _token_query, 401),
    ("form content type", [], lambda r, s: post(r, b"layer=notes", Content_Type="application/x-www-form-urlencoded"), 415),
    ("bad JSON", [], lambda r, s: post(r, b"{not json"), 400),
    ("body over 64 KiB", [], lambda r, s: post(r, plan_build("x" * (64 * 1024))), 413),
    ("bad request key", [], lambda r, s: post(r, {**plan_build(), "request_key": "x"}), 400),
    ("prompt not a string", [], lambda r, s: post(r, {**plan_build(), "prompt": ["x"]}), 400),
    ("empty prompt for a workflow", [], lambda r, s: post(r, plan_build("  ")), 400),
    ("bad class", [], lambda r, s: post(r, plan_build(**{"class": "epic"})), 400),
    ("bad tier", [], lambda r, s: post(r, plan_build(model_set="turbo")), 400),
    ("model id with a space", [], lambda r, s: post(r, plan_build(models={"heavy": "a b"})), 400),
    ("issue on forge none", [], lambda r, s: post(r, {**plan_build(), "layer": "bare", "issue": "start"}), 400),
    ("trigger for a command", [], lambda r, s: post(r, {"layer": "notes", "text": "/plan-chore x",
                                                        "request_key": key(), "issue": "trigger"}), 400),
    ("--read-only", ["--read-only"], lambda r, s: post(r, plan_build()), 403),
    ("--no-start", ["--no-start"], lambda r, s: post(r, plan_build()), 403),
    ("--in-container", ["--in-container"], lambda r, s: post(r, plan_build()), 403),
    ("layer without launch.py", [], lambda r, s: post(r, {**plan_build(), "layer": "old"}), 409),
    ("GET", [], lambda r, s: (r.get("/api/start")[0], {}), 405),
    ("PUT", [], lambda r, s: (r.get("/api/start", headers(r), method="PUT", body=b"{}")[0], {}), 405),
]


@pytest.mark.parametrize("why, flags, call, code", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_refusals(start_site, why, flags, call, code):
    running = start_server(["--layer", str(start_site["notes"]), "--layer", str(start_site["bare"]),
                            "--layer", str(start_site["old"]), *flags])
    try:
        status, data = call(running, start_site)
    finally:
        stop_server(running)
    assert status == code, (why, data)
    for name in ("notes", "bare", "old"):
        assert run_dirs(start_site[name]) == [], why
    if code in (400, 403, 404, 409) and why not in ("GET", "PUT"):
        assert data.get("error"), data
    if why == "--no-start":
        assert data["error"] == "the dashboard was started with --no-start"
    if why == "--in-container":
        assert "--allow-start-in-container" in data["error"]
    if why == "layer without launch.py":
        assert "run `al gen old`" in data["error"]
    if why == "unknown slash name":
        assert "/plan-build" in data["error"] and "/implement" in data["error"]


def test_in_container_start_is_allowed_with_its_flag(start_site):
    running = start_server(["--layer", str(start_site["notes"]), "--in-container", "--allow-start-in-container"])
    try:
        body = {"layer": "notes", "target": {"kind": "workflow", "name": "ship-it"}, "prompt": "x",
                "request_key": key()}
        status, data = post(running, body)   # reaches the launcher: it asks for the ship confirmation
    finally:
        stop_server(running)
    assert status == 409 and data["confirm"] == "ships"
    assert run_dirs(start_site["notes"]) == []


# ---------------------------------------------------------------- the issue race with the layer's poller


def _poller(layer: Path):
    cron = _cron()
    launched: list[list[str]] = []

    class Proc:
        def poll(self):
            return None

    def launch(argv, log, env):
        launched.append(argv)
        return Proc()

    return cron.Scheduler(layer, "cron", 3, launch=launch, out=lambda _m: None), launched


def test_issue_start_now_then_poller_never_starts_it(site, fake_forgejo):
    running, layers = site
    status, data = post(running, plan_build("Log it and start it", issue="start", **{"class": "chore"}))
    assert status == 202, data
    scheduler, launched = _poller(layers["notes"])
    assert scheduler.poll_once() == 0 and launched == []
    body, = fake_forgejo.bodies()
    assert f"<!-- al:claimed run={data['run_id']} -->" in body and data["issue"]["number"] == 1
    state = wait_state(layers["notes"], data["run_id"])
    assert state["status"] == "passed" and state["source"] == "issue:1"
    assert "al:claimed" not in state["prompt"] and state["prompt"].startswith("Log it and start it")
    assert scheduler.poll_once() == 0 and launched == []


def test_poller_first_then_issue_start_now_still_one_run(site, fake_forgejo):
    running, layers = site
    scheduler, launched = _poller(layers["notes"])
    assert scheduler.poll_once() == 0
    status, data = post(running, plan_build("Poll first", issue="start"))
    assert status == 202, data
    assert scheduler.poll_once() == 0 and launched == []
    assert run_dirs(layers["notes"]) == [data["run_id"]]
    wait_state(layers["notes"], data["run_id"])
    assert scheduler.poll_once() == 0 and run_dirs(layers["notes"]) == [data["run_id"]]


def test_issue_trigger_mode_starts_nothing_and_the_poller_starts_it_once(site, fake_forgejo):
    running, layers = site
    status, data = post(running, plan_build("Let the trigger do it", issue="trigger", **{"class": "bug"}))
    assert status == 202 and data["run_id"] == "" and data["url"] == "", data
    assert data["message"] == "the layer's trigger will start it"
    assert run_dirs(layers["notes"]) == []
    body, = fake_forgejo.bodies()
    assert "al:claimed" not in body and body.endswith("adw: plan-build")
    labels = {label["name"] for label in fake_forgejo._issue_view(fake_forgejo.issues[1])["labels"]}
    assert labels == {"adw", "bug"}
    scheduler, launched = _poller(layers["notes"])
    assert scheduler.poll_once() == 1
    assert scheduler.poll_once() == 0
    argv, = launched
    assert argv[argv.index("--source") + 1] == "issue:1" and argv[3].endswith("adw_plan-build.py")


# ---------------------------------------------------------------- what the page is told


def test_the_page_state_says_why_starting_is_off(start_site):
    cases = [([], {"notes": (True, ""), "old": (False, "generated before starting from the dashboard: run `al gen old`")}),
             (["--read-only"], {"notes": (False, "the dashboard was started with --read-only")}),
             (["--no-start"], {"notes": (False, "the dashboard was started with --no-start")}),
             (["--in-container"], {"notes": (False, "--allow-start-in-container")})]
    for flags, expected in cases:
        running = start_server(["--layer", str(start_site["notes"]), "--layer", str(start_site["old"]), *flags])
        try:
            layers = {layer["key"]: layer["start"] for layer in running.json("/api/snapshot")["layers"]}
        finally:
            stop_server(running)
        for name, (enabled, why) in expected.items():
            assert layers[name]["enabled"] is enabled, (flags, layers[name])
            assert why in layers[name]["why"] if why else layers[name]["why"] == "", (flags, layers[name])


# ---------------------------------------------------------------- start again


def _old_run(layer: Path, run_id: str, status: str = "failed", prompt: str = "", **extra: Any) -> None:
    """A finished run of plan-build as the runtime records it; request.md holds the full prompt."""
    import settle_fixtures

    settle_fixtures.write_run(layer, run_id, adw="plan-build", prompt=prompt[:40], status=status,
                              started="2026-11-10T09:00:00+00:00", ended="2026-11-10T09:01:00+00:00",
                              klass="chore", class_fixed=True,
                              overrides={"tier": "heavy", "models": {}, "from": {"tier": "--model-set"}}, **extra)
    (layer / "agents" / run_id / "request.md").write_text(prompt + "\n")


def _seen(running: Running, run_id: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if running.get(f"/api/run?layer=notes&run={run_id}")[0] == 200 and \
                any(r["id"] == run_id for r in running.json("/api/snapshot")["runs"]):
            return
        time.sleep(0.2)
    raise AssertionError(f"the dashboard never saw {run_id}")


def test_start_again_rebuilds_request_and_passes_replaces(site):
    running, layers = site
    old = "0a1d0a1d"
    prompt = "Add a word count to the notes list, and show it in the footer of every page as well"
    _old_run(layers["notes"], old, prompt=prompt)
    _seen(running, old)
    body = {"layer": "notes", "again": old, "request_key": f"again-{old}"}
    status, data = post(running, body)
    assert status == 202, data
    status, twice = post(running, body)   # a double click starts one run
    assert status == 202 and twice["run_id"] == data["run_id"] and twice["existing"] is True, twice
    new = data["run_id"]
    state = wait_state(layers["notes"], new)
    assert state["replaces"] == old and state["adw"] == "plan-build"
    assert state["prompt"] == prompt   # the full prompt from request.md, not the clipped state.prompt
    assert (state["klass"], state["class_fixed"], state["overrides"]["tier"]) == ("chore", True, "heavy")
    events = [json.loads(line) for line in (layers["notes"] / "agents" / new / "events.jsonl").read_text().splitlines()]
    assert next(e for e in events if e.get("type") == "run.start").get("replaces") == old
    assert sorted(run_dirs(layers["notes"])) == sorted([old, new])
    _seen(running, new)
    status, refused = post(running, {**body, "request_key": key()})   # replaced now: not again
    assert status == 409 and new in refused["error"]


def test_start_again_refuses_passed_or_replaced_run(site):
    running, layers = site
    _old_run(layers["notes"], "0a55ed00", status="passed", prompt="Tidy the notes")
    _old_run(layers["notes"], "0f00f000", prompt="Sort the notes")
    _old_run(layers["notes"], "0e0e0e0e", status="running", prompt="Sort the notes", replaces="0f00f000",
             heartbeat="2026-11-10T09:01:00+00:00")
    for run_id in ("0a55ed00", "0f00f000", "0e0e0e0e"):
        _seen(running, run_id)
    status, data = post(running, {"layer": "notes", "again": "0a55ed00", "request_key": key()})
    assert status == 400 and "only a failed or died run" in data["error"]
    status, data = post(running, {"layer": "notes", "again": "0f00f000", "request_key": key()})
    assert status == 409 and "0e0e0e0e" in data["error"]
    status, data = post(running, {"layer": "notes", "again": "0badc0de", "request_key": key()})
    assert status == 404
    status, data = post(running, {"layer": "notes", "again": "0f00f000", "request_key": key(), "prompt": "x"})
    assert status == 400
    assert sorted(run_dirs(layers["notes"])) == ["0a55ed00", "0e0e0e0e", "0f00f000"]
