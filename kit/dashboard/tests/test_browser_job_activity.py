"""Real-browser tests of the Job page with an agent step: a `create` on the invented repository fern whose
author step replays a recorded Claude Code stream (data/job-streams/) through `fake_al`. Running, finished
and failed, each at 1280 and 390 px wide: the steps row leads and says what the agent is doing; the
activity shows messages and one-line tool calls, with hook and bookkeeping events hidden until "show
everything"; a finished job shows its outcome, Decisions and Left out; a failed one shows its step, its
reason, "Retry from author" and "Download log" without scrolling, and the raw stream is one click away.
The page only ever reads the condensed journal and the windowed log, never a step's file whole.

    uv run --with pytest --with playwright pytest kit/dashboard/tests/test_browser_job_activity.py

Skipped without Playwright or Chrome (nothing is downloaded). With AL_DASHBOARD_SHOTS=<directory> the
tests save job-running-1280.png, job-running-390.png, job-finished-1280.png, job-finished-390.png,
job-failed-1280.png and job-failed-390.png there."""
from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest

import workspace_fixtures as wf
from conftest import stop_server

pytest.importorskip("playwright.sync_api")
from test_browser import browser, open_, until  # noqa: E402,F401 — fixtures; skips without Chrome
from test_browser_workspace import no_personal_paths, serve, shot, workspace  # noqa: E402,F401
from test_actions import confirmed, wait_job  # noqa: E402

pytestmark = pytest.mark.browser

WIDTHS = [1280, 390]
HEIGHTS = {1280: 800, 390: 844}


@pytest.fixture()
def tab(browser):
    """A factory: tab(running, width) → a page of that width that records every request it makes."""
    pages = []

    def make(running, width: int):
        context = browser.new_context(viewport={"width": width, "height": HEIGHTS[width]}, accept_downloads=True)
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.requests = []  # type: ignore[attr-defined]
        page.on("request", lambda request: page.requests.append(request.url))
        page.base = running.base  # type: ignore[attr-defined]
        page.errors = errors  # type: ignore[attr-defined]
        pages.append((context, page))
        return page

    yield make
    for context, page in pages:
        context.close()
        assert not [e for e in page.errors if "favicon" not in e], page.errors


def start_create(tmp_path: Path, workspace: Path, stream: str, **behaviour):
    al = wf.fake_al(tmp_path / "bin" / "al", out="plain step output", streams={"author": wf.job_stream(stream, workspace)},
                    **behaviour)
    running = serve(tmp_path, workspace, al)
    return running, confirmed(running, "fern", "create")


def stop_runner(tmp_path: Path, job_id: str) -> None:
    record = json.loads((tmp_path / "jobs" / job_id / "job.json").read_text())
    if record.get("pid"):
        try:
            os.kill(record["pid"], signal.SIGTERM)
        except OSError:
            pass


def only_condensed_reads(page) -> None:
    """The page reads a step through /api/job/journal and /api/job/log only, never a file whole."""
    assert not [url for url in page.requests if "/api/job/file" in url or "/files/" in url], page.requests
    assert any("/api/job/journal" in url for url in page.requests)


def activity_text(page) -> str:
    return page.locator('[data-role="activity"]').inner_text()


@pytest.mark.parametrize("width", WIDTHS)
def test_running_job_shows_steps_and_what_the_agent_does(tmp_path, workspace, tab, width):
    running, job_id = start_create(tmp_path, workspace, "author-running", hold={"author": 60})
    try:
        page = tab(running, width)
        open_(page, f"/job/{job_id}")
        page.wait_for_selector('[data-role="steps"] .step-chip[data-step="author"].s-running [data-role="doing"]', timeout=20000)
        until(page, "document.querySelector('.step-chip[data-step=\"author\"] [data-role=\"doing\"]').textContent === 'writing agentic.rkt'", 20000)
        assert page.locator('.step-chip[data-step="init"]').get_attribute("class").count("s-passed") == 1
        assert page.locator('[data-role="steps"]').bounding_box()["y"] < page.locator('[data-role="activity"]').bounding_box()["y"]
        page.wait_for_selector('[data-role="activity"][data-step="author"] .act-tool')
        until(page, "document.querySelectorAll('[data-role=\"activity\"] .act-tool').length >= 5", 10000)
        assert page.locator('[data-role="activity"] .act-msg').count() >= 2
        assert "I will read the codebase facts first" in activity_text(page)
        tools = page.locator('[data-role="activity"] .act-tool').all_inner_texts()
        assert any("Read" in row and ".github/workflows/ci.yml" in row for row in tools)
        assert page.locator('[data-role="activity"] .act-err').count() >= 1          # the failed compile
        text = activity_text(page)
        assert "hook configuration" not in text and "SessionStart" not in text and "rate limit" not in text
        shot(page, f"job-running-{width}.png")
        no_personal_paths(page)
        page.locator('[data-role="show-all"]').click()
        until(page, "document.querySelector('[data-role=\"activity\"]').innerText.includes('SessionStart')")
        assert "hook configuration" in activity_text(page) and "rate limit" in activity_text(page)
        page.locator('[data-role="show-all"]').click()
        before = int(page.locator('[data-role="activity"]').get_attribute("data-rows"))
        page.fill('[data-role="activity"] [data-role="search"]', "ci.yml")
        until(page, f"Number(document.querySelector('[data-role=\"activity\"]').dataset.rows) < {before}")
        assert page.locator('[data-role="activity"] mark').count() >= 1
        assert int(page.locator('[data-role="activity"]').get_attribute("data-rows")) >= 1
        no_personal_paths(page)
        only_condensed_reads(page)
    finally:
        stop_runner(tmp_path, job_id)
        stop_server(running)


@pytest.mark.parametrize("width", WIDTHS)
def test_finished_job_shows_the_result_card_decisions_and_left_out(tmp_path, workspace, tab, width):
    running, job_id = start_create(tmp_path, workspace, "author-finished")
    try:
        assert wait_job(running, job_id)["status"] == "passed"
        page = tab(running, width)
        open_(page, f"/job/{job_id}")
        page.wait_for_selector('[data-role="outcome"].s-passed')
        outcome = page.locator('[data-role="outcome"]')
        assert "ws/fern.al" in outcome.locator('[data-role="created"]').inner_text()
        assert "UNVERIFIED" in outcome.locator('[data-role="unverified"]').inner_text()
        assert "al verify ws/fern.al" in outcome.locator('[data-role="next"]').inner_text()
        assert outcome.locator('[data-role="forge"]').count() == 1                     # the result card is in the outcome
        assert page.locator('[data-role="decisions"] li').count() == 3
        assert page.locator('[data-role="left-out"] li').count() == 2
        assert page.locator('[data-role="needs-person"]').inner_text() == "needs a person: 2"
        page.wait_for_selector('[data-role="activity"][data-step="author"] .act-report')
        text = activity_text(page)
        assert "<script>window.injected = 1</script>" in text                        # hostile text stays text
        assert page.evaluate("window.injected") is None
        assert page.locator('[data-role="activity"] script, [data-role="activity"] img').count() == 0
        assert page.locator('[data-role="activity"] .act-result').count() == 1
        assert page.locator(".job-output[open]").count() == 0                         # the full output is collapsed
        shot(page, f"job-finished-{width}.png")
        no_personal_paths(page)
        only_condensed_reads(page)
        page.locator('.step-chip[data-step="init"]').click()
        until(page, "location.hash.endsWith('?step=init')")
        page.wait_for_selector('[data-role="activity"][data-step="init"] .tw-line')
        assert "plain step output" in activity_text(page)
        assert not [url for url in page.requests if "/api/job/file" in url]
    finally:
        stop_server(running)


@pytest.mark.parametrize("width", WIDTHS)
def test_failed_job_shows_step_reason_and_actions_without_scrolling(tmp_path, workspace, tab, width):
    running, job_id = start_create(tmp_path, workspace, "author-failed", fail={"author": (1, "")})
    try:
        assert wait_job(running, job_id)["status"] == "failed"
        page = tab(running, width)
        open_(page, f"/job/{job_id}")
        page.wait_for_selector('[data-role="outcome"].s-failed [data-role="retry"]')
        assert page.locator('[data-role="outcome-step"]').inner_text() == "author"
        reason = page.locator('[data-role="outcome"] [data-role="reason"]').inner_text()
        assert reason.startswith("the agent ended with error_max_turns") and "exited 1" not in reason
        assert page.locator('[data-role="retry"]').inner_text() == "Retry from author"
        assert page.evaluate("window.scrollY") == 0
        inner = page.evaluate("window.innerHeight")
        for role in ("outcome-step", "reason", "retry", "download-log"):
            box = page.locator(f'[data-role="outcome"] [data-role="{role}"]').bounding_box()
            assert box is not None and box["y"] >= 0 and box["y"] + box["height"] <= inner, (role, box, inner)
        page.wait_for_selector('[data-role="activity"][data-step="author"] .act-err')
        raw = page.locator('.job-activity [data-role="raw"]')
        assert raw.count() == 1 and "steps%2Fauthor.log" in raw.get_attribute("href")   # the raw stream, one click away
        shot(page, f"job-failed-{width}.png")
        no_personal_paths(page)
        only_condensed_reads(page)
        with page.expect_download() as download:
            page.locator('[data-role="download-log"]').click()
        data = Path(download.value.path()).read_bytes()
        assert data == wf.job_stream("author-failed", workspace).read_bytes()
    finally:
        stop_server(running)
