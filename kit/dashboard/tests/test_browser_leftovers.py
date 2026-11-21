"""Real-browser test of two things the run graph draws from the model: a step whose agent left
commands running says `leftover commands stopped` on its node, with the note in its title, and the
edge out of a running step into a pending one is dashed `waiting` with a `waiting` label.

    uv run --with pytest --with playwright pytest kit/dashboard/tests/test_browser_leftovers.py

Skipped without Playwright or Chrome (nothing is downloaded). It saves run-leftovers-stopped.png and
run-waiting-edge.png to the test's tmp_path and, with AL_DASHBOARD_SHOTS=<directory>, there too."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
import fixtures_events  # noqa: E402
from conftest import start_server, stop_server  # noqa: E402
from test_browser import browser, open_, until  # noqa: E402,F401 — fixtures; skips without Chrome

pytestmark = pytest.mark.browser

NOTE = "the agent finished; its leftover commands were stopped"


def _shot(page, tmp_path: Path, name: str) -> None:
    page.screenshot(path=str(tmp_path / name), full_page=False)
    directory = os.environ.get("AL_DASHBOARD_SHOTS")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(directory) / name), full_page=False)


@pytest.fixture()
def lsite(tmp_path: Path):
    paths = fixtures_events.build(tmp_path)
    path = paths["agents"] / "e1000001" / "events.jsonl"
    lines = path.read_text().splitlines()
    last = json.loads(lines[-1])
    lines.append(json.dumps({"ts": last["ts"], "seq": last["seq"] + 1, "type": "agent.leftovers", "step": "build",
                             "grace": 30.0, "stopped": [{"pid": 4242, "command": "sleep 300", "where": "host"}]}))
    path.write_text("\n".join(lines) + "\n")
    running = start_server(["--layer", str(paths["layer"])])
    try:
        yield running
    finally:
        stop_server(running)


@pytest.fixture()
def lpage(browser, lsite):  # noqa: F811
    context = browser.new_context(viewport={"width": 1280, "height": 860})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.base = lsite.base  # type: ignore[attr-defined]
    yield page
    context.close()
    assert not [e for e in errors if "favicon" not in e], errors


def test_a_step_whose_agent_left_commands_running_says_so_on_its_node(lpage, tmp_path):
    open_(lpage, "/run/events-shop/e1000001")
    lpage.wait_for_selector(".g-node[data-node='build']")
    until(lpage, "document.querySelector(\".g-node[data-node='build'] text.meta\").textContent"
                 ".includes('leftover commands stopped')", timeout=15000)
    assert NOTE in lpage.locator(".g-node[data-node='build'] title").text_content()
    classify = lpage.locator(".g-node[data-node='classify']")
    assert "leftover commands stopped" not in classify.locator("text.meta").text_content()
    assert NOTE not in classify.locator("title").text_content()
    _shot(lpage, tmp_path, "run-leftovers-stopped.png")


def test_the_edge_out_of_a_running_step_reads_waiting(lpage, tmp_path):
    open_(lpage, "/run/events-shop/e1000002")
    lpage.wait_for_selector(".g-node[data-node='test']")
    until(lpage, "document.querySelectorAll('path.g-edge.flow.waiting').length > 0", timeout=15000)
    labels = lpage.locator("text.g-edge-label.waiting")
    assert labels.count() >= 1
    assert all(text == "waiting" for text in labels.all_text_contents())
    _shot(lpage, tmp_path, "run-waiting-edge.png")
    # a passed run has no waiting edge
    open_(lpage, "/run/events-shop/e1000001")
    lpage.wait_for_selector(".g-node[data-node='build']")
    until(lpage, "document.querySelector(\".g-node[data-node='build'] text.meta\").textContent"
                 ".includes('leftover commands stopped')", timeout=15000)   # e1000001's graph is drawn
    assert lpage.locator("path.g-edge.waiting").count() == 0
    assert lpage.locator("text.g-edge-label.waiting").count() == 0
