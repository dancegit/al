"""Real-browser tests of "Give to an agent": the button in every Needs-you row and on every such run's
page, the plan preview of a rejected review (and starting it), a "needs a person" item approved by name
and the plan that remains, "Give all N to agents" with every run's plan first, and the count of
artefact files the reviewers did not get. The scenario is assign_fixtures (the invented pantry with
real git history; the layer's own adws/assign.py runs with the mock agent).

    uv run --with pytest --with playwright pytest kit/dashboard/tests/test_browser_assign.py

Skipped without Playwright or Chrome (nothing is downloaded). With AL_DASHBOARD_SHOTS=<directory> the
tests save assign-needs-you-1280.png, assign-button-1280.png, assign-preview-rejected-1280.png,
assign-preview-rejected-390.png, assign-preview-person-1280.png, assign-approved-1280.png,
assign-bulk-1280.png and assign-left-out-1280.png there."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

import assign_fixtures as af
import dashboard as d
import integrate_fixtures as fx
from conftest import start_server, stop_server

pytest.importorskip("playwright.sync_api")
from test_browser import browser, open_, until  # noqa: E402,F401 — fixtures; skips without Chrome
from test_browser_workspace import no_personal_paths, shot  # noqa: E402

pytestmark = pytest.mark.browser
RUN = af.REJECTED
ORDER = [af.FAILED_CHECK, af.NOTHING, fx.ACCEPTANCE, fx.CLEAN, fx.REJECTED, af.REJECTED, fx.PROTECTED, fx.CHANGED,
         af.PERSON]


@pytest.fixture(scope="module")
def scenario_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("browser-assign") / "s"
    af.build_scenario(root)
    return root


@pytest.fixture()
def scenario(tmp_path: Path, scenario_root: Path, monkeypatch: pytest.MonkeyPatch):
    for name in ("AL_APP_ROOT", "AL_MODEL_SET", "AL_MODELS", "AL_MAX_COST", "AL_DASHBOARD_TOKEN", "AL_DECIDER"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "s"
    shutil.copytree(scenario_root, root, symlinks=True)
    running = start_server(["--layer", str(root / "pantry.al")])
    try:
        yield running, root / "pantry.al", root / "pantry"
    finally:
        stop_server(running)


@pytest.fixture()
def tab(browser):
    pages = []

    def make(running, width: int = 1280, height: int = 800):
        context = browser.new_context(viewport={"width": width, "height": height})
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.base = running.base  # type: ignore[attr-defined]
        page.errors = errors  # type: ignore[attr-defined]
        pages.append((context, page))
        return page

    yield make
    for context, page in pages:
        context.close()
        assert not [e for e in page.errors if "favicon" not in e and "Failed to load resource" not in e], page.errors


def needs_you(page, count: int, timeout: int = 30000) -> None:
    until(page, f"document.querySelectorAll('#main tbody tr[data-key]').length === {count} && "
                f"!document.querySelector('#main td.reason')?.innerText.includes('checking')", timeout)


def row(page, run: str) -> dict:
    return page.evaluate("""(key) => { const tr = document.querySelector(`#main tbody tr[data-key="${key}"]`);
        return tr ? { reason: tr.querySelector('td.reason')?.innerText || '', next: tr.querySelector('td.next')?.innerText || '',
                      button: tr.querySelector('button.assign-open')?.innerText.trim() || '' } : null; }""", f"pantry/{run}")


def open_preview(page, run: str) -> None:
    open_(page, f"/run/pantry/{run}")
    page.wait_for_selector(".settle-card button.assign-open:not([disabled])")
    page.click(".settle-card button.assign-open")
    page.wait_for_selector(".assign-card .assign-preview .kv")


def card_rows(page) -> dict[str, str]:
    """The card's rows: {label: text}."""
    return page.evaluate("""Object.fromEntries([...document.querySelectorAll('.assign-card .assign-preview > dl.kv > dt')]
        .map((dt) => [dt.innerText.trim(), dt.nextElementSibling.innerText.replace(/\\s+/g, ' ').trim()]))""")


def run_dirs(layer: Path) -> list[str]:
    return sorted(p.name for p in (layer / "agents").iterdir() if p.is_dir() and len(p.name) == 8)


def wait_ended(layer: Path, run: str, timeout: float = 240.0) -> str:
    """Wait until the run's detached process has ended → its status."""
    deadline = time.monotonic() + timeout
    status = "not written"
    while time.monotonic() < deadline:
        path = layer / "agents" / run / "state.json"
        try:
            status = str(json.loads(path.read_text()).get("status")) if path.is_file() else "not written"
        except ValueError:
            status = "running"
        if status not in ("running", "not written"):
            return status
        time.sleep(0.3)
    raise AssertionError(f"run {run} did not end: {status}")


def wait_integration(layer: Path, run: str, timeout: float = 240.0) -> dict:
    deadline = time.monotonic() + timeout
    record: dict = {}
    while time.monotonic() < deadline:
        path = layer / "agents" / run / "integration.json"
        if path.is_file():
            record = json.loads(path.read_text())
            if record.get("status") not in ("queued", "running"):
                return record
        time.sleep(0.3)
    raise AssertionError(f"the integration of {run} did not end: {record}")


def test_button_in_row_and_on_page_for_every_needs_you_run(scenario, tab, tmp_path):
    running, layer, _repo = scenario
    page = tab(running)
    open_(page, "/runs")
    needs_you(page, 9)
    assert page.inner_text("#crumbs").strip() == "9 need you"
    for run in af.KINDS:
        found = row(page, run)
        assert found["button"] == "Give to an agent", (run, found)
        assert found["next"].split("\n")[0].strip().endswith(", or Give to an agent"), (run, found)
    assert "Start again, or Give to an agent" in row(page, af.NOTHING)["next"]
    assert page.inner_text("button.assign-all").strip() == "Give all 9 to agents" and page.is_enabled("button.assign-all")
    no_personal_paths(page)
    shot(page, "assign-needs-you-1280.png")
    page.click(".chips .chip[data-view=landed]")              # only the runs that need you have the button
    until(page, "document.querySelectorAll('#main tbody tr[data-key]').length === 1")
    assert not page.query_selector("button.assign-open") and not page.query_selector("button.assign-all")
    for run in af.KINDS:
        open_(page, f"/run/pantry/{run}")
        page.wait_for_selector(".settle-card button.assign-open:not([disabled])")
        buttons = page.evaluate("[...document.querySelectorAll('.settle-card .settle-actions button')].map((b) => b.innerText.trim())")
        assert buttons == ["Give to an agent", "Check if it has landed", "Find what replaced it"], run
        assert not page.query_selector(".assign-card"), "the plan opens only when asked"
    open_(page, f"/run/pantry/{fx.LANDED}")                   # a run that needs nobody has no such button
    page.wait_for_selector(".settle-card")
    assert not page.query_selector(".settle-card button.assign-open")
    open_(page, f"/run/pantry/{af.FAILED_CHECK}")
    page.wait_for_selector(".settle-card button.assign-open:not([disabled])")
    assert page.query_selector("button.start-again"), "Start again stays beside it"
    no_personal_paths(page)
    shot(page, "assign-button-1280.png")
    open_(page, "/runs")
    needs_you(page, 9)
    page.click(f'#main tbody tr[data-key="pantry/{af.FAILED_CHECK}"] button.assign-open')   # the row's button opens the plan
    until(page, f"location.hash.startsWith('#/run/pantry/{af.FAILED_CHECK}') && location.hash.includes('assign=1')")
    page.wait_for_selector(".assign-card .assign-preview .kv")
    assert card_rows(page)["resumes from"] == "test (agent steps on the heavy tier)"
    # a layer generated before `al assign`: no button in the row, a disabled one on the page that says what to run
    old_root = tmp_path / "old"
    shutil.copytree(layer.parent, old_root, symlinks=True)
    (old_root / "pantry.al" / "adws" / "assign.py").unlink()
    old = start_server(["--layer", str(old_root / "pantry.al")])
    try:
        before = tab(old)
        open_(before, "/runs")
        needs_you(before, 9)
        assert not before.query_selector("button.assign-open") and not before.query_selector("button.assign-all")
        assert "Give to an agent" not in before.inner_text("#main table")
        open_(before, f"/run/pantry/{RUN}")
        before.wait_for_selector(".settle-card button.assign-open")
        assert before.is_disabled(".settle-card button.assign-open")
        assert before.get_attribute(".settle-card button.assign-open", "title") == "run `al gen` to add adws/assign.py"
    finally:
        stop_server(old)


def test_plan_preview_of_a_rejected_review(scenario, tab):
    running, layer, repo = scenario
    page = tab(running)
    before = run_dirs(layer)
    open_preview(page, RUN)
    text = page.inner_text(".assign-card")
    assert "give to an agent" in text.lower() and "what will happen; nothing starts until you confirm" in text
    rows = card_rows(page)
    assert rows["stopped"] == "the review rejected it: the change is not right yet"
    assert rows["plan"].startswith("address the review's blockers address 1 blocker (the review's patch round) and review once more")
    steps = page.evaluate("[...document.querySelectorAll('.assign-card ol.assign-steps > li')].map((li) => li.innerText.replace(/\\s+/g, ' ').trim())")
    assert steps == [f"apply branch {af.branch_of(RUN)} onto main in a fresh worktree",
                     "address the blockers with the review's patch round: - total() accepts a negative discount",
                     "run the checks again", "review once, with no further patch round",
                     "ship through the unchanged gate, or back to you with the history"]
    tip = fx.git(repo, "rev-parse", f"refs/heads/{af.branch_of(RUN)}")
    main = fx.git(repo, "rev-parse", "refs/heads/main")
    assert rows["branch"] == f"{af.branch_of(RUN)} at {tip[:7]} (it is not changed)" and rows["target"] == f"main at {main[:7]}"
    assert rows["conflicted files"].startswith("none: the branch applies cleanly")
    assert rows["steps"] == "prepare → resolve → address → test → review → ship"
    blockers = page.evaluate("[...document.querySelectorAll('.assign-card .assign-blockers li')].map((li) => li.innerText.replace(/\\s+/g, ' ').trim())")
    assert blockers == ["total() accepts a negative discount resolution: reject a negative discount"]
    assert rows["limit"] == "given to an agent 0 of 2 times" and "attempts" not in rows and "a person decides" not in rows
    assert page.get_attribute(".assign-card input.assign-note-text", "maxlength") == "500"
    assert not page.query_selector(".assign-card input.assign-override"), "the override is offered only at the limit"
    assert page.inner_text(".assign-card button.assign-start").strip() == "Start" and page.is_enabled(".assign-card button.assign-start")
    no_personal_paths(page)
    shot(page, "assign-preview-rejected-1280.png")
    narrow = tab(running, 390, 844)
    open_preview(narrow, RUN)
    narrow.locator(".assign-card").scroll_into_view_if_needed()
    wide = narrow.evaluate("""[...document.querySelectorAll('.assign-card, .assign-card *')]
        .filter((el) => el.getBoundingClientRect().right > window.innerWidth + 1).map((el) => el.className || el.tagName)""")
    assert wide == [], f"the plan fits a phone's width: {wide}"
    no_personal_paths(narrow)
    shot(narrow, "assign-preview-rejected-390.png")
    narrow.click(".assign-card button.assign-close")
    until(narrow, "!document.querySelector('.assign-card')")
    assert run_dirs(layer) == before and not (layer / "agents" / RUN / "assign.json").exists(), "looking starts nothing"
    # one sentence for the agent, then Start: the page goes to the run that does it
    page.fill(".assign-card input.assign-note-text", "keep the discount optional")
    page.click(".assign-card button.assign-start")
    until(page, f"location.hash.startsWith('#/run/pantry/') && !location.hash.includes('{RUN}')", 30000)
    new = page.evaluate("location.hash.split('/')[3].split('?')[0]")
    assert new not in before
    page.wait_for_selector(".g-node[data-step='address']")
    steps = page.evaluate("[...document.querySelectorAll('.g-node[data-step]')].map((n) => n.dataset.step)")
    assert steps == ["prepare", "resolve", "address", "test", "review", "ship"]
    assert f"integrates {RUN}" in page.inner_text(".run-head")
    attempt = af.record_of(layer, RUN)["attempts"][0]
    assert (attempt["by"], attempt["kind"], attempt["run"], attempt["note"]) == (
        "dashboard", "blockers", new, "keep the discount optional")
    record = wait_integration(layer, RUN)
    assert (record["run"], record["status"]) == (new, "landed"), (layer / "agents" / new / "launch.log").read_text()[-3000:]
    assert "negative discount" in fx.git(repo, "show", "main:stock.py")
    # the original: landed by that run, and its page says it was given to an agent
    open_(page, f"/run/pantry/{RUN}")
    page.wait_for_selector(".run-head a.landed-by")
    until(page, "document.querySelector('.assign-fact')?.innerText.includes('landed')", 20000)
    fact = page.locator(".assign-fact")
    assert fact.inner_text().startswith(f"1 of 2; last: landed ({new})")
    assert fact.locator("a").get_attribute("href") == f"#/run/pantry/{new}"
    assert not page.query_selector(".settle-card") and not page.query_selector("button.assign-open")
    no_personal_paths(page)


def test_needs_a_person_offers_the_approval(scenario, tab):
    running, layer, _repo = scenario
    page = tab(running)
    before = run_dirs(layer)
    open_preview(page, af.PERSON)
    page.wait_for_selector(".assign-card .assign-decision")
    rows = card_rows(page)
    assert rows["plan"] == f"a person decides a person decides: {af.PERSON_BLOCKER['description']}"
    decisions = page.evaluate("[...document.querySelectorAll('.assign-card .assign-decision .decision-text')].map((el) => el.innerText)")
    assert decisions == [af.PERSON_BLOCKER["description"]]
    assert page.inner_text(".assign-card button.assign-approve").strip() == "I approve this, continue"
    assert not page.query_selector(".assign-card button.assign-start"), "nothing can start before the person decided"
    assert not page.query_selector(".assign-card input.assign-note-text")
    no_personal_paths(page)
    shot(page, "assign-preview-person-1280.png")
    page.fill(".assign-card input.assign-who", "Ada Example")
    page.click(".assign-card button.assign-approve")
    page.wait_for_selector(".assign-card .assign-decision.approved")
    page.wait_for_selector(".assign-card button.assign-start:not([disabled])")
    until(page, "document.querySelector('.assign-card .assign-plan')?.dataset.kind === 'blockers'")
    rows = card_rows(page)
    assert "address 2 blockers (the review's patch round) and review once more" in rows["plan"]
    assert rows["a person decides"].startswith(f"approved by Ada Example {af.PERSON_BLOCKER['description']}")
    assert not page.query_selector(".assign-card button.assign-approve")
    blockers = page.evaluate("[...document.querySelectorAll('.assign-card .assign-blockers li .desc')].map((el) => el.innerText)")
    assert blockers == [af.LABEL_BLOCKER["description"], af.PERSON_BLOCKER["description"]]
    record = af.record_of(layer, af.PERSON)
    assert record["attempts"] == [] and [(a["by"], a["text"], a["via"]) for a in record["approvals"]] == [
        ("Ada Example", af.PERSON_BLOCKER["description"], "dashboard")]
    assert run_dirs(layer) == before, "the approval starts nothing"
    no_personal_paths(page)
    shot(page, "assign-approved-1280.png")
    # the approval is a record: the card shows the same remaining plan when it is opened again
    again = tab(running)
    open_preview(again, af.PERSON)
    again.wait_for_selector(".assign-card .assign-decision.approved")
    assert again.is_enabled(".assign-card button.assign-start")


def test_give_all_shows_the_plans_first(scenario, tab):
    running, layer, _repo = scenario
    page = tab(running)
    before = run_dirs(layer)
    open_(page, "/runs")
    needs_you(page, 9)
    page.click("button.assign-all")
    page.wait_for_selector(".assign-bulk ol.assign-bulk-plans li")
    listed = page.evaluate("""[...document.querySelectorAll('.assign-bulk ol.assign-bulk-plans li')].map((li) => ({
        run: li.dataset.run, skipped: li.classList.contains('skipped'), text: li.innerText.replace(/\\s+/g, ' ').trim() }))""")
    assert [item["run"] for item in listed] == ORDER
    assert [item["skipped"] for item in listed] == [False] * 6 + [True] * 3
    assert "resume from check test" in listed[0]["text"] and "start the same request again" in listed[1]["text"]
    assert "address 1 blocker" in listed[5]["text"] and "skipped: needs-a-person: " in listed[8]["text"]
    assert page.inner_text(".assign-bulk button.assign-bulk-start").strip() == "Start 6"
    assert page.is_enabled(".assign-bulk button.assign-bulk-start")
    no_personal_paths(page)
    shot(page, "assign-bulk-1280.png")
    page.click(".assign-bulk button.confirm-cancel")
    until(page, "document.getElementById('overlay').hidden")
    assert run_dirs(layer) == before and not list((layer / "agents").glob("*/assign.json")), "looking starts nothing"
    # all but two are settled by a person meanwhile; the dialog lists what is left, and Start gives those
    for run in ORDER:
        if run not in (af.FAILED_CHECK, af.PERSON):
            d.write_settlement(layer, run, as_="abandoned", who="Ada Example", via="dashboard",
                               status=fx.state_of(layer, run)["status"])
    needs_you(page, 2)
    assert page.inner_text("button.assign-all").strip() == "Give all 2 to agents"
    page.click("button.assign-all")
    until(page, "document.querySelector('.assign-bulk button.assign-bulk-start')?.innerText.trim() === 'Start 1'", 30000)
    assert page.evaluate("[...document.querySelectorAll('.assign-bulk ol.assign-bulk-plans li')].map((li) => li.dataset.run)") == [
        af.FAILED_CHECK, af.PERSON]
    page.click(".assign-bulk button.assign-bulk-start")
    until(page, "document.querySelectorAll('.start-report div').length === 3", 60000)
    lines = page.evaluate("[...document.querySelectorAll('.start-report div')].map((el) => el.innerText)")
    assert lines[0] == "Give all to agents (pantry):"
    assert lines[1] == f"{af.FAILED_CHECK} → resumed in place (check)"
    assert lines[2].startswith(f"{af.PERSON}: skipped: needs-a-person: ")
    assert page.evaluate("document.getElementById('overlay').hidden")
    attempts = af.record_of(layer, af.FAILED_CHECK)["attempts"]
    assert [(a["by"], a["kind"], a["run"]) for a in attempts] == [("dashboard", "check", af.FAILED_CHECK)]
    assert not (layer / "agents" / af.PERSON / "assign.json").exists()
    wait_ended(layer, af.FAILED_CHECK)


def test_left_out_count_on_the_run_page(scenario, tab):
    running, _layer, _repo = scenario
    page = tab(running)
    open_(page, f"/run/pantry/{RUN}/artefacts")
    page.wait_for_selector(".art-group .left-out")
    lines = page.evaluate("[...document.querySelectorAll('.left-out')].map((el) => el.innerText.trim())")
    assert lines == ["shots: 3 more files matched and were not handed to the reviewers"]
    assert "5 files matched" in page.get_attribute(".left-out", "title")
    assert page.locator(".art-group .thumb").count() == 2, "the two that were handed over are shown"
    no_personal_paths(page)
    shot(page, "assign-left-out-1280.png")
    open_(page, f"/run/pantry/{af.PERSON}/artefacts")
    page.wait_for_selector("#main .empty")
    assert not page.query_selector(".left-out")
