"""Real-browser tests: Playwright drives the host's Google Chrome (nothing is downloaded).

    uv run --with pytest --with playwright pytest kit/dashboard/tests/test_browser.py

Skipped when Playwright is not installed or no Chrome/Chromium binary is found
(AL_DASHBOARD_CHROME overrides the search)."""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Iterator

import pytest

import fixtures
from conftest import start_server, stop_server

playwright_api = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.browser


def chrome_path() -> str | None:
    explicit = os.environ.get("AL_DASHBOARD_CHROME")
    if explicit:
        return explicit if Path(explicit).exists() else None
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


CHROME = chrome_path()
if CHROME is None:
    pytest.skip("no Chrome/Chromium on this machine", allow_module_level=True)


@pytest.fixture(scope="module")
def browser() -> Iterator:
    with playwright_api.sync_playwright() as p:
        instance = p.chromium.launch(executable_path=CHROME, headless=True)
        yield instance
        instance.close()


@pytest.fixture()
def site(tmp_path: Path):
    root = tmp_path / "fx"
    fixtures.build(root)
    running = start_server(["--layer", str(root / "demo-shop.al")])
    try:
        yield running, root / "demo-shop.al" / "agents"
    finally:
        stop_server(running)


@pytest.fixture()
def page(browser, site):
    running, _agents = site
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.base = running.base  # type: ignore[attr-defined]
    page.errors = errors  # type: ignore[attr-defined]
    yield page
    context.close()
    assert not [e for e in errors if "favicon" not in e], errors


def open_(page, route: str) -> None:
    page.goto(f"{page.base}/#{route}")
    page.wait_for_selector("#conn.conn-live", timeout=10000)


def until(page, expression: str, timeout: int = 10000) -> None:
    """Poll a JS expression with page.evaluate. Playwright's wait_for_function evaluates strings
    inside the page, which the dashboard's CSP (no unsafe-eval) rightly refuses."""
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        if page.evaluate(expression):
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for: {expression}")


# ------------------------------------------------------------------------------ views

def test_now_view_shows_the_running_run_and_held_slots(page, site):
    import fcntl
    _running, agents = site
    handle = os.open(agents / ".slots" / "runs.0.lock", os.O_RDWR)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)   # what a run's process does while it runs
        open_(page, "/now")
        card = page.locator(".run-card", has_text="Cache the catalog for 60 seconds")
        card.wait_for()
        assert "build" in card.locator(".doing").inner_text() and "thinking" in card.locator(".doing").inner_text()
        assert "al-demo-shop-a1000006" in card.locator(".container").inner_text()
        runs_row = page.locator(".pool", has_text="demo-shop").locator(".pool-row", has_text="runs")
        until(page, "[...document.querySelectorAll('.pool-row')].some(r => r.textContent.startsWith('runs') && r.textContent.endsWith('1/2'))")
        assert runs_row.locator("i.on").count() == 1
    finally:
        os.close(handle)
    until(page, "[...document.querySelectorAll('.pool-row')].some(r => r.textContent.startsWith('runs') && r.textContent.endsWith('0/2'))",
                           timeout=10000)   # released: the page follows


def test_runs_filters_search_and_sort(page):
    open_(page, "/runs")
    page.wait_for_selector("tbody tr[data-key]")
    assert page.locator("tbody tr[data-key]").count() == 14
    page.select_option("select[aria-label='statuses']", "failed")
    assert page.locator("tbody tr[data-key]").count() == 5   # four zte runs and one fan-out candidate
    assert "status=failed" in page.url
    page.select_option("select[aria-label='statuses']", "")
    page.fill("input[aria-label='search runs']", "coupons")
    until(page, "document.querySelectorAll('tbody tr[data-key]').length === 1")
    assert "a1000004" in page.locator("tbody tr[data-key]").inner_text()
    page.fill("input[aria-label='search runs']", "")
    until(page, "document.querySelectorAll('tbody tr[data-key]').length === 14")
    page.click("th:has-text('cost')")
    first = page.locator("tbody tr[data-key]").first.get_attribute("data-key")
    assert first == "demo-shop/a1000004"   # most expensive first ($1.24)
    totals = page.locator(".strip").inner_text()
    assert "PASS RATE" in totals.upper() and "MEDIAN COST" in totals.upper()


def test_graph_node_opens_the_step_and_transcript(page):
    open_(page, "/run/demo-shop/a1000001")
    page.wait_for_selector(".g-node[data-step='build']")
    assert page.locator(".g-node[data-step='plan-bug']").get_attribute("class").find("st-skipped") >= 0
    page.click(".g-node[data-step='build']")
    page.wait_for_selector(".ev.k-tool")
    assert "/step/build" in page.url
    titles = page.locator(".ev.k-tool .title").all_inner_texts()
    assert "edit tests/test_cart.py" in titles and any(t.startswith("$ uv run --locked pytest") for t in titles)
    assert page.locator(".ev.k-tool .diff .add", has_text="def test_two_items").count() == 1
    assert page.locator(".ev.k-final").inner_text().count("$0.18") == 1


def test_transcript_filters_search_and_raw_json(page):
    open_(page, "/run/demo-shop/a1000001/step/build")
    page.wait_for_selector(".ev.k-tool")
    page.click("button[data-filter='errors']")
    visible = page.locator(".ev:not(.hidden)")
    assert visible.count() == 1 and "pytest" in visible.inner_text() and "NameError" in visible.inner_text()
    page.click("button[data-filter='errors']")
    page.select_option("select[aria-label='tool']", "Edit")
    assert page.locator(".ev:not(.hidden)").count() == 2
    page.select_option("select[aria-label='tool']", "")
    page.keyboard.press("/")
    page.keyboard.type("Item")
    until(page, "document.querySelectorAll('.feed mark').length > 0")
    assert all("item" in t.lower() for t in page.locator(".ev:not(.hidden)").all_inner_texts())
    assert "q=Item" in page.url
    page.fill("input[aria-label='search transcript']", "")
    until(page, "document.querySelectorAll('.feed mark').length === 0")
    row = page.locator(".ev.k-init")
    assert row.is_hidden()                       # system events are hidden by default
    page.click("button[data-filter='system']")
    row.hover()
    row.locator(".raw-button").click()
    until(page, "document.querySelector('.ev.k-init .rawjson').textContent.includes('\"subtype\": \"init\"')")


def test_deep_link_round_trip(page):
    route = "/run/demo-shop/a1000001/step/build?agent=build&q=cart&hide=think"
    open_(page, route)
    until(page, "document.querySelectorAll('.feed mark').length > 0")
    assert page.input_value("input[aria-label='search transcript']") == "cart"
    assert "on" not in page.get_attribute("button[data-filter='think']", "class")
    page.reload()
    until(page, "document.querySelectorAll('.feed mark').length > 0")
    assert page.input_value("input[aria-label='search transcript']") == "cart"
    assert page.locator(".g-node.selected").get_attribute("data-step") == "build"


def test_check_attempts_ansi_and_resolve_link(page):
    open_(page, "/run/demo-shop/a1000002/step/test")
    page.wait_for_selector(".chip[data-source='test.3.log']")
    assert [c.strip() for c in page.locator(".chip").all_inner_texts()] == ["test #1", "test-resolve-1", "test #2", "test-resolve-2", "test #3"]
    page.wait_for_selector(".log span")
    colour = page.locator(".log span", has_text="FAILED").first.evaluate("e => e.style.color")
    assert colour in {"rgb(248, 113, 113)", "#f87171"}
    page.click(".chip[data-source='test.1.log']")
    page.wait_for_selector("a:has-text('resolved by test-resolve-1')")
    page.click("a:has-text('resolved by test-resolve-1')")
    page.wait_for_selector(".ev.k-text")
    assert "agent=test-resolve-1" in page.url


def test_outcome_handoff_and_review_rounds(page):
    open_(page, "/run/demo-shop/a1000002")
    page.wait_for_selector(".why")
    assert "failed after 3 attempts" in page.locator(".why").inner_text()
    open_(page, "/run/demo-shop/a1000004")
    page.wait_for_selector(".reviewer")
    text = page.locator(".card", has_text="Outcome").inner_text()
    assert "round 1: not approved" in text and "round 2: approved" in text
    assert "Expired coupons are still accepted" in text
    patch = page.locator(".g-edge.patch")
    assert patch.count() == 1


def test_artefacts_tab(page):
    open_(page, "/run/demo-shop/a1000001/artefacts")
    page.wait_for_selector(".thumb img")
    until(page, "document.querySelector('.thumb img').naturalWidth > 0")
    assert page.locator("video").count() == 1 and page.locator(".frames img").count() == 1
    page.click(".thumb")
    assert page.locator("#overlay img").is_visible()
    page.keyboard.press("Escape")
    assert page.locator("#overlay").is_hidden()


def test_keyboard_navigation(page):
    open_(page, "/runs")
    page.wait_for_selector("tbody tr[data-key]")
    page.keyboard.press("j")
    page.keyboard.press("j")
    selected = page.locator("tbody tr.selected").get_attribute("data-key")
    page.keyboard.press("Enter")
    page.wait_for_selector(".run-head")
    assert selected.split("/")[1] in page.url
    page.keyboard.press("Escape")
    until(page, "location.hash.startsWith('#/runs')")
    page.keyboard.press("3")
    page.wait_for_selector(".layer-card")


def test_layer_view(page):
    open_(page, "/layer/demo-shop")
    page.wait_for_selector(".adw-section")
    assert page.locator(".adw-section").count() == 4
    page.click("button:has-text('plain words')")
    assert "toy shop" in page.locator("#overlay").inner_text()


def test_hostile_content_is_shown_as_text_and_never_runs(page):
    hostile = '<script>window.__pwned=1</script><img src=x onerror="window.__pwned=2">'
    open_(page, "/run/demo-shop/a1000007")
    page.wait_for_selector(".run-head")
    assert hostile in page.locator(".run-head .prompt").inner_text()
    open_(page, "/run/demo-shop/a1000007/step/build")
    page.wait_for_selector(".ev.k-text")
    assert hostile in page.locator(".ev.k-text").first.inner_text()
    page.locator(".result-head").first.click()
    assert "<style>body{display:none}</style>" in page.locator(".result pre").first.inner_text()
    open_(page, "/run/demo-shop/a1000007/step/plan-chore?tab=prompt")
    page.wait_for_selector(".textblock details")
    assert hostile in page.locator(".textblock").inner_text()
    open_(page, "/run/demo-shop/a1000007/step/lint")
    page.wait_for_selector(".log span")
    assert hostile in page.locator(".log").inner_text()
    assert page.evaluate("window.__pwned") is None
    assert page.locator("img[src='x']").count() == 0 and page.locator("main script").count() == 0
    assert page.locator("a[href^='javascript']").count() == 0


# ------------------------------------------------------------------------------ live

def test_live_updates_without_reload(page, site):
    _running, agents = site
    raw = agents / "a1000006" / "build" / "raw.jsonl"
    os.utime(raw)   # the agent is mid-write: its last line has no newline yet
    open_(page, "/run/demo-shop/a1000006/step/build")
    page.wait_for_selector(".ev.k-tool")
    page.evaluate("window.__sameDocument = true")
    assert "on" in page.get_attribute("button[data-role='follow']", "class")   # a running step follows
    assert page.locator(".ev.k-corrupt").count() == 0
    with raw.open("a") as handle:
        handle.write(fixtures.PARTIAL_EDIT[fixtures.PARTIAL_SPLIT:] + "\n")
        handle.write(json.dumps({"type": "assistant", "timestamp": "2026-11-01T11:24:00Z",
                                 "message": {"id": "msg_live", "content": [{"type": "text", "text": "Live line appended by the test"}]}}) + "\n")
    page.wait_for_selector(".ev.k-text:has-text('Live line appended by the test')", timeout=10000)
    assert page.locator(".ev.k-tool .title", has_text="edit shop/catalog.py").count() == 1
    # follow keeps the newest row in view
    until(page, "(() => { const f = document.querySelector('.feed'); return f.scrollHeight - f.scrollTop - f.clientHeight < 60; })()")
    page.keyboard.press("f")
    assert "on" not in page.get_attribute("button[data-role='follow']", "class")
    page.keyboard.press("f")
    # the run moves on: build passes, lint starts; the graph recolours in place
    state_path = agents / "a1000006" / "state.json"
    state = json.loads(state_path.read_text())
    state["steps"][-1].update(status="passed", ended=fixtures.iso(5100))
    state["steps"].append({"name": "lint", "kind": "check", "status": "running", "attempts": 1,
                           "started": fixtures.iso(5100), "ended": "", "detail": ""})
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, state_path)
    page.wait_for_selector(".g-node.st-passed[data-step='build']", timeout=10000)
    page.wait_for_selector(".g-node.st-running[data-step='lint']", timeout=10000)
    assert page.evaluate("window.__sameDocument") is True
    # and the Now view says what it is doing
    page.click("nav a[data-view='now']")
    page.wait_for_selector(".run-card .doing:has-text('running check lint')", timeout=10000)
    assert page.evaluate("window.__sameDocument") is True


def test_reconnects_after_the_server_restarts(page, site):
    running, agents = site
    open_(page, "/now")
    page.evaluate("window.__sameDocument = true")
    stop_server(running)
    page.wait_for_selector("#conn.conn-wait", timeout=10000)
    again = start_server(["--layer", str(agents.parent), "--port", str(running.port)])
    try:
        page.wait_for_selector("#conn.conn-live", timeout=15000)   # EventSource retried on its own
        assert page.evaluate("window.__sameDocument") is True
        page.locator(".run-card", has_text="Cache the catalog").wait_for()
    finally:
        stop_server(again)
