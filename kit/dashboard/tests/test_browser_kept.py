"""Real-browser test of the run page's "Kept on a branch" block: a run of a workflow without a ship
step shows the branch, the kept files and changes.diff, each opening in the Documents viewer, and
a run that changed nothing says so.

    uv run --with pytest --with playwright pytest kit/dashboard/tests/test_browser_kept.py

Skipped without Playwright or Chrome (nothing is downloaded). It saves run-kept-1280.png,
run-kept-390.png and run-kept-nothing-changed.png to the test's tmp_path and, with
AL_DASHBOARD_SHOTS=<directory>, there too."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from test_browser import browser, open_, page, site, until  # noqa: E402,F401 — fixtures; skips without Chrome
from test_kept_model import KEPT, NOTHING, _write_runs  # noqa: E402

pytestmark = pytest.mark.browser


def _shot(page, tmp_path: Path, name: str) -> None:
    page.screenshot(path=str(tmp_path / name), full_page=False)
    directory = os.environ.get("AL_DASHBOARD_SHOTS")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(directory) / name), full_page=False)


def _kept_block(page, rid: str):
    open_(page, f"/run/demo-shop/{rid}")
    until(page, "!!document.querySelector('.kept-block')")
    page.wait_for_selector(".kept-block")
    return page.locator(".kept-block")


@pytest.mark.parametrize("width", [1280, 390])
def test_run_page_shows_what_was_kept(page, site, tmp_path, width):
    _running, agents = site
    page.set_viewport_size({"width": width, "height": 800 if width == 1280 else 844})
    _write_runs(agents.parent)
    block = _kept_block(page, KEPT)
    assert block.locator(".card-head .badge").inner_text() == "kept on a branch"
    text = block.inner_text()
    assert f"chore/{KEPT}-report" in text
    report = block.locator("a[data-role=kept-doc]", has_text="docs/report.md")
    assert report.count() == 1
    assert f"documents?path=artefacts%2F{KEPT}%2Fkept-files%2Fdocs%2Freport.md" in report.get_attribute("href")
    assert block.locator("a[data-role=kept-doc]", has_text="changes.diff").count() == 1
    assert "big.bin (2000000 bytes; on the branch only)" in text
    block.scroll_into_view_if_needed()
    _shot(page, tmp_path, f"run-kept-{width}.png")


def test_kept_diff_link_opens_the_document_viewer(page, site, tmp_path):
    _running, agents = site
    _write_runs(agents.parent)
    block = _kept_block(page, KEPT)
    block.locator("a[data-role=kept-doc]", has_text="changes.diff").click()
    page.wait_for_selector(".doc-view pre.doc-raw")
    assert "+++ b/docs/report.md" in page.locator(".doc-view pre.doc-raw").inner_text()
    assert "/documents?path=" in page.url


def test_run_page_says_nothing_changed(page, site, tmp_path):
    _running, agents = site
    _write_runs(agents.parent)
    block = _kept_block(page, NOTHING)
    assert block.locator(".card-head .badge").inner_text() == "nothing changed"
    assert "The workflow has no ship step and the run changed nothing." in block.inner_text()
    block.scroll_into_view_if_needed()
    _shot(page, tmp_path, "run-kept-nothing-changed.png")
