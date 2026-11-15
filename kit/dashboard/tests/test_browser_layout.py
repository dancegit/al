"""Real-browser tests for layout and live-log rendering: a growing check log is shown by lines
(partial chunks joined), the panel keeps its log when a check settles, a long workflow (the al
layer's own 14-step `sdlc`) fits a 1280-pixel window, and the cost table's totals row is a row."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import fixtures
import fixtures_events
from conftest import start_server, stop_server
from test_browser import CHROME, browser, open_, until, playwright_api  # noqa: F401 — fixtures and skips

pytestmark = pytest.mark.browser

AL_LAYER_IR = Path(__file__).resolve().parents[3] / "agentic" / "agentic-layer.json"


def new_page(browser, base: str):  # noqa: F811
    context = browser.new_context(viewport={"width": 1280, "height": 860})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.base = base  # type: ignore[attr-defined]
    page.errors = errors  # type: ignore[attr-defined]
    return context, page


@pytest.fixture()
def esite(tmp_path: Path):
    paths = fixtures_events.build(tmp_path)
    running = start_server(["--layer", str(paths["layer"])])
    try:
        yield running, paths["agents"]
    finally:
        stop_server(running)


@pytest.fixture()
def epage(browser, esite):  # noqa: F811
    context, page = new_page(browser, esite[0].base)
    yield page
    context.close()
    assert not [e for e in page.errors if "favicon" not in e], page.errors


def log_text(page) -> str:
    return page.evaluate("document.querySelector('.log').textContent")


def test_a_live_check_log_joins_partial_chunks_into_lines(epage, esite):
    """pytest -q writes its dots a few at a time: the page must show them on one line, as the file has them."""
    _running, agents = esite
    log = agents / "e1000002" / "checks" / "test.1.log"   # ends in a partial line: "tests/test_cart.py .."
    open_(epage, "/run/events-shop/e1000002/step/test")
    until(epage, "document.querySelector('.log') && document.querySelector('.log').textContent.includes('test_cart.py ..')")
    for chunk in (".", ".", " [ 66%]\n"):
        time.sleep(1.8)                                   # longer than the server waits before showing a partial line
        with log.open("a") as handle:
            handle.write(chunk)
    until(epage, "document.querySelector('.log').textContent.includes('[ 66%]')")
    time.sleep(0.6)
    assert "collecting ...\ntests/test_cart.py .... [ 66%]\n" in log_text(epage), repr(log_text(epage))


def test_a_settling_check_keeps_its_log_on_screen(epage, esite):
    """When the running check ends, the panel re-renders for the finished attempt; the log the
    reader was looking at must not be emptied and fetched again (it was, for a moment)."""
    _running, agents = esite
    run = agents / "e1000002"
    open_(epage, "/run/events-shop/e1000002/step/test")
    until(epage, "document.querySelector('.log') && document.querySelector('.log').textContent.includes('collecting')")
    with (run / "checks" / "test.1.log").open("a") as handle:
        handle.write("\n3 passed in 0.12s\n")
    until(epage, "document.querySelector('.log').textContent.includes('3 passed')")
    epage.evaluate("document.querySelector('.log').dataset.mark = 'same'")
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    with (run / "events.jsonl").open("a") as handle:
        handle.write(json.dumps({"ts": stamp, "seq": 99, "type": "check.end", "step": "test", "owner": "test",
                                 "phase": "check", "cmd": "test", "attempt": 1, "log": "agents/e1000002/checks/test.1.log",
                                 "exit": 0, "seconds": 70}) + "\n")
    until(epage, "document.querySelector('[data-role=\"live-check\"]') === null", timeout=15000)
    # the settled panel shows the same log element, still holding the text, at once
    assert epage.evaluate("document.querySelector('.log').dataset.mark") == "same"
    assert "3 passed" in log_text(epage)


@pytest.fixture()
def alsite(tmp_path: Path):
    """A layer with this repository's own IR (agentic/agentic-layer.json) and one passed `sdlc` run."""
    ir = json.loads(AL_LAYER_IR.read_text(encoding="utf-8"))
    layer = tmp_path / "al.al"
    fixtures.write(layer / "agentic-layer.json", json.dumps(ir, indent=2))
    sdlc = next(adw for adw in ir["adws"] if adw["name"] == "sdlc")
    records, t = [], 0.0
    for step in sdlc["steps"]:
        skipped = step.get("when", "always") not in ("always", "class-chore")
        records.append(fixtures.rec(step["name"], step["kind"], "skipped" if skipped else "passed", t, t if skipped else t + 5))
        t += 0 if skipped else 5
    run = layer / "agents" / "f1000001"
    fixtures.write(run / "state.json", json.dumps(fixtures.state("f1000001", "sdlc", "A small chore", "passed", 0, t, records)))
    fixtures.write(run / "run.log", "[adw f1000001] sdlc start: class=chore dry_run=False\n")
    running = start_server(["--layer", str(layer)])
    try:
        yield running, sdlc
    finally:
        stop_server(running)


def test_a_fourteen_step_workflow_fits_a_1280_pixel_window(browser, alsite):  # noqa: F811
    running, sdlc = alsite
    assert len(sdlc["steps"]) == 14
    context, page = new_page(browser, running.base)
    try:
        open_(page, f"/run/{json.loads(AL_LAYER_IR.read_text())['name']}/f1000001")
        page.wait_for_selector(".g-node[data-step='ship']")
        wrap = page.locator(".graph-wrap").first
        box = wrap.bounding_box()
        assert page.evaluate("(() => { const w = document.querySelector('.graph-wrap'); return w.scrollWidth <= w.clientWidth + 1; })()"), \
            "the graph needs a sideways scroll"
        for step in sdlc["steps"]:
            node = page.locator(f".g-node[data-step='{step['name']}'] rect.box").first
            rect = node.bounding_box()
            assert rect is not None
            assert box["x"] - 1 <= rect["x"] and rect["x"] + rect["width"] <= box["x"] + box["width"] + 1, step["name"]
            assert rect["width"] >= 100, f"{step['name']} is drawn {rect['width']:.0f}px wide: too small to read"
        label = page.locator(".g-node[data-step='commit'] text.label").first.bounding_box()
        assert label["height"] >= 10, "labels shrank below a readable size"
        page.locator(".g-node[data-step='ship']").first.click()
        page.wait_for_selector(".step-panel h3")
        assert page.locator(".step-panel h3").inner_text() == "ship"
        assert not [e for e in page.errors if "favicon" not in e], page.errors
    finally:
        context.close()


def test_the_cost_tables_totals_row_lines_up_with_its_columns(epage):
    open_(epage, "/run/events-shop/e1000001")
    epage.wait_for_selector("tfoot tr")
    table = epage.locator("table", has=epage.locator("tfoot")).first
    assert table.locator("tfoot tr").evaluate("tr => getComputedStyle(tr).display") == "table-row"
    heads = table.locator("thead th")
    cells = table.locator("tfoot td")
    assert heads.count() == cells.count() == 10
    for index in range(10):
        head, cell = heads.nth(index).bounding_box(), cells.nth(index).bounding_box()
        assert abs(head["x"] - cell["x"]) < 1 and abs(head["width"] - cell["width"]) < 1, index
    assert cells.nth(8).inner_text().startswith("$")
