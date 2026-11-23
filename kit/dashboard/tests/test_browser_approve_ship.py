"""Real-browser tests of Approve & ship / Approve & merge: the row button of the Needs-you view with
what it approves, the confirmation (the gate's reasons, the diff size and files, the target or the
pull request, every check; the person's name remembered), "Approve & ship all N" for one layer, and the
run page's banner. A real layer (start_layer's `bare`) with handed-over runs of ship_fixtures: the
dashboard asks the layer's own adws/dispatch.py what each waits for, and the approval is recorded and
carried out by it.

    (cd kit/dashboard && rm -rf shots && AL_DASHBOARD_SHOTS=shots uv run --with pytest --with playwright \
        pytest -q tests/test_browser_approve_ship.py)

Skipped without Playwright or Chrome (nothing is downloaded). It saves ship-approve-row.png,
ship-approve-confirm.png, ship-approve-all.png and ship-approve-banner.png to $AL_DASHBOARD_SHOTS."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import ship_fixtures as shf
from conftest import start_server, stop_server
from test_approve_ship_http import _approval, _settlement, _state

pytest.importorskip("playwright.sync_api")
from test_browser import browser, open_, until  # noqa: E402,F401 — fixtures; skips without Chrome
from test_browser_assign import tab  # noqa: E402,F401 — a page that fails the test on a console error
from test_browser_workspace import no_personal_paths, shot  # noqa: E402

pytestmark = pytest.mark.browser
DIALOG = "[data-role='approve-ship-dialog']"
RUN, OTHER = "f3000001", "f3000002"


@pytest.fixture()
def site(start_site):
    layer, app = start_site["bare"], start_site["app"]
    running = start_server(["--layer", str(layer)])
    try:
        yield running, layer, app
    finally:
        stop_server(running)


def _row(run: str) -> str:
    return f"#main tbody tr[data-key='bare/{run}']"


def test_row_button_and_dialog_list_what_is_approved(site, tab):
    running, layer, app = site
    shf.add_ship_run(layer, app, RUN)
    _approval(running, RUN)
    commit, main = _state(layer, RUN)["ship"]["commit"], shf.git(app, "rev-parse", "main")
    page = tab(running)
    open_(page, "/runs")
    row = _row(RUN)
    page.wait_for_selector(f"{row} button.approve-ship")
    button = page.locator(f"{row} button.approve-ship")
    assert button.inner_text().strip() == "Approve & merge" and button.get_attribute("data-way") == "merge"
    assert page.locator(f"{row} button.approve-resume").count() == 0
    said = page.locator(f"{row} .approve-items").inner_text()
    assert shf.DIFF in said and "3148 lines, 1 file" in said and "integration" in said
    assert page.locator("button.chip.appr").inner_text().replace("\n", " ").strip() == "waiting for approval (1)"
    assert page.locator("button.approve-ship-all").inner_text().strip() == "Approve & ship all 1"
    assert page.locator("button.approve-resume-all").count() == 0
    no_personal_paths(page)
    shot(page, "ship-approve-row.png")

    button.click()
    page.wait_for_selector(DIALOG)
    assert "/run/" not in page.url, "the button does not open the run"
    dialog = page.locator(DIALOG)
    entry = dialog.locator(f"li[data-run='{RUN}']")
    assert entry.locator("ul.approve-ship-reasons li").all_inner_texts() == [shf.DIFF]
    text = entry.inner_text()
    assert "3148 lines in 1 file" in text and f"shelf_{RUN}.py" in text and "merges into integration" in text
    assert commit[:7] in text and "after: the run's branch is merged into integration (fast-forward)" in text
    checks = entry.locator("ul.approve-ship-checks li")
    assert checks.count() == 1 and checks.first.get_attribute("data-state") == "passed"
    assert checks.first.inner_text().split() == ["test", "passed"]
    go = dialog.locator("button.approve-ship-go")
    assert go.inner_text().strip() == "Approve & merge"
    assert go.is_disabled(), "no name yet: nothing can be approved"
    dialog.locator("input[aria-label='your name']").fill("Robin Example")
    dialog.locator("input[aria-label='a note (optional)']").fill("agreed in review")
    assert go.is_enabled()
    shot(page, "ship-approve-confirm.png")
    go.click()
    until(page, f"!document.querySelector(\"{DIALOG}\")")
    message = f"is merged into integration ({commit[:7]})"
    until(page, f"document.querySelector('.start-report')?.innerText.includes({json.dumps(message)})")
    assert page.evaluate("localStorage.getItem('al.approve.who')") == "Robin Example"
    (given,) = _state(layer, RUN)["approvals"]
    assert (given["what"], given["who"], given["via"], given["note"]) == (
        f"ship:{shf.DIFF}", "Robin Example", "dashboard", "agreed in review")
    assert shf.git(app, "rev-parse", "integration") == commit and shf.git(app, "rev-parse", "main") == main
    assert _settlement(layer, RUN)["as"] == "landed"
    # the run left Needs you, and nothing waits for an approval any more
    until(page, f"!document.querySelector(\"{row}\")", 30000)
    until(page, "!document.querySelector('button.approve-ship-all')")


def test_pull_request_run_says_approve_and_ship_with_a_link(site, tab):
    running, layer, app = site
    shf.add_ship_run(layer, app, RUN, way="pr", reasons=("class-not-allowed:feature",))
    _approval(running, RUN)
    before = shf.git(app, "for-each-ref", "--format=%(refname) %(objectname)")
    page = tab(running)
    page.add_init_script("localStorage.setItem('al.approve.who', 'Robin Example')")
    open_(page, "/runs")
    row = _row(RUN)
    page.wait_for_selector(f"{row} button.approve-ship")
    button = page.locator(f"{row} button.approve-ship")
    assert button.inner_text().strip() == "Approve & ship" and button.get_attribute("data-way") == "pr"
    link = page.locator(f"{row} .approve-items a")
    assert link.get_attribute("href") == shf.PR_URL and "noopener" in link.get_attribute("rel")
    button.click()
    page.wait_for_selector(DIALOG)
    dialog = page.locator(DIALOG)
    assert dialog.locator(f"li[data-run='{RUN}'] a[href='{shf.PR_URL}']").count() == 1
    assert "class-not-allowed:feature" in dialog.inner_text()
    go = dialog.locator("button.approve-ship-go")
    assert go.inner_text().strip() == "Approve & ship" and go.is_enabled()        # the name is remembered
    go.click()
    until(page, f"!document.querySelector(\"{DIALOG}\")")
    until(page, f"document.querySelector('.start-report')?.innerText.includes('shipped to {shf.PR_URL}')")
    settled = _settlement(layer, RUN)
    assert (settled["as"], settled["by"], settled["who"]) == ("shipped", shf.PR_URL, "Robin Example")
    assert shf.git(app, "for-each-ref", "--format=%(refname) %(objectname)") == before, "no ref moved"
    # it is listed as settled, with its pull request as a link
    until(page, f"!document.querySelector(\"{row}\")", 30000)
    open_(page, "/runs?view=settled")
    page.wait_for_selector(f"{row} .landing.settled a")
    assert page.locator(f"{row} .landing.settled a").get_attribute("href") == shf.PR_URL
    assert page.locator(f"{row} .landing.settled").inner_text().startswith("shipped (")


def test_approve_ship_all_lists_every_run_of_one_layer(site, tab):
    running, layer, app = site
    shf.add_ship_run(layer, app, RUN)
    shf.add_ship_run(layer, app, OTHER, way="pr", title="Give the till a label")
    for run in (RUN, OTHER):
        _approval(running, run)
    page = tab(running)
    page.add_init_script("localStorage.setItem('al.approve.who', 'Robin Example')")
    open_(page, "/runs")
    page.wait_for_selector(f"{_row(OTHER)} button.approve-ship")
    page.wait_for_selector(f"{_row(RUN)} button.approve-ship")
    every = page.locator("button.approve-ship-all")
    assert every.inner_text().strip() == "Approve & ship all 2" and every.is_enabled()
    every.click()
    page.wait_for_selector(DIALOG)
    dialog = page.locator(DIALOG)
    assert dialog.locator("ol > li[data-run]").count() == 2
    assert sorted(dialog.locator("ol > li[data-run]").evaluate_all("els => els.map(e => e.dataset.run)")) == [RUN, OTHER]
    assert dialog.locator(f"li[data-run='{RUN}'] .badge").inner_text().strip() == "Approve & merge"
    assert dialog.locator(f"li[data-run='{OTHER}'] .badge").inner_text().strip() == "Approve & ship"
    go = dialog.locator("button.approve-ship-go")
    assert go.inner_text().strip() == "Approve & ship 2 runs"
    no_personal_paths(page)
    shot(page, "ship-approve-all.png")
    go.click()
    until(page, f"!document.querySelector(\"{DIALOG}\")")
    until(page, "document.querySelector('.start-report')?.innerText.includes('Approve & ship (bare):')")
    report = page.locator(".start-report").inner_text()
    assert f"{RUN} → " in report and "is merged into integration" in report and f"shipped to {shf.PR_URL}" in report
    assert _settlement(layer, RUN)["as"] == "landed" and _settlement(layer, OTHER)["as"] == "shipped"
    until(page, "!document.querySelector('button.approve-ship-all')", 30000)


def test_run_page_banner_has_the_ship_button(site, tab):
    running, layer, app = site
    shf.add_ship_run(layer, app, RUN)
    _approval(running, RUN)
    page = tab(running)
    page.add_init_script("localStorage.setItem('al.approve.who', 'Robin Example')")
    open_(page, f"/run/bare/{RUN}")
    banner = "[data-role='approve-banner']"
    page.wait_for_selector(f"{banner} button.approve-ship")
    assert page.evaluate(f"(() => {{ const b = document.querySelector(\"{banner}\"); "
                         "return b.parentElement.firstElementChild === b && b.classList.contains('card'); })()")
    text = page.locator(banner).inner_text()
    assert "This run waits for your approval to ship" in text and shf.DIFF in text
    assert "3148 lines in 1 file" in text and f"shelf_{RUN}.py" in text and "merges into integration" in text
    assert page.locator(f"{banner} ul.approve-ship-checks li[data-state='passed']").count() == 1
    assert page.locator(f"{banner} button.approve-ship").inner_text().strip() == "Approve & merge"
    assert page.locator(f"{banner} button.approve-resume").count() == 0
    no_personal_paths(page)
    shot(page, "ship-approve-banner.png")

    page.click(f"{banner} button.approve-ship")
    page.wait_for_selector(DIALOG)
    page.click(f"{DIALOG} button.confirm-cancel")
    until(page, f"!document.querySelector(\"{DIALOG}\")")
    assert "approvals" not in _state(layer, RUN), "cancelling records nothing"

    page.click(f"{banner} button.approve-ship")
    page.wait_for_selector(DIALOG)
    dialog = page.locator(DIALOG)
    assert dialog.locator("input[aria-label='your name']").input_value() == "Robin Example"   # remembered
    dialog.locator("button.approve-ship-go").click()
    until(page, f"!document.querySelector(\"{DIALOG}\")")
    until(page, f"document.querySelector(\"{banner}\")?.innerText.includes('is merged into integration')")
    assert [(a["what"], a["who"], a["via"]) for a in _state(layer, RUN)["approvals"]] == [
        (f"ship:{shf.DIFF}", "Robin Example", "dashboard")]
    until(page, f"!document.querySelector(\"{banner} button.approve-ship\")", 30000)
