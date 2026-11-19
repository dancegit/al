"""The Testing panel, a run's depth and a failed batch in a real Chrome (tests/testing_fixtures.py): the
layer page's three levels with their typical minutes, the per-command miss rates and the batch state per
stage, switching to heavy (the local setting is written); a run page's depth line, the proof table's
Depth column with the accounting and the "Full before it ships" button; the "broke the batch" banner of
the culprit with its batch and fix runs; the start form's testing choice. Skipped without Playwright or
Chrome (nothing is downloaded). With AL_DASHBOARD_SHOTS=<directory> it saves testing-panel-1280.png,
testing-heavy-1280.png, run-targeted-accounting-1280.png, batch-failed-1280.png and
start-testing-1280.png there. Everything on the page is synthetic."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import dashboard as d
import testing_fixtures as tf
from conftest import start_server, stop_server
from test_browser import CHROME, browser, open_, until, playwright_api  # noqa: F401 — fixtures and skips

pytestmark = pytest.mark.browser


def no_personal_paths(page) -> None:
    text = page.evaluate("document.body.innerText")
    assert str(Path.home()) not in text and f"/{os.environ.get('USER', chr(0))}/" not in text


def shot(page, name: str) -> None:
    directory = os.environ.get("AL_DASHBOARD_SHOTS")
    if directory:
        no_personal_paths(page)
        Path(directory).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(directory) / name), full_page=True)


@pytest.fixture()
def tpage(browser, tmp_path):  # noqa: F811
    site = tf.build_testing_site(tmp_path / "site")
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


def test_testing_panel_switches_to_heavy(tpage):
    page, site = tpage
    open_(page, f"/layer/{tf.NAME}")
    page.wait_for_selector(".testing-panel .testing-level")
    panel = page.locator(".testing-panel")
    assert panel.locator(".testing-level.current").get_attribute("data-level") == "standard"
    assert "targeted" in panel.locator(".testing-level[data-level='standard']").inner_text()
    assert "about 9 min" in panel.locator(".testing-level[data-level='heavy']").inner_text()
    assert "layer file" in panel.locator(".testing-source").inner_text()
    row = panel.locator(".testing-commands tr[data-cmd='test']")
    assert "50%" in row.inner_text() and row.locator(".pill-fallback").inner_text() == "runs full"
    sprint = panel.locator(".batch-row[data-stage='sprint']")
    assert sprint.locator(".batch-red").count() == 1 and tf.CULPRIT in sprint.inner_text() and tf.FIX in sprint.inner_text()
    assert panel.locator(".batch-row[data-stage='main'] .batch-green").count() == 1
    assert sprint.locator("button.batch-run").is_enabled()
    page.locator(".testing-panel").scroll_into_view_if_needed()
    shot(page, "testing-panel-1280.png")

    page.click(".testing-level[data-level='heavy']")
    page.click(".testing-panel button.testing-apply")
    until(page, "document.querySelector('.testing-panel .testing-source') && "
                "document.querySelector('.testing-panel .testing-source').textContent.includes('local setting')")
    assert json.loads((site["layer"] / d.TESTING_FILE).read_text()) == {"version": 1, "level": "heavy"}
    assert page.locator(".testing-panel .testing-level.current").get_attribute("data-level") == "heavy"
    assert page.locator(".testing-panel button.testing-reset").is_visible()
    shot(page, "testing-heavy-1280.png")


def test_run_page_shows_depth_and_full_before_ships(tpage):
    page, site = tpage
    open_(page, f"/run/{tf.NAME}/{tf.TARGETED}")
    page.wait_for_selector("[data-role='proof'] table.proof")
    assert "Testing: targeted (layer policy)" in page.locator(".testing-line").inner_text()
    depth = page.locator("[data-role='proof'] tr[data-depth='targeted'] td.depth").first
    assert tf.ACCOUNTING in depth.inner_text() and depth.locator(".depth-pill.d-targeted").count() == 1
    assert page.locator("button.full-before-ship").count() == 0           # an ended run: nothing to ask

    open_(page, f"/run/{tf.NAME}/{tf.RUNNING}")
    page.wait_for_selector("button.full-before-ship")
    page.click("button.full-before-ship")
    request = site["layer"] / "agents" / tf.RUNNING / "testing-request.json"
    until(page, "document.querySelector('.testing-line') && "
                "document.querySelector('.testing-line').textContent.includes('full before it ships: asked by dashboard')")
    assert json.loads(request.read_text())["by"] == "dashboard"
    assert page.locator("button.full-before-ship").count() == 0
    page.wait_for_selector("[data-role='proof'] td.depth")
    shot(page, "run-targeted-accounting-1280.png")


def test_failed_batch_shows_culprit_and_fix_run(tpage):
    page, site = tpage
    open_(page, f"/run/{tf.NAME}/{tf.CULPRIT}")
    page.wait_for_selector(".broke-batch")
    banner = page.locator(".broke-batch")
    assert "broke the batch" in banner.inner_text() and "tests/test_plugins.py::test_discount" in banner.inner_text()
    assert banner.locator(f"a[href='#/run/{tf.NAME}/{tf.BATCH}']").count() == 1
    assert banner.locator(f"a[href='#/run/{tf.NAME}/{tf.FIX}']").count() == 1
    shot(page, "batch-failed-1280.png")
    # the runs list carries the badge too
    open_(page, f"/runs?layer={tf.NAME}&view=all")
    page.wait_for_selector(f"tr[data-key='{tf.NAME}/{tf.CULPRIT}'] .badge.broke-batch-badge")
    assert page.locator(f"tr[data-key='{tf.NAME}/{tf.TARGETED}'] .broke-batch-badge").count() == 0
    # "Run the full suites now" on the sprint lands on the new batch run
    open_(page, f"/layer/{tf.NAME}")
    page.click(".batch-row[data-stage='sprint'] button.batch-run")
    until(page, "location.hash === '#/run/tested/7e5700b1'")
    assert json.loads((site["layer"] / tf.FIX_FILE).read_text().splitlines()[-1]) == ["run", "--detach", "--json",
                                                                                    "--stage", "sprint"]


def test_start_form_testing_choice(tpage):
    page, site = tpage
    sent: list[dict] = []

    def answer(route):
        sent.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(status=202, content_type="application/json",
                      body=json.dumps({"status": "started", "run_id": tf.TARGETED, "url": f"#/run/{tf.NAME}/{tf.TARGETED}"}))

    page.route("**/api/start", answer)
    open_(page, "/layers")
    page.wait_for_selector(".start-panel select.start-testing")
    options = page.locator(".start-panel select.start-testing option").all_inner_texts()
    assert options[0].startswith("layer default") and [o.split(" ")[0] for o in options[1:]] == ["light", "standard", "heavy"]
    page.select_option(".start-panel select.start-testing", "heavy")
    page.fill(".start-panel textarea.start-prompt", "/plan-build tidy the readme")
    shot(page, "start-testing-1280.png")
    page.click(".start-panel button.start-go")
    until(page, f"location.hash === '#/run/{tf.NAME}/{tf.TARGETED}'")
    assert sent and sent[-1]["testing"] == "heavy"
