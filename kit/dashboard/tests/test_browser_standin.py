"""The stand-in on the page, in a real browser: the panel in each mode, a suggestion with its three
answers, the decisions on a run's page, the live log with the header counts, and the items that
waited already. Playwright drives the host's Chrome (nothing is downloaded); skipped without one.

    uv run --with pytest --with playwright pytest kit/dashboard/tests/test_browser_standin.py

With AL_DASHBOARD_SHOTS=<directory> the tests save standin-panel-off.png, standin-panel-suggest.png,
standin-panel-auto.png, standin-suggestion.png, standin-timeline.png, standin-log.png and
standin-existing-items.png there."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import standin_fixtures as sf
from conftest import start_server, stop_server
from standin_fixtures import post
from test_browser import browser, open_, until  # noqa: F401 — the module's browser fixture; skips without Chrome
from test_start import key

pytestmark = pytest.mark.browser
RUN, OTHER = "5a0d1e01", "5a0d1e02"


def shot(page, name: str) -> None:
    directory = os.environ.get("AL_DASHBOARD_SHOTS")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(directory) / name), full_page=True)


@pytest.fixture()
def bare(start_site, monkeypatch) -> Path:
    monkeypatch.delenv("AL_DECIDER", raising=False)
    sf.stops(start_site["bare"])
    return start_site["bare"]


@pytest.fixture()
def opened(browser, bare):  # noqa: F811
    """open(*flags) → (page, running): a dashboard over the layer `bare` and a page that fails the test
    on any script error."""
    started: list = []
    errors: list[str] = []

    def start(*flags: str):
        running = start_server(["--layer", str(bare), *flags])
        context = browser.new_context(viewport={"width": 1280, "height": 1000})
        page = context.new_page()
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" and "409" not in msg.text else None)
        page.base = running.base  # type: ignore[attr-defined]
        started.append((context, running))
        return page, running

    try:
        yield start
    finally:
        for context, running in started:
            context.close()
            stop_server(running)
    assert not [e for e in errors if "favicon" not in e], errors


def panel(page):
    return page.locator('.standin-panel[data-layer="bare"]')


def test_panel_in_each_mode(opened, bare):
    sf.declare(bare, "off", may=["resume", "restart", "settle"], never=["promote"], maxActionsPerRun=4)
    page, running = opened()
    open_(page, "/now")
    box = panel(page)
    box.wait_for(timeout=10000)
    assert box.get_attribute("data-mode") == "off"
    assert box.locator('.mode-row[data-mode="off"] input').is_checked()
    for mode, line in (("off", "a person reads every hand-over"), ("suggest", "it proposes, you answer"),
                       ("auto", "it takes the decisions ticked below and proposes the others")):
        assert line in box.locator(f'.mode-row[data-mode="{mode}"]').inner_text()
    kinds = box.locator(".kind-row")
    assert [kinds.nth(i).get_attribute("data-kind") for i in range(kinds.count())] == [
        "resume", "restart", "answer", "integrate", "settle", "land", "retry-job"]      # promote is in #:never
    for kind, checked, enabled in (("resume", True, True), ("settle", True, True), ("land", False, False)):
        box_ = box.locator(f'.kind-row[data-kind="{kind}"] input')
        assert (box_.is_checked(), box_.is_enabled()) == (checked, enabled), kind
    assert box.locator('.kind-row[data-kind="land"]').get_attribute("title") == "not in the layer's #:may"
    assert "never (neither taken nor proposed): promote" in box.inner_text()
    assert box.locator(".limits-text").inner_text() == "at most 4 actions per run · 12 per hour"
    assert box.locator(".counts").inner_text() == "handled today 0 · waiting on you 0"
    assert box.locator("button.pause").inner_text() == "Pause"
    shot(page, "standin-panel-off.png")
    for mode in ("suggest", "auto"):
        panel(page).locator(f'.mode-row[data-mode="{mode}"] input').check()
        until(page, f"document.querySelector('.standin-panel[data-layer=\"bare\"]').dataset.mode === '{mode}'")
        assert json.loads((bare / "agents" / "settings" / "decider.json").read_text())["mode"] == mode
        assert "from: local setting" in panel(page).inner_text()
        shot(page, f"standin-panel-{mode}.png")
    # the scope and the pause are a click each
    panel(page).locator('.kind-row[data-kind="restart"] input').uncheck()
    until(page, "!document.querySelector('.standin-panel[data-layer=\"bare\"] .kind-row[data-kind=\"restart\"] input').checked"
                " && document.querySelector('.standin-panel .standin-note').textContent.includes('alone it may: resume, settle')")
    assert json.loads((bare / "agents" / "settings" / "decider.json").read_text())["may"] == ["resume", "settle"]
    panel(page).locator("button.pause").click()
    until(page, "document.querySelector('.standin-panel[data-layer=\"bare\"] button.pause').textContent === 'Continue'")
    assert json.loads((bare / "agents" / "settings" / "decider.json").read_text())["paused"] is True
    assert panel(page).locator(".badge", has_text="paused").count() == 1
    # under --read-only the panel shows and says why it cannot switch
    page, _running = opened("--read-only", "--port", "0")
    open_(page, "/now")
    panel(page).wait_for(timeout=10000)
    assert not panel(page).locator('.mode-row[data-mode="off"] input').is_enabled()
    assert "the dashboard was started with --read-only" in panel(page).inner_text()


def test_a_suggestion_and_its_three_actions(opened, bare):
    ready = bare.parent / "ready"
    sf.mock_decision(bare, "resume", summary="The check waits for the site; run it again once it is ready.",
                     explanation="The check `ready` failed because the site was not up. Nothing in the plan needs a person.")
    page, running = opened()
    assert post(running, {"layer": "bare", "mode": "suggest"}, path="/api/decider")[0] == 200
    status, data = post(running, {"layer": "bare", "target": {"kind": "workflow", "name": "stops"},
                                  "prompt": "Add a note about the orchard", "request_key": key()}, path="/api/start")
    assert status == 202, data
    run = data["run_id"]
    sf.wait_for(lambda: (r := sf.run_row(running, "bare", run)) and r.get("suggestion"), why="a suggestion")
    # the runs list names the proposal, and the header counts the run
    open_(page, "/runs")
    until(page, "document.querySelector('td.reason .proposal') !== null")
    assert page.locator("td.reason .proposal").inner_text().startswith("proposes: resume from ready — The check waits")
    assert page.locator('[data-count="needs-you"]').inner_text() == "1"
    assert "1 with a suggestion" in page.locator('[data-count="needs-you"]').get_attribute("title")
    assert "(1)" in page.locator(".chip.sug").inner_text()
    assert "1 needs you · 1 suggestion" in page.locator("#crumbs").inner_text()
    # the run page: the proposal with its three answers
    open_(page, f"/run/bare/{run}")
    card = page.locator(".suggestion-card")
    card.wait_for(timeout=10000)
    text = card.inner_text()
    assert "null" not in text and "undefined" not in text
    assert "the stand-in proposes" in text.lower()
    for needle in ("resume from ready", "The check waits for the site", "Nothing in the plan needs a person",
                   "80% sure", "it read: why it stopped", "Go ahead", "Yes, with this change", "No, I decide"):
        assert needle in text, needle
    assert "suggest (layer default)" in page.locator(".outcome-block", has_text="Status").first.inner_text()
    shot(page, "standin-suggestion.png")
    card.locator("button.with-change").click()                    # the text field opens; nothing is sent yet
    assert card.locator("textarea.suggestion-change").is_visible()
    ready.touch()
    card.locator("button.go-ahead").click()
    state = sf.wait_for(lambda: (st := sf.state_of(bare, run)).get("status") == "passed" and st.get("ended") and st,
                        timeout=180, why="the resumed run passes")
    assert state["decision"]["answers"][0] == {**state["decision"]["answers"][0], "kind": "go", "via": "dashboard"}
    until(page, "document.querySelector('.suggestion-card') === null", timeout=30000)
    until(page, "document.querySelector('[data-count=\"needs-you\"]').hidden", timeout=30000)   # the header count drops
    until(page, "document.querySelector('.decisions-card') !== null", timeout=30000)
    assert "answer: go" in page.locator(".decisions-card").inner_text()


def test_decisions_on_the_run_page(opened, bare):
    directory = sf.write_run(bare, RUN, decider="auto")
    state = sf.state_of(bare, RUN)
    (directory / "decide").mkdir()
    (directory / "decide" / "prompt.md").write_text("# Stand in for a person\n")
    (directory / "decide" / "raw.jsonl").write_text(json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.08, "num_turns": 1, "result": "x"}) + "\n")
    inside = sf.iso(__import__("datetime").datetime.fromisoformat(state["ended"]) - __import__("datetime").timedelta(seconds=10))
    sf.record(bare,
              sf.entry(RUN, id="ee000001", at=inside, via="run", mode="auto", status="acted", auto=True,
                       outcome="stopped again for the same reason", summary="The check looks flaky; run it again.",
                       why="The check failed on a timeout, not on the change."),
              sf.entry(RUN, id="ee000002", at=state["ended"], via="loop", mode="auto", kind="settle", action="settle",
                       settle_as="abandoned", status="suggested", summary=sf.HOSTILE),
              {"id": "ee000003", "at": sf.ago(5), "type": "answer", "who": "person", "via": "dashboard", "run": RUN,
               "answer": "reject", "kind": "settle", "action": "settle", "status": "refused"})
    page, _running = opened()
    open_(page, f"/run/bare/{RUN}")
    card = page.locator(".decisions-card")
    card.wait_for(timeout=10000)
    rows = card.locator("tbody tr")
    assert rows.count() == 3
    first = rows.nth(0).inner_text()
    for needle in ("stand-in", "automatic", "resume", "acted", "The check failed on a timeout", "stopped again for the same reason"):
        assert needle in first, needle
    assert rows.nth(0).locator(".badge.auto").count() == 1 and rows.nth(1).locator(".badge.auto").count() == 0
    assert "person" in rows.nth(2).inner_text() and "answer: reject" in rows.nth(2).inner_text()
    assert sf.HOSTILE in rows.nth(1).inner_text()                 # shown as text
    assert page.evaluate("window.__pwned === undefined") and card.locator("img").count() == 0
    assert page.locator(".tl-mark").count() == 3 and page.locator(".tl-mark.person").count() == 1
    assert page.locator(".cost-standin").inner_text().strip() == "(stand-in $0.080)"
    assert "auto (--decider)" in page.locator(".outcome-block", has_text="Status").first.inner_text()
    shot(page, "standin-timeline.png")


def test_the_live_log_and_the_header_counts(opened, bare):
    sf.write_run(bare, RUN)
    sf.write_run(bare, OTHER)
    sf.with_suggestion(bare, OTHER, action="settle", kind="settle", settle_as="abandoned", from_step="",
                       summary="Nobody needs this any more.")
    sf.record(bare,
              {"id": "ff000001", "at": sf.ago(300), "type": "mode", "who": "person", "via": "dashboard", "mode": "auto",
               "may": ["resume"], "paused": False, "set": ["mode"]},
              sf.entry(RUN, id="ff000002", at=sf.ago(120), mode="auto", status="acted", auto=True, outcome="resuming from ready"),
              sf.entry(RUN, id="ff000003", at=sf.ago(60), mode="auto", status="acted", auto=True, outcome=sf.HOSTILE),
              sf.entry(RUN, id="ff000004", at=sf.ago(30), mode="auto", status="escalated",
                       reason="the stand-in acted 2 times on this run (limit 2)"))
    (bare / "agents" / "settings").mkdir(parents=True, exist_ok=True)
    (bare / "agents" / "settings" / "decider.json").write_text(json.dumps({"version": 1, "mode": "auto"}))
    page, _running = opened("--no-stand-in")
    open_(page, "/now")
    box = panel(page)
    box.wait_for(timeout=10000)
    rows = box.locator(".standin-log tbody tr")
    assert rows.count() == 5
    newest = rows.nth(0).inner_text()
    assert "escalated" in newest and "limit 2" in newest
    assert "acted · automatic" in rows.nth(1).inner_text() and sf.HOSTILE in rows.nth(1).inner_text()
    assert "mode: auto" in rows.nth(3).inner_text() and "person" in rows.nth(3).inner_text()
    assert "settle as abandoned" in rows.nth(4).inner_text() and "suggested" in rows.nth(4).inner_text()   # the oldest line
    assert page.evaluate("window.__pwned === undefined") and box.locator("img").count() == 0
    assert box.locator(".counts").inner_text() == "handled today 2 · waiting on you 1"
    assert "its loop does not run in this dashboard (off: --no-stand-in)" in box.inner_text()
    handled = page.locator('[data-count="stand-in"]')
    assert handled.inner_text() == "2" and handled.get_attribute("title") == "2 handled by the stand-in today"
    assert page.locator('[data-count="needs-you"]').inner_text() == "2"
    shot(page, "standin-log.png")
    open_(page, "/runs")
    until(page, "document.querySelector('#crumbs').textContent.includes('handled by the stand-in today')")
    assert "2 need you · 1 suggestion · 2 handled by the stand-in today" in page.locator("#crumbs").inner_text()
    page.locator(".chip.sug").click()                             # only the runs with a suggestion
    until(page, f"!document.querySelector('table.needs-you tbody').textContent.includes('{RUN}')")
    assert OTHER in page.locator("table.needs-you tbody").inner_text()


def test_handle_the_items_already_waiting(opened, bare):
    sf.declare(bare, "off", may=["settle"])
    sf.mock_decision(bare, "settle", settle_as="abandoned", summary="Nobody waits for this; drop it.")
    for run in (RUN, OTHER):
        sf.write_run(bare, run, ended=sf.ago(3600))
    page, running = opened()
    open_(page, "/now")
    panel(page).wait_for(timeout=10000)
    assert panel(page).locator("button.handle-existing").count() == 0          # off: nothing is offered
    panel(page).locator('.mode-row[data-mode="auto"] input').check()
    handle = panel(page).locator("button.handle-existing")
    handle.wait_for(timeout=15000)
    assert handle.inner_text() == "Handle the 2 items already waiting"
    assert all(sf.run_row(running, "bare", run)["standin_last"] is None for run in (RUN, OTHER))   # left alone until asked
    handle.click()
    until(page, "document.querySelectorAll('.standin-panel .proposals li').length === 2", timeout=120000)
    text = panel(page).locator(".proposals").inner_text()
    assert RUN in text and OTHER in text and text.count("settle as abandoned — Nobody waits for this; drop it.") == 2
    assert not (bare / "agents" / RUN / "settlement.json").exists()               # first what it would do: nothing done
    shot(page, "standin-existing-items.png")
    do = panel(page).locator("button.do-these")
    assert do.inner_text() == "Do these 2"
    do.click()
    for run in (RUN, OTHER):
        record = sf.wait_for(lambda: (p := bare / "agents" / run / "settlement.json").exists() and json.loads(p.read_text()),
                             timeout=120, why="the stand-in settles it")
        assert (record["as"], record["via"]) == ("abandoned", "stand-in")
    until(page, "document.querySelector('.standin-panel .counts').textContent.startsWith('handled today 2')", timeout=30000)
    # a settlement by the stand-in can be undone on the run's page
    open_(page, f"/run/bare/{RUN}")
    undo = page.locator("button.undo-settle")
    undo.wait_for(timeout=10000)
    undo.click()
    sf.wait_for(lambda: not (bare / "agents" / RUN / "settlement.json").exists(), timeout=30)
    until(page, "document.querySelector('button.undo-settle') === null", timeout=30000)
