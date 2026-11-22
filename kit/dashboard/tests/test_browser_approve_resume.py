"""Real-browser tests of Approve & resume: the "waiting for approval (N)" chip and the row button of the
Needs-you view with its confirmation (every item in full, the person's name remembered), and the run
page's banner. A real layer (start_layer's `bare`, the mock agent): the run stops because its plan's
Proof plan needs `approval:vocabulary`, the dashboard asks the layer's own adws/dispatch.py what it
waits for, and the approval resumes it.

    (cd kit/dashboard && rm -rf shots && AL_DASHBOARD_SHOTS=shots uv run --with pytest --with playwright \
        pytest -q tests/test_browser_approve_resume.py)

Skipped without Playwright or Chrome (nothing is downloaded). It saves run-approve-row.png,
run-approve-confirm.png and run-approve-banner.png to $AL_DASHBOARD_SHOTS."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import start_server, stop_server
from test_approve_resume_http import PLAN, _approval, _passed, _stopped_run

pytest.importorskip("playwright.sync_api")
from test_browser import browser, open_, until  # noqa: E402,F401 — fixtures; skips without Chrome
from test_browser_assign import tab  # noqa: E402,F401 — a page that fails the test on a console error
from test_browser_workspace import no_personal_paths, shot  # noqa: E402

pytestmark = pytest.mark.browser
DIALOG = "[data-role='approve-resume-dialog']"
ITEM = "vocabulary: needed by C1: the tests pass"


@pytest.fixture()
def site(start_site):
    layer = start_site["bare"]
    (layer / "mock").mkdir(exist_ok=True)
    (layer / "mock" / "plan.json").write_text(json.dumps({"layer_files": {"specs/plan-word.md": PLAN},
                                                          "output": "specs/plan-word.md"}))
    running = start_server(["--layer", str(layer)])
    try:
        yield running, layer
    finally:
        stop_server(running)


def _waiting_run(running, layer: Path) -> str:
    run = _stopped_run(running, layer)
    _approval(running, run)
    return run


def test_row_button_confirms_and_shows_the_resumed_run(site, tab):
    running, layer = site
    run = _waiting_run(running, layer)
    page = tab(running)
    open_(page, "/runs")
    row = f"#main tbody tr[data-key='bare/{run}']"
    page.wait_for_selector(f"{row} button.approve-resume")
    chip = page.locator("button.chip.appr")
    assert chip.inner_text().replace("\n", " ").strip() == "waiting for approval (1)"
    assert page.locator(f"{row} button.approve-resume").inner_text().strip() == "Approve & resume"
    assert page.locator(f"{row} .approve-items").inner_text().strip() == ITEM
    assert page.locator("button.approve-resume-all").inner_text().strip() == "Approve & resume all 1"
    chip.click()                                         # the chip keeps only the runs that wait for an approval
    until(page, "document.querySelector('button.chip.appr').classList.contains('active')")
    assert page.locator("#main tbody tr[data-key]").count() == 1 and "appr=1" in page.url
    no_personal_paths(page)
    shot(page, "run-approve-row.png")

    page.click(f"{row} button.approve-resume")
    page.wait_for_selector(DIALOG)
    assert "/run/" not in page.url, "the button does not open the run"
    dialog = page.locator(DIALOG)
    assert dialog.locator(f"li[data-run='{run}'] li[data-item='vocabulary']").inner_text().strip() == ITEM
    assert "resumes from prove" in dialog.inner_text() and "after: resume from prove" in dialog.inner_text()
    go = dialog.locator("button.approve-resume-go")
    assert go.is_disabled(), "no name yet: nothing can be approved"
    dialog.locator("input[aria-label='your name']").fill("Robin Example")
    dialog.locator("input[aria-label='a note (optional)']").fill("agreed in review")
    assert go.is_enabled()
    shot(page, "run-approve-confirm.png")
    go.click()
    until(page, f"!document.querySelector(\"{DIALOG}\")")
    message = f"approved vocabulary for run {run}; resuming from step prove"
    until(page, f"document.querySelector('.start-report')?.innerText.includes({json.dumps(message)})")
    assert page.evaluate("localStorage.getItem('al.approve.who')") == "Robin Example"
    state = _passed(layer, run)
    (given,) = state["approvals"]
    assert (given["what"], given["who"], given["via"], given["note"]) == ("vocabulary", "Robin Example", "dashboard",
                                                                         "agreed in review")
    # the run left Needs you, and nothing waits for an approval any more
    until(page, f"!document.querySelector(\"{row}\")", 30000)
    until(page, "!document.querySelector('button.approve-resume-all')")


def test_run_page_banner_approves_and_resumes(site, tab):
    running, layer = site
    run = _waiting_run(running, layer)
    page = tab(running)
    page.add_init_script("localStorage.setItem('al.approve.who', 'Robin Example')")
    open_(page, f"/run/bare/{run}")
    banner = "[data-role='approve-banner']"
    page.wait_for_selector(f"{banner} button.approve-resume")
    assert page.evaluate(f"(() => {{ const b = document.querySelector(\"{banner}\"); "
                         "return b.parentElement.firstElementChild === b && b.classList.contains('card'); })()")
    text = page.locator(banner).inner_text()
    assert "This run waits for your approval" in text and ITEM in text and "resumes from prove" in text
    no_personal_paths(page)
    shot(page, "run-approve-banner.png")
    # the proof-plan box's Approve button opens the same confirmation
    page.wait_for_selector("[data-role='proof-plan'] .approve-go")
    assert page.locator("[data-role='proof-plan'] .approve-go").inner_text().strip() == "Approve vocabulary"
    page.click("[data-role='proof-plan'] .approve-go")
    page.wait_for_selector(DIALOG)
    page.click(f"{DIALOG} button.confirm-cancel")
    until(page, f"!document.querySelector(\"{DIALOG}\")")
    assert "approvals" not in json.loads((layer / "agents" / run / "state.json").read_text())

    page.click(f"{banner} button.approve-resume")
    page.wait_for_selector(DIALOG)
    dialog = page.locator(DIALOG)
    assert dialog.locator("li[data-item='vocabulary']").inner_text().strip() == ITEM
    assert dialog.locator("input[aria-label='your name']").input_value() == "Robin Example"   # remembered
    go = dialog.locator("button.approve-resume-go")
    assert go.is_enabled()
    go.click()
    until(page, f"!document.querySelector(\"{DIALOG}\")")
    until(page, f"document.querySelector(\"{banner}\")?.innerText.includes('approved vocabulary for run {run}')")
    state = _passed(layer, run)
    assert [(a["what"], a["who"], a["via"]) for a in state["approvals"]] == [("vocabulary", "Robin Example", "dashboard")]
    # the page follows the run: it is running or has ended, and no longer asks for an approval
    until(page, f"!document.querySelector(\"{banner} button.approve-resume\")", 30000)
    until(page, "document.querySelector('.run-head .pill.s-running, .run-head .pill.s-passed') !== null", 30000)
