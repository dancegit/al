"""Real-browser test of the prepare step: the dashed `prepare` setup node comes first in the graph,
its bar comes first on the timeline, and the step panel shows its failed log. The layer and its failed
run are the ones test_setup_step.py builds.

    uv run --with pytest --with playwright pytest kit/dashboard/tests/test_browser_setup_step.py

Skipped without Playwright or Chrome (nothing is downloaded).
With AL_DASHBOARD_SHOTS=<directory> it saves run-setup-step.png and run-setup-step-log.png there."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import start_server, stop_server
from test_setup_step import NAME, RUN, write_layer

pytest.importorskip("playwright.sync_api")
from test_browser import browser, until  # noqa: E402,F401 — fixtures; skips without Chrome

pytestmark = pytest.mark.browser


def shot(page, name: str) -> None:
    directory = os.environ.get("AL_DASHBOARD_SHOTS")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(directory) / name), full_page=False)


def test_setup_step_is_first_in_graph_and_timeline_and_shows_its_log(browser, tmp_path):
    layer = write_layer(tmp_path)
    running = start_server(["--layer", str(layer)])
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    try:
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"{running.base}/#/run/{NAME}/{RUN}")
        page.wait_for_selector("#conn.conn-live", timeout=10000)
        page.wait_for_selector(".g-node")
        first = page.locator(".g-node").first
        assert "t-setup-node" in first.get_attribute("class").split()
        assert "prepare" in first.text_content()
        page.wait_for_selector("rect.tl-bar")
        bars = [c.split() for c in page.locator("rect.tl-bar").evaluate_all("els => els.map(e => e.getAttribute('class'))")]
        steps = [c for c in bars if not {"k-wait", "k-limit"} & set(c)]
        assert steps and "k-setup" in steps[0] and "st-failed" in steps[0], bars
        shot(page, "run-setup-step.png")
        page.goto(f"{running.base}/#/run/{NAME}/{RUN}/step/prepare")
        page.wait_for_selector(".step-panel")
        until(page, "(document.querySelector('.step-panel') || {}).textContent"
                    "?.includes('no matching distribution for nope==9') || false")
        shot(page, "run-setup-step-log.png")
        assert not errors, errors
    finally:
        context.close()
        stop_server(running)
