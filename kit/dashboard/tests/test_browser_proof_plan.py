"""Real-browser test of the run page's Proof plan card: a state per criterion from the moment the plan
exists, a run stopped before its build with who can clear it (and the Approve button for an approver),
a review that converged in one round with its debt, and the Layers view's review numbers.

    (cd kit/dashboard && rm -rf shots && AL_DASHBOARD_SHOTS=shots uv run --with pytest --with playwright \
        pytest -q tests/test_browser_proof_plan.py)

Skipped without Playwright or Chrome (nothing is downloaded). It saves proof-table.png,
stopped-before-build.png and converged-review.png to $AL_DASHBOARD_SHOTS."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from test_browser import CHROME, browser, open_, page, site, until  # noqa: E402,F401 — fixtures; skips without Chrome
import proof_plan_fixtures as pf  # noqa: E402

pytestmark = pytest.mark.browser
assert CHROME


def shot(page, name: str) -> None:
    directory = os.environ.get("AL_DASHBOARD_SHOTS")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(directory) / name), full_page=False)


def _card(page, run: str):
    open_(page, f"/run/demo-shop/{run}")
    page.wait_for_selector("[data-role='proof-plan']")
    card = page.locator("[data-role='proof-plan']")
    card.scroll_into_view_if_needed()
    return card


def _no_build(page) -> None:
    """A run stopped before its build shows no build: the graph (drawn from the workflow, so it always has
    a build node) leaves that node pending, and the timeline has no build row (.tl-label)."""
    node = page.locator(".g-node[data-step='build']")
    assert node.count() == 1 and "st-pending" in node.get_attribute("class").split()
    assert page.locator(".tl-label", has_text=re.compile(r"^\s*build$")).count() == 0


def test_proof_table_shows_state_per_criterion(page, site):
    _running, agents = site
    pf.add_proof_runs(agents.parent)
    card = _card(page, pf.PLANNED)
    rows = card.locator("tr[data-criterion]")
    assert rows.count() == 2
    assert [rows.nth(i).get_attribute("data-state") for i in range(2)] == ["planned", "planned"]
    # hostile text is shown as text, never as markup
    assert card.locator("img").count() == 0
    assert pf.HOSTILE in card.locator("tr[data-criterion='C1']").inner_text()
    build = page.locator(".g-node[data-step='build']")
    assert build.count() == 1 and "st-running" in build.get_attribute("class").split()
    assert page.locator(".tl-label", has_text=re.compile(r"^\s*build$")).count() == 1
    shot(page, "proof-table.png")


def test_stopped_before_build_shows_party_and_approve_button(page, site):
    _running, agents = site
    pf.add_proof_runs(agents.parent)
    card = _card(page, pf.BEFORE_BUILD)
    stop = card.locator(".proof-stop")
    assert stop.get_attribute("data-party") == "layer author"
    assert "#:max-files 30" in stop.inner_text() and card.locator(".approve-go").count() == 0
    _no_build(page)
    card = _card(page, pf.NEEDS_PERSON)
    until(page, "document.querySelector('.approve-go') !== null")
    _no_build(page)
    button = card.locator(".approve-go")
    assert button.inner_text() == "Approve vocabulary" and button.is_enabled()
    assert "may this run vocabulary?" in card.locator(".proof-stop").inner_text()
    shot(page, "stopped-before-build.png")


def test_converged_review_shows_one_round_and_debt(page, site):
    _running, agents = site
    pf.add_proof_runs(agents.parent)
    card = _card(page, pf.CONVERGED)
    assert [card.locator("tr[data-criterion]").nth(i).get_attribute("data-state") for i in range(2)] == ["proven", "proven"]
    assert "debt from run b1000003" in card.locator(".proof-debt").inner_text()
    progress = page.locator(".round-progress")
    assert progress.count() == 1 and progress.first.inner_text().startswith("resolved 0 · still open 0 · new 0")
    assert "rounds ended: passed" in page.locator(".rounds-stopped").inner_text()
    shot(page, "converged-review.png")
    open_(page, "/layers")
    page.wait_for_selector("[data-role='reviews']")
    reviews = page.locator("[data-role='reviews']").inner_text()
    assert "rounds per run" in reviews and "add first: #:max-files 30" in reviews
