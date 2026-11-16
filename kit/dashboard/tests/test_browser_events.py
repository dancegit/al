"""Real-browser tests for runs that carry events.jsonl, liveness and fan-out parents
(fixtures_events.py): a running check's log followed live, a died run, models on the graph."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import fixtures_events
from conftest import start_server, stop_server
from test_browser import CHROME, browser, open_, until, playwright_api  # noqa: F401 — fixtures and skips

pytestmark = pytest.mark.browser


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
    running, _agents = esite
    context = browser.new_context(viewport={"width": 1280, "height": 860})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.base = running.base  # type: ignore[attr-defined]
    yield page
    context.close()
    assert not [e for e in errors if "favicon" not in e], errors


def test_a_running_checks_log_is_followed_live_then_settles(epage, esite):
    _running, agents = esite
    run = agents / "e1000002"
    open_(epage, "/run/events-shop/e1000002/step/test")
    epage.wait_for_selector(".log")
    until(epage, "document.querySelector('.log') && document.querySelector('.log').textContent.includes('collecting')")
    assert epage.locator("[data-role='live-check']").count() == 1
    assert "running" in epage.locator(".chip.active").inner_text()
    log = run / "checks" / "test.1.log"
    with log.open("a") as handle:                       # the check writes more while the page is open
        handle.write(" [ 66%]\ntests/test_totals.py .\n")
    until(epage, "document.querySelector('.log').textContent.includes('test_totals.py')")
    with log.open("a") as handle:
        handle.write("3 passed in 0.12s\n")
    until(epage, "document.querySelector('.log').textContent.includes('3 passed')")
    # the check ends: its events and state say so, and the page settles on the finished attempt
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    with (run / "events.jsonl").open("a") as handle:
        handle.write(json.dumps({"ts": stamp, "seq": 99, "type": "check.end", "step": "test", "owner": "test",
                                 "phase": "check", "cmd": "test", "attempt": 1, "log": "agents/e1000002/checks/test.1.log",
                                 "exit": 0, "seconds": 70}) + "\n")
    state = json.loads((run / "state.json").read_text())
    state["checks"] = {"test": {"cmd": "pytest -q", "exit": 0, "log": "agents/e1000002/checks/test.1.log"}}
    (run / "state.json").write_text(json.dumps(state))
    until(epage, "document.querySelector('[data-role=\"live-check\"]') === null", timeout=15000)
    until(epage, "[...document.querySelectorAll('.badge')].some(b => b.textContent === 'exit 0')", timeout=15000)
    assert "3 passed" in epage.locator(".log").inner_text()


def test_a_died_run_is_shown_as_died_with_when_it_was_last_seen(epage):
    open_(epage, "/run/events-shop/e1000003")
    epage.wait_for_selector(".run-head .pill.s-died")
    badge = epage.locator(".badge.died").inner_text()
    assert badge.startswith("process died · last seen") and "min ago" in badge
    assert "st-died" in epage.locator(".g-node[data-step='build']").get_attribute("class")
    open_(epage, "/now")
    epage.wait_for_selector(".run-card")
    cards = epage.locator(".run-card").all_inner_texts()
    assert any("Speed up the cart tests" in c for c in cards) and not any("Rewrite the checkout" in c for c in cards)
    assert epage.locator("table tr", has_text="Rewrite the checkout").locator(".pill.s-died").count() == 1


def test_graph_and_costs_name_the_model_and_waits_are_exact(epage):
    open_(epage, "/run/events-shop/e1000001")
    epage.wait_for_selector(".g-node[data-step='build']")
    assert "opus-5-5" in epage.locator(".g-node[data-step='build'] text.sub").text_content()
    assert "claude-opus-5-5" in epage.locator(".g-node[data-step='build'] title").text_content()
    epage.wait_for_selector("table")
    row = epage.locator("tr", has_text="lint-resolve-1")
    assert "claude-opus-5-5" in row.inner_text()
    assert "waiting for a slot" in epage.locator(".totals").first.inner_text()


def test_a_candidate_links_to_its_parent(epage):
    open_(epage, "/run/events-shop/e1000004")
    link = epage.locator("a.badge", has_text="candidate 2 of e1000099")
    link.wait_for()
    assert "(race)" in link.inner_text() and link.get_attribute("href") == "#/run/events-shop/e1000099"


def test_setup_bars_show_on_the_run_page(epage, esite):
    _running, agents = esite
    fixtures_events.add_setup_runs(agents.parent)
    open_(epage, "/run/events-shop/e1000005")
    epage.wait_for_selector(".tl-bar.k-setup")
    assert epage.locator(".tl-bar.k-setup").count() == 2
    titles = " ".join(epage.locator(".tl-row title").all_text_contents())
    assert f"building sandbox image {fixtures_events.SETUP_RECIPE} (312 MB) · setup · done" in titles
    assert "preflight: missing command:test · setup · failed" in titles
    epage.locator(".tl-bar.k-setup").first.click()                    # a setup bar is not a link
    assert epage.url.endswith("/run/events-shop/e1000005")
    from test_browser_documents import shot
    shot(epage, "run-setup-bars.png")


def test_check_skips_show_next_to_passed(epage, esite):
    _running, agents = esite
    run = agents / "e1000001"
    reasons = [["no podman", 2], ["needs network", 1]]
    lines = []
    for line in (run / "events.jsonl").read_text().splitlines():
        event = json.loads(line)
        if event["type"] == "check.end" and event.get("step") == "test":
            event.update(skipped=3, skip_reasons=reasons, skip_source="pytest")
        lines.append(json.dumps(event))
    (run / "events.jsonl").write_text("\n".join(lines) + "\n")
    state = json.loads((run / "state.json").read_text())
    state["checks"]["test"]["skips"] = {"skipped": 3, "passed": 1, "failed": 0, "reasons": reasons,
                                        "unexplained": 0, "source": "pytest"}
    (run / "state.json").write_text(json.dumps(state))
    open_(epage, "/run/events-shop/e1000001")
    epage.wait_for_selector(".g-node[data-step='test']")
    until(epage, "document.querySelector(\".g-node[data-step='test']\").textContent.includes('3 skipped')", timeout=15000)
    open_(epage, "/run/events-shop/e1000001/step/test")
    badge = epage.locator("[data-role='skips']")
    badge.wait_for()
    assert badge.inner_text() == "3 skipped" and "warn" in badge.get_attribute("class")
    assert badge.get_attribute("title") == "no podman ×2\nneeds network ×1"
    assert epage.locator(".badge", has_text="exit 0").count() >= 1   # next to the passed exit
    from test_browser_documents import shot
    shot(epage, "check-skips.png")
