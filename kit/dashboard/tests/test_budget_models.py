"""Model tiers, spending limits and script steps on the dashboard, from runs the real runtime
made (kit/runtime, mock agent with scripted costs, real git): a run a budget limit stopped, and
a run of a workflow whose step is the team's own script."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import start_server, stop_server

RUNTIME = Path(__file__).resolve().parents[2] / "runtime"
STOPPED, SCRIPTED = "aa000001", "aa000002"
TEXT = {"output": "text", "description": "", "file": "", "focus": [], "fabric": [], "relevant": [], "validation": []}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True, timeout=30)


def build_site(root: Path) -> Path:
    app = root / "app"
    app.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(app)], check=True)
    (app / "adws").mkdir()
    (app / "adws" / "legacy.sh").write_text("#!/bin/sh\necho \"legacy ran: $*\"\n")
    (app / "adws" / "legacy.sh").chmod(0o755)
    _git(app, "add", ".")
    _git(app, "commit", "-qm", "init")
    layer = root / "shop.al"
    (layer / "commands").mkdir(parents=True)
    for name in ("plan", "build"):
        (layer / "commands" / f"{name}.md").write_text(f"# {name}\n$ARGUMENTS\n")
    (layer / "mock").mkdir()
    (layer / "mock" / "plan.json").write_text(json.dumps({"output": "planned", "cost": 1.5}))
    (layer / "mock" / "build.json").write_text(json.dumps({"files": {"half.txt": "x\n"}, "output": "built", "cost": 1.5}))
    ir = {
        "schemaVersion": "agentic-layer-v1", "name": "shop", "description": "budget and tiers",
        "codebase": {"root": str(app), "defaultBranch": "main", "paths": {"protected": []},
                     "commands": [{"name": "legacy-run", "run": "./adws/legacy.sh", "timeout": 30, "description": ""}]},
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "environments": [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 28100,
                          "portsPerRun": 0, "keep": "never", "status": "stable"}],
        "reviews": [{"id": "human", "kind": "human", "status": "stable", "reviewers": [], "draft": False, "bodyCmd": ""}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "storage": {"id": "local", "kind": "local", "status": "stable", "dir": "artefacts"},
        "promptInputs": [{"id": "cli", "kind": "cli", "status": "stable", "allowStdin": False}],
        "budget": {"id": "limits", "kind": "limits", "status": "stable", "runUsd": 2.5, "stepUsd": None, "dayUsd": 50},
        "commands": [{"name": "plan", "template": "plan-chore", "modelSet": "heavy", **TEXT},
                     {"name": "build", "template": "build", "modelSet": "base", **TEXT}],
        "hops": [], "artefacts": [], "voice": None,
        "adws": [{"name": "work", "agent": "mock", "review": "human", "environment": ["wt"], "input": ["cli"],
                  "trigger": ["manual"], "classes": [],
                  "steps": [{"name": "plan", "kind": "run", "command": "plan", "modelSet": "inherit"},
                            {"name": "build", "kind": "run", "command": "build", "modelSet": "inherit"},
                            {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}]},
                 {"name": "legacy", "agent": "mock", "review": "human", "environment": ["wt"], "input": ["cli"],
                  "trigger": ["manual"], "classes": [],
                  "steps": [{"name": "run-legacy", "kind": "script", "cmd": "legacy-run", "args": ["$prompt"], "timeout": 0},
                            {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}]}],
    }
    (layer / "agentic-layer.json").write_text(json.dumps(ir, indent=1))
    env = {k: v for k, v in os.environ.items() if not k.startswith("AL_")}
    for adw, prompt, run_id in (("work", "Add a thing", STOPPED), ("legacy", "tidy up", SCRIPTED)):
        code = (f"import sys; sys.path.insert(0, {str(RUNTIME)!r}); from adw_modules.runner import main; "
                f"sys.exit(main({adw!r}, __import__('pathlib').Path({str(layer)!r}), [{prompt!r}, '--adw-id', {run_id!r}]))")
        subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=120)
    return layer


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    layer = build_site(tmp_path_factory.mktemp("budget"))
    running = start_server(["--layer", str(layer)])
    try:
        yield running, layer
    finally:
        stop_server(running)


def _snapshot(running) -> dict:
    import time
    for _ in range(50):
        snap = running.json("/api/snapshot")
        if len(snap["runs"]) == 2:
            return snap
        time.sleep(0.1)
    raise AssertionError(snap)


def test_runs_were_made_by_the_runtime(site):
    _running, layer = site
    stopped = json.loads((layer / "agents" / STOPPED / "state.json").read_text())
    assert stopped["budget"]["stopped"]["reason"] == "budget:run: spent $3.00 of $2.50"
    assert json.loads((layer / "agents" / SCRIPTED / "state.json").read_text())["status"] == "passed"


def test_summary_and_layer_show_limits_spending_and_the_budget_stop(site):
    running, _layer = site
    snap = _snapshot(running)
    layer = snap["layers"][0]
    assert layer["budget"] == {"run": 2.5, "step": None, "day": 50.0, "today": 3.0, "stops_today": 1}
    runs = {r["id"]: r for r in snap["runs"]}
    stopped = runs[STOPPED]
    assert stopped["cost"] == 3.0
    assert stopped["budget"]["run"] == 2.5 and stopped["budget"]["spent"] == 3.0
    assert stopped["budget"]["stopped"] == {"limit": "run", "spent": 3.0, "of": 2.5, "step": "build",
                                            "reason": "budget:run: spent $3.00 of $2.50"}
    assert stopped["outcome"]["ship"] == "handoff"
    assert runs[SCRIPTED]["budget"]["stopped"] is None


def test_detail_shows_the_tier_of_every_agent_call_and_the_script_step(site):
    running, _layer = site
    key = _snapshot(running)["layers"][0]["key"]
    detail = running.json(f"/api/run?layer={key}&run={STOPPED}")
    tiers = {a["dir"]: (a["tier"], a["tier_from"]) for a in detail["agents"]}
    assert tiers == {"plan": ("heavy", "command"), "build": ("base", "command")}
    assert detail["budget"]["stopped"]["limit"] == "run"
    assert detail["handoff"]["reasons"] == ["run-stopped:build: budget:run: spent $3.00 of $2.50"]
    scripted = running.json(f"/api/run?layer={key}&run={SCRIPTED}")
    node = next(n for n in scripted["graph"]["nodes"] if n["step"] == "run-legacy")
    assert (node["type"], node["sub"]) == ("script", "$ legacy-run")
    assert scripted["scripts"]["run-legacy"]["exit"] == 0
    assert scripted["scripts"]["run-legacy"]["args"] == ["tidy up"]


@pytest.mark.browser
def test_the_page_shows_budget_tiers_and_script_nodes(site):
    from test_browser import CHROME, open_, until, playwright_api  # noqa: F401 — skips without Chrome
    running, _layer = site
    key = _snapshot(running)["layers"][0]["key"]
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, headless=True)
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.base = running.base  # type: ignore[attr-defined]
        open_(page, "/")
        until(page, "!!document.querySelector('.stat.budget-stat')")
        stat = page.locator(".stat.budget-stat").inner_text().lower()
        assert "budget today" in stat and "$3.00 of $50.00" in stat and "$2.50/run" in stat and "1 budget stop" in stat
        open_(page, "/runs")
        until(page, "!!document.querySelector('.badge.budget')")
        assert "budget stop: run" in page.locator(".badge.budget").first.inner_text()
        open_(page, f"/run/{key}/{STOPPED}")
        until(page, "!!document.querySelector('.budget-block')")
        block = page.locator(".budget-block").inner_text()
        assert "stopped by the run limit" in block and "budget:run: spent $3.00 of $2.50" in block
        assert "of $2.50 run limit" in page.locator(".figures").first.inner_text()
        until(page, "document.body.innerText.includes('mock · heavy')")
        open_(page, f"/run/{key}/{SCRIPTED}")
        until(page, "!!document.querySelector(\".g-node.t-script-node[data-step='run-legacy']\")")
        page.locator(".g-node[data-step='run-legacy']").click()
        until(page, "!!document.querySelector('.script-log pre') && document.querySelector('.script-log pre').textContent.includes('legacy ran: tidy up')")
        browser.close()
        assert not errors, errors
