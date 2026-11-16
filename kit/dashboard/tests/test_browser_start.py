"""The Layers view's "Start work" panel in a real Chrome: `/` completion picks a single command, the
start lands on the new run, and a workflow that merges without a person asks first. Skipped without
Playwright or Chrome (nothing is downloaded). With AL_DASHBOARD_SHOTS=<directory> it saves
start-form.png, start-confirm.png and start-run.png there. Everything on the page is synthetic: the
mock agent, a fake forge, a temporary application."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from conftest import start_server, stop_server
from test_browser import CHROME, browser, open_, until, playwright_api  # noqa: F401 — fixtures and skips

pytestmark = pytest.mark.browser


def shot(page, name: str) -> None:
    directory = os.environ.get("AL_DASHBOARD_SHOTS")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(directory) / name), full_page=True)


@pytest.fixture()
def spage(browser, start_site):  # noqa: F811
    running = start_server(["--layer", str(start_site["notes"])])
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" and "409" not in msg.text else None)
    page.base = running.base  # type: ignore[attr-defined]
    try:
        yield page, start_site["notes"]
    finally:
        context.close()
        stop_server(running)
    assert not [e for e in errors if "favicon" not in e], errors


def wait_finished(layer: Path, run_id: str, timeout: float = 180) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = json.loads((layer / "agents" / run_id / "state.json").read_text())
            if state.get("status") in ("passed", "failed") and state.get("ended"):
                return state
        except (OSError, ValueError):
            pass
        time.sleep(0.3)
    raise AssertionError(f"run {run_id} did not finish")


def test_slash_completion_starts_a_single_command_and_lands_on_the_run(spage):
    page, layer = spage
    open_(page, "/layers")
    page.wait_for_selector(".start-panel .start-prompt")
    page.click(".start-prompt")
    page.keyboard.type("/plan-c")
    page.wait_for_selector(".start-complete li")
    assert page.locator(".start-complete li").first.get_attribute("data-value") == "command:plan-chore"
    page.keyboard.press("Enter")
    assert page.input_value(".start-prompt") == "/plan-chore "
    assert page.input_value(".start-target") == "command:plan-chore"
    page.keyboard.type("tidy the readme")
    assert "one step: plan-chore (heavy tier), writes a plan; never ships" in page.inner_text(".start-summary")
    assert "never merges" in page.inner_text(".start-unattended")
    assert page.locator(".start-class").is_hidden()
    shot(page, "start-form.png")
    page.click(".start-go")
    until(page, "location.hash.startsWith('#/run/notes/')", timeout=30000)
    run_id = page.evaluate("location.hash").rsplit("/", 1)[1]
    state = wait_finished(layer, run_id)
    assert state["status"] == "passed" and state["adw"] == "command:plan-chore" and state["prompt"] == "tidy the readme"
    page.wait_for_selector(".g-node[data-step='plan-chore']", timeout=20000)
    until(page, "document.querySelector(\".g-node[data-step='plan-chore']\").getAttribute('class').includes('st-passed')",
          timeout=30000)
    shot(page, "start-run.png")


def test_a_workflow_that_merges_without_a_person_asks_first(spage):
    page, layer = spage
    open_(page, "/layers")
    page.wait_for_selector(".start-panel .start-target")
    page.select_option(".start-target", "workflow:ship-it")
    assert "merges into the main branch" in page.inner_text(".start-unattended")
    assert page.locator(".start-class").is_visible()
    page.fill(".start-prompt", "Fix the typo in the readme")
    page.click(".start-go")
    page.wait_for_selector(".start-confirm")
    assert "merges without a person" in page.inner_text(".start-confirm .confirm-message")
    shot(page, "start-confirm.png")
    page.click(".confirm-cancel")
    until(page, "document.querySelector('.start-note').textContent === 'not started'")
    agents = layer / "agents"
    assert not agents.exists() or not [p for p in agents.iterdir() if p.is_dir() and len(p.name) == 8]
