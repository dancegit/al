"""Real-browser test of the step page's step-discipline line: a build step that stayed in its step
says so in green, and one that left it says what it did, what was restored and what was not, in
amber.

    uv run --with pytest --with playwright pytest kit/dashboard/tests/test_browser_discipline.py

Skipped without Playwright or Chrome (nothing is downloaded). It saves step-discipline-ok.png and
step-discipline-violation.png to the test's tmp_path and, with AL_DASHBOARD_SHOTS=<directory>,
there too."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from test_browser import browser, open_, page, site, until  # noqa: E402,F401 — fixtures; skips without Chrome
from test_step_discipline_model import OK, VIOLATION, _run  # noqa: E402

pytestmark = pytest.mark.browser


def _shot(page, tmp_path: Path, name: str) -> None:
    page.screenshot(path=str(tmp_path / name), full_page=False)
    directory = os.environ.get("AL_DASHBOARD_SHOTS")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(directory) / name), full_page=False)


def _discipline(page, rid: str):
    open_(page, f"/run/demo-shop/{rid}/step/build")
    until(page, "!!document.querySelector('.step-panel .discipline')")
    page.wait_for_selector(".step-panel .discipline")
    line = page.locator(".step-panel .discipline")
    assert line.is_visible()
    return line


def test_the_step_page_shows_the_build_step_stayed_in_its_step(page, site, tmp_path):
    _running, agents = site
    _run(agents.parent, OK, rid="d15c0011")
    line = _discipline(page, "d15c0011")
    assert "good" in line.get_attribute("class")
    assert line.inner_text() == "Stayed in its step: HEAD unchanged, no refs changed, nothing outside the worktree"
    _shot(page, tmp_path, "step-discipline-ok.png")


def test_the_step_page_shows_what_the_build_step_did_and_what_was_restored(page, site, tmp_path):
    _running, agents = site
    _run(agents.parent, VIOLATION, rid="d15c0012")
    line = _discipline(page, "d15c0012")
    assert "warn" in line.get_attribute("class")
    text = line.inner_text()
    assert text.startswith("Left its step: the build step committed"), text
    assert "Restored: HEAD reset to 1111111" in text and "Not restored: refs/heads/side" in text, text
    _shot(page, tmp_path, "step-discipline-violation.png")
