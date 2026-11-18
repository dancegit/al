"""Real-browser test for runs placed on hosts (fixtures.py: notes.al places its runs on build-1 and
build-2, demo-shop.al places none): the host badge on the run card, in the runs list and on the run
page, the status `lost`, and the Now view's Hosts panel with each host's runs, load, memory, disk,
image and login, "no answer" for a silent host and the age of the survey.

Skipped without Playwright or Chrome (nothing is downloaded). With AL_DASHBOARD_SHOTS=<directory> it
saves hosts-panel.png (the Now view) and run-on-host.png (the run page) there."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import fixtures
from conftest import start_server, stop_server

pytest.importorskip("playwright.sync_api")
from test_browser import browser, open_, until  # noqa: E402,F401 — fixtures; skips without Chrome

pytestmark = pytest.mark.browser
REMOTE, LOST, AT_HOME = fixtures.REMOTE, fixtures.LOST, fixtures.AT_HOME


@pytest.fixture()
def site(tmp_path: Path):
    root = tmp_path / "fx"
    fixtures.build(root)
    paths = fixtures.build_hosts(root)
    running = start_server(["--layer", str(paths["layer"]), "--layer", str(root / "demo-shop.al")])
    try:
        yield running, paths
    finally:
        stop_server(running)


@pytest.fixture()
def page(browser, site):
    running, _paths = site
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.base = running.base  # type: ignore[attr-defined]
    yield page
    context.close()
    assert not [e for e in errors if "favicon" not in e], errors


def _shot(page, name: str, full_page: bool = True) -> None:
    directory = os.environ.get("AL_DASHBOARD_SHOTS")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(directory) / name), full_page=full_page)


def test_runs_show_their_host_and_the_hosts_panel_shows_load(page, site):
    _running, paths = site
    # ---- the Now view: the card of the run on build-1 and the Hosts panel
    open_(page, "/now")
    until(page, "document.querySelectorAll('.hosts-layer .host-row').length === 3")
    assert page.locator("h2", has_text="Hosts").is_visible()
    assert page.locator(".hosts-layer").count() == 1                       # demo-shop places no runs: no block
    assert page.locator(".hosts-layer").get_attribute("data-layer") == "notes"
    card = page.locator(f".run-card[data-key='notes/{REMOTE}']")
    assert card.locator(".badge.host").inner_text() == "⌂ build-1"
    assert "runs on build-1" in card.locator(".badge.host").get_attribute("title")
    assert page.locator(".run-card[data-key='demo-shop/a1000006'] .badge.host").count() == 0
    row = lambda host: page.locator(f".hosts-layer .host-row[data-host='{host}']")  # noqa: E731
    home, one, two = row("home").inner_text(), row("build-1").inner_text(), row("build-2").inner_text()
    for needle in ("home", "runs 0/2", "load 0.42", "/ 8 cpu", "memory free 8.6 GB", "of 15 GB", "disk free 114 GB",
                   "image present", "claude-code: own login"):
        assert needle in home, (needle, home)
    for needle in ("build-1", "runs 1/2", "+1 starting", "load 1.37", "memory free 5.2 GB", "image missing",
                   "claude-code: token ANTHROPIC_API_KEY in its env file"):
        assert needle in one, (needle, one)
    assert "mock" not in home and "mock" not in one                        # a login that is not needed is not listed
    for needle in ("build-2", "runs 0/2", "no answer", "no answer within 10 s"):
        assert needle in two, (needle, two)
    assert "load" not in two and "silent" in row("build-2").get_attribute("class")
    survey = page.locator(".hosts-layer .survey").inner_text()
    assert survey.startswith("load as of the last survey, ") and survey.endswith("s ago"), survey
    _shot(page, "hosts-panel.png")

    # a new survey reaches the page: build-2 answers again, with hostile text that stays text
    data = fixtures.hosts_survey(time.time(), age=0)
    data["hosts"]["build-2"] = fixtures.host_load(0, 0.05, 12_000_000, image=None, login="<img src=x onerror=alert(1)>")
    paths["hosts"].write_text(json.dumps(data))
    until(page, "document.querySelector(\".host-row[data-host='build-2']\").innerText.includes('load 0.05')")
    again = row("build-2").inner_text()
    assert "image: not needed" in again and "claude-code: <img src=x onerror=alert(1)>" in again and "no answer" not in again
    assert page.evaluate("document.querySelector('.hosts img') === null")

    # ---- the runs list: a host column, the lost run, the status filter
    open_(page, "/runs?view=all")   # the list opens on "Needs you"; a running run is in "All"
    until(page, f"!!document.querySelector(\"tr[data-key='notes/{LOST}']\")")
    assert "host" in [text.strip().lower() for text in page.locator("thead th").all_inner_texts()]
    cell = lambda rid: page.locator(f"tr[data-key='notes/{rid}'] .badge.host").inner_text()  # noqa: E731
    assert (cell(REMOTE), cell(LOST), cell(AT_HOME)) == ("⌂ build-1", "⌂ build-2 · no answer", "⌂ home")
    assert page.locator("tr[data-key='demo-shop/a1000001'] .badge.host").count() == 0
    assert page.locator(f"tr[data-key='notes/{LOST}'] .pill").inner_text().lower() == "lost"            # upper-cased by CSS
    assert "s-lost" in page.locator(f"tr[data-key='notes/{LOST}'] .pill").get_attribute("class")
    page.locator("select", has=page.locator("option[value='lost']")).select_option("lost")
    until(page, "document.querySelectorAll('tbody tr[data-key]').length === 1")
    assert page.locator("tbody tr[data-key]").get_attribute("data-key") == f"notes/{LOST}"
    open_(page, "/runs?view=all&layer=demo-shop")
    until(page, "!!document.querySelector(\"tr[data-key='demo-shop/a1000001']\")")
    assert "host" not in [text.strip().lower() for text in page.locator("thead th").all_inner_texts()]   # no run has a host

    # ---- the run page
    open_(page, f"/run/notes/{LOST}")
    until(page, "!!document.querySelector('.line1 .badge.lost')")
    line = page.locator(".line1").inner_text()
    assert "⌂ build-2 · no answer" in line and "lost · no answer from build-2 for 120 s" in line, line
    open_(page, f"/run/notes/{REMOTE}")
    until(page, "!!document.querySelector('.line1 .badge.host')")
    assert page.locator(".line1 .badge.host").inner_text() == "⌂ build-1"
    assert page.locator(".line1 .pill").first.inner_text().lower() == "running"
    _shot(page, "run-on-host.png", full_page=False)
