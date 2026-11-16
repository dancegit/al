"""Real-browser test of a run whose review was rejected: the review step is amber with ⊗, its rounds
are under the step, and the run is handed-over in the header and in the run list. The run list
totals, the layer card and the Now view count handed-over runs apart from passed ones.

    uv run --with pytest --with playwright pytest kit/dashboard/tests/test_browser_review.py

Skipped without Playwright or Chrome (nothing is downloaded). It saves review-rejected.png,
review-rejected-runs.png, review-rejected-layers.png and review-rejected-now.png to the test's
tmp_path and, with AL_DASHBOARD_SHOTS=<directory>, there too."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import dashboard as d
import fixtures

pytest.importorskip("playwright.sync_api")
from test_browser import browser, open_, page, site, until  # noqa: E402,F401 — fixtures; skips without Chrome
from test_review_rejected_model import _model, add_review_runs  # noqa: E402

pytestmark = pytest.mark.browser


def _shot(page, tmp_path: Path, name: str) -> None:
    page.screenshot(path=str(tmp_path / name), full_page=False)
    directory = os.environ.get("AL_DASHBOARD_SHOTS")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(directory) / name), full_page=False)


def test_rejected_review_is_shown_as_rejected_and_the_run_as_handed_over(page, site, tmp_path):
    _running, agents = site
    add_review_runs(agents.parent)
    open_(page, "/run/demo-shop/a1000101")
    page.wait_for_selector(".g-node[data-step='review']")
    assert page.locator(".pill", has_text="handed-over").first.is_visible()
    assert page.locator(".badge.handed-over", has_text="handed to a person").first.is_visible()
    node = page.locator(".g-node[data-step='review']")
    assert "st-rejected" in node.get_attribute("class")
    icon = node.locator("text.icon").text_content()
    assert icon == "⊗" and icon != "✓"

    page.click(".g-node[data-step='review']")
    page.wait_for_selector(".step-panel .step-rounds")
    rounds = page.locator(".step-panel .step-rounds").inner_text()
    for number in (1, 2, 3):
        assert f"round {number}: not approved" in rounds
    _shot(page, tmp_path, "review-rejected.png")

    open_(page, "/runs")
    page.wait_for_selector("tbody tr[data-key='demo-shop/a1000101']")
    until(page, "document.querySelector(\"tbody tr[data-key='demo-shop/a1000101']\").textContent.includes('handed-over')")
    until(page, "[...document.querySelectorAll('.stat')].some((s) => s.querySelector('.label')?.textContent.trim() === 'handed over'"
                " && s.querySelector('.value')?.textContent.trim() === '2')")
    _shot(page, tmp_path, "review-rejected-runs.png")

    runs = [s for k, s in _model(agents.parent).summaries.items() if k.startswith("demo-shop/")]
    agg = d.aggregate(runs)
    assert agg["handed_over"] == 2
    expected = f"{int(agg['passed'] / agg['finished'] * 100 + 0.5)}% passed"   # JS Math.round
    counted_as_passed = int((agg["passed"] + 2) / agg["finished"] * 100 + 0.5)

    open_(page, "/layers")
    page.wait_for_selector(".layer-card")
    card = page.locator(".layer-card", has_text="demo-shop").first
    text = " ".join(card.inner_text().split())
    assert "2 handed over" in text, text
    assert expected in text, text
    if f"{counted_as_passed}% passed" != expected:
        assert f"{counted_as_passed}% passed" not in text, text
    _shot(page, tmp_path, "review-rejected-layers.png")


def test_now_view_counts_handed_over_runs_apart_from_passed(page, site, tmp_path):
    _running, agents = site
    add_review_runs(agents.parent)
    page.clock.set_fixed_time(fixtures.at(22200))   # Date.now only, not the timers: the review runs fall in the last 24 h
    open_(page, "/now")
    until(page, "document.querySelector('.strip').textContent.includes('· 2 handed over')")
    _shot(page, tmp_path, "review-rejected-now.png")
