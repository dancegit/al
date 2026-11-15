"""End to end: a generated layer's own dashboard, watched in the host's Chrome while real
(mock-agent) runs go through the generated scripts.

    uv run --with pytest --with pyyaml --with jsonschema --with playwright pytest tests/tools/test_dashboard_e2e.py -q

The layer is generated the way tests/integration does it (a private clone of a pinned example
repository, the `layer` fixture of tests/integration/conftest.py, which also checks on teardown that
the clone is exactly as it was). Then `adws/dashboard.py` is started, two runs go through
`adws/adw_plan-build.py` while the page is open, and the page must show them live from their events:
exact check timings, a check log growing while the check runs, and a third run, killed mid-check,
shown as "died". Skipped without Playwright, without Chrome, or when the testbed can be neither
found locally nor fetched.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[2]
INTEGRATION = ROOT / "tests" / "integration"


def _chrome() -> str | None:
    explicit = os.environ.get("AL_DASHBOARD_CHROME")
    if explicit:
        return explicit if Path(explicit).exists() else None
    return next((shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
                 if shutil.which(n)), None)


CHROME = _chrome()
if CHROME is None:
    pytest.skip("no Chrome/Chromium on this machine", allow_module_level=True)

# the integration suite's fixtures (private testbed clone, generated layer, leak checks on teardown)
sys.path.insert(0, str(INTEGRATION))
_spec = importlib.util.spec_from_file_location("al_integration_conftest", INTEGRATION / "conftest.py")
_integration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_integration)  # type: ignore[union-attr]
testbed, layer = _integration.testbed, _integration.layer   # noqa: F401 — pytest fixtures
alit = _integration.alit

pytestmark = pytest.mark.browser
SLOW_CHECK = "echo starting; for i in 1 2 3 4 5 6 7 8 9 10; do echo tick $i; sleep 1; done; echo all done"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def events(lay, run_id: str) -> list[dict]:
    path = lay.dir / "agents" / run_id / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def wait(predicate, timeout: float, what: str, every: float = 0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}")


def new_run_id(lay, known: set[str]) -> str:
    def found():
        ids = {p.name for p in (lay.dir / "agents").glob("*") if len(p.name) == 8 and (p / "state.json").exists()}
        fresh = ids - known
        return next(iter(fresh)) if fresh else None
    run_id = wait(found, 60, "a new run directory")
    known.add(run_id)
    return run_id


def page_text(page, selector: str) -> str:
    return page.evaluate(f"(document.querySelector({json.dumps(selector)}) || {{}}).textContent || ''")


def test_generated_layer_dashboard_shows_runs_live(layer):  # noqa: F811
    lay = layer("flask", cmds={"slow": SLOW_CHECK})
    launcher = lay.dir / "adws" / "dashboard.py"
    assert launcher.is_file() and (lay.dir / "adws" / "dashboard" / "static" / "app.js").is_file()
    port = free_port()
    dashboard = subprocess.Popen(["uv", "run", "--quiet", str(launcher), "--port", str(port), "--poll", "0.3",
                                  "--no-podman"], cwd=lay.dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    procs: list[subprocess.Popen] = []
    try:
        wait(lambda: _healthy(base), 60, "the generated dashboard to answer")
        layer_key = lay.name
        with playwright_api.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROME, headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 860})
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(f"{base}/#/now")
            page.wait_for_selector("#conn.conn-live", timeout=15000)

            # two runs through the generated entry scripts, while the page is open
            known: set[str] = set()
            procs.append(lay.popen_adw("plan-build", "Add a docstring to the app factory"))
            first = new_run_id(lay, known)
            procs.append(lay.popen_adw("plan-build", "Explain the request context in a comment"))
            second = new_run_id(lay, known)
            wait(lambda: page.locator(".run-card").count() >= 1, 30, "a run card in the Now view")

            # follow the first run's check while it runs: its log grows on the page
            wait(lambda: any(e["type"] == "check.start" for e in events(lay, first)), 120, "the first run's check")
            page.goto(f"{base}/#/run/{layer_key}/{first}/step/slow")
            page.wait_for_selector(".log", timeout=15000)
            wait(lambda: "tick 1" in page_text(page, ".log"), 30, "the first lines of the running check")
            early = page_text(page, ".log")
            assert page.locator("[data-role='live-check']").count() == 1, "the page says it follows a running check"
            assert "all done" not in early
            wait(lambda: "tick 6" in page_text(page, ".log"), 30, "the check's log to grow on the page")
            assert len(page_text(page, ".log")) > len(early)

            for proc in procs:
                out, _ = proc.communicate(timeout=300)
                assert proc.returncode == 0, out[-4000:]

            # exact timings: the page's model of the run is the events file's
            detail = page.evaluate(f"fetch('/api/run?layer={layer_key}&run={first}').then(r => r.json())")
            assert detail["summary"]["timing"] == "events" and detail["summary"]["status"] == "passed"
            ev = events(lay, first)
            start = next(e for e in ev if e["type"] == "check.start")
            end = next(e for e in ev if e["type"] == "check.end")
            bar = next(b for b in detail["timeline"]["bars"] if b["id"] == "check:slow.1.log")
            assert bar["start"] == pytest.approx(datetime.fromisoformat(start["ts"]).timestamp(), abs=0.002)
            assert bar["end"] == pytest.approx(datetime.fromisoformat(end["ts"]).timestamp(), abs=0.002)
            assert bar["end"] - bar["start"] >= 9.5 and bar["exit"] == 0
            agents = {a["dir"]: a for a in detail["agents"]}
            assert agents["plan"]["agent_kind"] == "mock" and agents["build"]["agent_kind"] == "mock"
            page.goto(f"{base}/#/run/{layer_key}/{second}")
            page.wait_for_selector(".run-head .pill.s-passed", timeout=20000)

            # a third run is killed mid-check: the page shows it as died, with when it was last seen
            procs.append(lay.popen_adw("plan-build", "This one will be killed"))
            victim = new_run_id(lay, known)
            wait(lambda: any(e["type"] == "check.start" for e in events(lay, victim)), 120, "the victim's check")
            os.killpg(procs[-1].pid, signal.SIGKILL)
            procs[-1].wait(timeout=30)
            state = json.loads((lay.dir / "agents" / victim / "state.json").read_text())
            assert state["status"] == "running" and state["heartbeat"]   # nobody was left to record the end
            page.goto(f"{base}/#/run/{layer_key}/{victim}")
            page.wait_for_selector(".run-head .pill.s-died", timeout=60000)
            assert page.locator(".badge.died").inner_text().startswith("process died · last seen")
            page.goto(f"{base}/#/now")
            page.wait_for_selector("table .pill.s-died", timeout=15000)
            assert not errors, errors
            browser.close()
    finally:
        for proc in procs:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
        os.killpg(dashboard.pid, signal.SIGTERM)
        try:
            dashboard.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(dashboard.pid, signal.SIGKILL)


def _healthy(base: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/healthz", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False
