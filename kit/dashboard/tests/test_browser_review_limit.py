"""The "Ship without review" card and a run stopped for its size in a real Chrome
(tests/review_limit_fixtures.py): the layer page's card with each value and where it comes from, the
override form with its confirmation, who and when, and Reset; the run page's "Gate and ship" block and
the Needs-you row saying "diff 2500 lines > limit 300 (review option zero-touch), 12 files changed".
Skipped without Playwright or Chrome (nothing is downloaded). With AL_DASHBOARD_SHOTS=<directory> it
saves review-limit-policy.png (the card before a change), review-limit-override.png (the card after the
override) and review-limit-run.png (the run page's Gate and ship block) there, and a second time under
.al-proof/review-limit/ when the repository has a .al-proof folder (a run's proof folder; outside a run
nothing is written there). Everything on the page is synthetic."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import dashboard as d
import review_limit_fixtures as rf
from conftest import start_server, stop_server
from test_browser import CHROME, browser, open_, until, playwright_api  # noqa: F401 — fixtures and skips

pytestmark = pytest.mark.browser

PROOF = Path(__file__).resolve().parents[3] / ".al-proof"   # the runtime creates it, git-ignored, in a run's worktree
ROW = ".review-policy tr[data-option='zero-touch']"


def no_personal_paths(page) -> None:
    text = page.evaluate("document.body.innerText")
    assert str(Path.home()) not in text and f"/{os.environ.get('USER', chr(0))}/" not in text


def shot(page, name: str) -> None:
    directory = os.environ.get("AL_DASHBOARD_SHOTS")
    if directory:
        no_personal_paths(page)
        Path(directory).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(directory) / name), full_page=True)
        if PROOF.is_dir():   # a run's proof folder is collected in full: how these reach the reviewers
            (PROOF / "review-limit").mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(PROOF / "review-limit" / name), full_page=True)


@pytest.fixture()
def rpage(browser, tmp_path):  # noqa: F811
    site = rf.build_review_site(tmp_path / "site")
    running = start_server(["--layer", str(site["layer"])])
    context = browser.new_context(viewport={"width": 1280, "height": 1000})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(f"{msg.text} {(msg.location or {}).get('url', '')}")
            if msg.type == "error" else None)
    page.base = running.base  # type: ignore[attr-defined]
    try:
        yield page, site
    finally:
        context.close()
        stop_server(running)
    assert not [e for e in errors if "favicon" not in e], errors


def row_text(page) -> str:
    return page.locator(ROW).inner_text()


def test_policy_card_and_override_form(rpage):
    page, site = rpage
    setting = site["layer"] / d.REVIEW_FILE
    open_(page, f"/layer/{rf.NAME}")
    page.wait_for_selector(ROW)
    card = page.locator(".review-policy")
    assert "Ship without review" in card.inner_text()
    assert card.locator("tbody tr").count() == 1   # the human option has no limit
    text = row_text(page)
    for word in ("zero-touch", "merge", "300", "layer definition", "chore", "bug", "al's default", "lint", "test"):
        assert word in text, word
    assert page.locator(f"{ROW} .limit-value").inner_text() == "300"
    assert page.locator(f"{ROW} .limit-source").inner_text() == "layer definition"
    assert page.locator(f"{ROW} button.limit-reset").count() == 0
    card.scroll_into_view_if_needed()
    shot(page, "review-limit-policy.png")

    apply = page.locator(f"{ROW} button.limit-apply")
    assert apply.is_disabled()                       # nothing changed, no name
    page.evaluate("localStorage.removeItem('al.settle.who')")
    page.fill(f"{ROW} input.limit-who", "")
    page.fill(f"{ROW} input.limit-input", "3000")
    assert apply.is_disabled()                       # a value, but no name
    page.fill(f"{ROW} input.limit-who", "Ada")
    assert apply.is_enabled()
    page.fill(f"{ROW} input.limit-input", "60000")
    assert apply.is_disabled()                       # outside 1 to 50000
    page.fill(f"{ROW} input.limit-input", "300")
    assert apply.is_disabled()                       # the limit it has
    page.fill(f"{ROW} input.limit-input", "3000")
    assert apply.is_enabled()

    apply.click()
    page.wait_for_selector(".start-confirm")
    message = page.locator(".start-confirm").inner_text()
    assert "zero-touch" in message and "3000" in message and "declared: 300" in message
    assert page.locator(".start-confirm .confirm-go").inner_text() == "Set to 3000"
    page.click(".start-confirm .confirm-cancel")
    assert page.locator(".start-confirm").count() == 0 and not setting.exists()   # Cancel writes nothing

    apply.click()
    page.click(".start-confirm .confirm-go")
    until(page, f"""(document.querySelector("{ROW} .limit-value") || {{}}).textContent === '3000'""")
    stored = json.loads(setting.read_text())
    assert stored["maxDiffLines"] == {"zero-touch": 3000}
    assert (stored["set"]["zero-touch"]["who"], stored["set"]["zero-touch"]["via"]) == ("Ada", "dashboard")
    text = row_text(page)
    assert "local override by Ada" in text and "declared 300" in text
    assert "saved; applies to every gate from now on" in page.locator(".review-policy .review-policy-note").inner_text()
    page.locator(".review-policy").scroll_into_view_if_needed()
    shot(page, "review-limit-override.png")

    page.click(f"{ROW} button.limit-reset")
    page.wait_for_selector(".start-confirm")
    assert "The declared limit 300 applies again." in page.locator(".start-confirm").inner_text()
    page.click(".start-confirm .confirm-go")
    until(page, f"""(document.querySelector("{ROW} .limit-value") || {{}}).textContent === '300'""")
    assert not setting.exists()
    assert "local override" not in row_text(page) and page.locator(f"{ROW} button.limit-reset").count() == 0
    no_personal_paths(page)


def test_run_page_and_needs_you_row_say_why_the_size_stopped_it(rpage):
    page, _site = rpage
    open_(page, f"/run/{rf.NAME}/{rf.RUN}")
    page.wait_for_selector(".size-stop")
    assert page.locator(".size-stop").inner_text() == rf.TEXT
    assert "limit now 300 (layer definition)" in page.locator(".size-now").inner_text()
    block = page.locator(".outcome-block", has_text="Gate and ship")
    assert rf.TEXT in block.inner_text()
    assert rf.TEXT in page.locator(".run-head .badge.handed-over", has_text="↪").inner_text()
    block.scroll_into_view_if_needed()
    shot(page, "review-limit-run.png")

    open_(page, "/runs")
    page.wait_for_selector(f"#main tbody tr[data-key='{rf.NAME}/{rf.RUN}'] td.reason")
    reason = page.locator(f"#main tbody tr[data-key='{rf.NAME}/{rf.RUN}'] td.reason").inner_text()
    assert rf.TEXT in reason and "diff-too-large" not in reason
    no_personal_paths(page)
