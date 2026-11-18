"""Real-browser tests of "Resolve with an agent": the button in the Needs-you row and on a run's page,
the preview of what the integration will do, the integration run and the original moving to Landed
with a link to it, and the two evidence buttons beside Settle. The scenario is integrate_fixtures (an
invented pantry with real git history; the layer's own adws/integrate.py runs with the mock agent).

    uv run --with pytest --with playwright pytest kit/dashboard/tests/test_browser_integrate.py

Skipped without Playwright or Chrome (nothing is downloaded). With AL_DASHBOARD_SHOTS=<directory> the
tests save integrate-needs-you-1280.png, integrate-button-1280.png, integrate-preview-1280.png,
integrate-preview-390.png, integrate-running-1280.png, integrate-landed-original-1280.png,
integrate-runs-landed-1280.png, integrate-nothing-needs-you-1280.png,
integrate-evidence-landed-1280.png and integrate-evidence-replaced-1280.png there."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

import integrate_fixtures as fx
from conftest import start_server, stop_server

pytest.importorskip("playwright.sync_api")
from test_browser import browser, open_, until  # noqa: E402,F401 — fixtures; skips without Chrome
from test_browser_workspace import no_personal_paths, shot  # noqa: E402

pytestmark = pytest.mark.browser
RUN = fx.ACCEPTANCE


@pytest.fixture(scope="module")
def scenario_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("browser-integrate") / "s"
    fx.build_scenario(root)
    return root


@pytest.fixture()
def scenario(tmp_path: Path, scenario_root: Path, monkeypatch: pytest.MonkeyPatch):
    for name in ("AL_APP_ROOT", "AL_MODEL_SET", "AL_MODELS", "AL_MAX_COST", "AL_DASHBOARD_TOKEN", "AL_DECIDER"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "s"
    shutil.copytree(scenario_root, root, symlinks=True)
    running = start_server(["--layer", str(root / "pantry.al")])
    try:
        yield running, root / "pantry.al", root / "pantry"
    finally:
        stop_server(running)


@pytest.fixture()
def tab(browser):
    pages = []

    def make(running, width: int = 1280, height: int = 800):
        context = browser.new_context(viewport={"width": width, "height": height})
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.base = running.base  # type: ignore[attr-defined]
        page.errors = errors  # type: ignore[attr-defined]
        pages.append((context, page))
        return page

    yield make
    for context, page in pages:
        context.close()
        # a refused preview answers 4xx by design; the browser logs every failed request as an error
        assert not [e for e in page.errors if "favicon" not in e and "Failed to load resource" not in e], page.errors


def needs_you(page, count: int, timeout: int = 30000) -> None:
    until(page, f"document.querySelectorAll('#main tbody tr[data-key]').length === {count} && "
                f"!document.querySelector('#main td.reason')?.innerText.includes('checking')", timeout)


def row(page, run: str) -> dict:
    return page.evaluate("""(key) => { const tr = document.querySelector(`#main tbody tr[data-key="${key}"]`);
        return tr ? { reason: tr.querySelector('td.reason')?.innerText || '', next: tr.querySelector('td.next')?.innerText || '',
                      button: !!tr.querySelector('button.integrate-open'), text: tr.innerText,
                      landing: tr.querySelector('.landing')?.innerText || '' } : null; }""", f"pantry/{run}")


def wait_record(layer: Path, run: str, timeout: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout
    record: dict = {}
    while time.monotonic() < deadline:
        path = layer / "agents" / run / "integration.json"
        if path.is_file():
            record = json.loads(path.read_text())
            if record.get("status") not in ("queued", "running"):
                return record
        time.sleep(0.3)
    raise AssertionError(f"the integration of {run} did not end: {record}")


def open_preview(page, run: str) -> None:
    open_(page, f"/run/pantry/{run}")
    page.wait_for_selector(".settle-card button.integrate-open:not([disabled])")
    page.click(".settle-card button.integrate-open")
    page.wait_for_selector(".integrate-card .integrate-preview .kv, .integrate-card .integrate-refused")


def start_from_page(page, layer: Path, run: str) -> str:
    """Click Resolve with an agent on the run's page, confirm, and wait until the integration ended."""
    open_preview(page, run)
    page.click(".integrate-card button.integrate-start")
    until(page, f"location.hash.startsWith('#/run/pantry/') && !location.hash.includes('{run}')", 30000)
    new = page.evaluate("location.hash.split('/')[3].split('?')[0]")
    record = wait_record(layer, run)
    assert record["run"] == new and record["status"] == "landed", (layer / "agents" / new / "launch.log").read_text()[-3000:]
    return new


def settle_abandoned(page, run: str) -> None:
    open_(page, f"/run/pantry/{run}")
    page.wait_for_selector(".settle-card")
    page.select_option(".settle-card select.settle-as", "abandoned")
    page.fill(".settle-card input.settle-who", "Ada Example")
    page.click(".settle-card button.settle-go")
    until(page, "!document.querySelector('.settle-card')", 15000)


def test_needs_you_row_and_run_page_offer_the_button(scenario, tab):
    running, _layer, _repo = scenario
    page = tab(running)
    open_(page, "/runs")
    needs_you(page, 5)
    assert page.inner_text("#crumbs").strip() == "5 need you"
    for run in (RUN, fx.CLEAN):
        found = row(page, run)
        assert found["button"] and "Resolve with an agent" in found["next"], found
        assert "or `al integrate`, or `al settle`" in found["next"]
    rejected = row(page, fx.REJECTED)
    assert not rejected["button"] and "al integrate" not in rejected["next"] and "review-rejected" in rejected["reason"]
    no_personal_paths(page)
    shot(page, "integrate-needs-you-1280.png")
    page.click(f'#main tbody tr[data-key="pantry/{RUN}"] button.integrate-open')     # the row's button opens the preview
    until(page, f"location.hash.startsWith('#/run/pantry/{RUN}') && location.hash.includes('integrate=1')")
    page.wait_for_selector(".integrate-card .integrate-preview .kv")
    open_(page, f"/run/pantry/{fx.CLEAN}")
    page.wait_for_selector(".settle-card")
    buttons = page.evaluate("[...document.querySelectorAll('.settle-card .settle-actions button')].map((b) => b.innerText.trim())")
    assert buttons == ["Resolve with an agent", "Check if it has landed", "Find what replaced it"]
    assert not page.query_selector(".integrate-card"), "the preview opens only when asked"
    no_personal_paths(page)
    shot(page, "integrate-button-1280.png")
    open_(page, f"/run/pantry/{fx.REJECTED}")
    page.wait_for_selector(".settle-card")
    buttons = page.evaluate("[...document.querySelectorAll('.settle-card .settle-actions button')].map((b) => b.innerText.trim())")
    assert buttons == ["Check if it has landed", "Find what replaced it"], "a rejected run has no Resolve button"


def test_preview_shows_branch_target_and_conflicted_files(scenario, tab):
    running, layer, repo = scenario
    page = tab(running)
    before = sorted(p.name for p in (layer / "agents").iterdir())
    open_preview(page, RUN)
    text = page.inner_text(".integrate-card")
    tip = fx.git(repo, "rev-parse", f"refs/heads/{fx.branch_of(RUN)}")
    main = fx.git(repo, "rev-parse", "refs/heads/main")
    assert "what will happen; nothing starts until you confirm" in text
    assert f"{fx.branch_of(RUN)} at {tip[:7]}" in text and f"main at {main[:7]}" in text
    files = page.evaluate("[...document.querySelectorAll('.integrate-card ul.conflicts li')].map((li) => li.innerText.replace(/\\s+/g, ' ').trim())")
    assert files == ["INDEX.md generated: regenerated, not merged by hand", "stock.py"]
    assert "prepare → resolve → test → review → ship" in text
    assert "waits for your approval" not in text
    options = page.evaluate("[...document.querySelectorAll('.integrate-card select.integrate-tier option')].map((o) => o.value)")
    assert options[0] == "" and "heavy" in options and "base" in options
    assert page.is_enabled(".integrate-card button.integrate-start")
    no_personal_paths(page)
    shot(page, "integrate-preview-1280.png")
    narrow = tab(running, 390, 844)
    open_preview(narrow, RUN)
    narrow.locator(".integrate-card").scroll_into_view_if_needed()
    wide = narrow.evaluate("""[...document.querySelectorAll('.integrate-card, .integrate-card *')]
        .filter((el) => el.getBoundingClientRect().right > window.innerWidth + 1).map((el) => el.className || el.tagName)""")
    assert wide == [], f"the preview fits a phone's width: {wide}"
    shot(narrow, "integrate-preview-390.png")
    clean = tab(running)
    open_preview(clean, fx.CLEAN)
    assert "none: the branch applies cleanly" in clean.inner_text(".integrate-card")
    clean.click(".integrate-card button.integrate-close")
    until(clean, "!document.querySelector('.integrate-card')")
    assert sorted(p.name for p in (layer / "agents").iterdir()) == before, "looking starts nothing"


def test_click_resolve_lands_the_work_and_empties_needs_you(scenario, tab):
    running, layer, repo = scenario
    page = tab(running)
    main = fx.git(repo, "rev-parse", "refs/heads/main")
    open_(page, "/runs")
    needs_you(page, 5)
    page.click(f'#main tbody tr[data-key="pantry/{RUN}"] button.integrate-open')
    page.wait_for_selector(".integrate-card button.integrate-start:not([disabled])")
    page.click(".integrate-card button.integrate-start")
    until(page, f"location.hash.startsWith('#/run/pantry/') && !location.hash.includes('{RUN}')", 30000)
    new = page.evaluate("location.hash.split('/')[3].split('?')[0]")
    page.wait_for_selector(".g-node[data-step='prepare']")
    steps = page.evaluate("[...document.querySelectorAll('.g-node[data-step]')].map((n) => n.dataset.step)")
    assert steps == ["prepare", "resolve", "test", "review", "ship"]
    assert page.locator(".g-node[data-step='resolve'] rect.t-integrate").count() == 1
    assert f"integrates {RUN}" in page.inner_text(".run-head")
    no_personal_paths(page)
    shot(page, "integrate-running-1280.png")
    record = wait_record(layer, RUN)
    assert (record["run"], record["status"], record["by"]) == (new, "landed", "dashboard"), \
        (layer / "agents" / new / "launch.log").read_text()[-3000:]
    landed = fx.git(repo, "rev-parse", "refs/heads/main")
    assert landed != main and fx.git(repo, "log", "-1", "--format=%s", "main") == (
        f"chore: Integrate run {RUN} (Give stock a discount) onto main [adw {new}]")
    assert fx.git(repo, "show", "main:stock.py") + "\n" == fx.BOTH_STOCK
    until(page, "document.querySelector('.run-head .pill')?.innerText.trim().toLowerCase() === 'passed'", 30000)
    assert page.locator(".integrates-fact").inner_text() == RUN
    # the original: under Landed, with a link to the integration run
    open_(page, f"/run/pantry/{RUN}")
    page.wait_for_selector(".run-head a.landed-by")
    badge = page.locator(".run-head a.landed-by")
    assert badge.inner_text() == f"landed by {new} (integration)"
    assert badge.get_attribute("href") == f"#/run/pantry/{new}"
    assert page.locator(".integration-fact a").get_attribute("href") == f"#/run/pantry/{new}"
    assert f"{new} landed in {landed[:7]}" in page.locator(".integration-fact").inner_text()
    assert not page.query_selector(".settle-card"), "a landed run needs no settling"
    no_personal_paths(page)
    shot(page, "integrate-landed-original-1280.png")
    open_(page, "/runs?view=landed")
    until(page, f"!!document.querySelector('#main tbody tr[data-key=\"pantry/{RUN}\"] .landing')", 20000)
    assert row(page, RUN)["landing"].strip() == f"landed by {new} (integration)"
    shot(page, "integrate-runs-landed-1280.png")
    # the others: one more through an agent, the rest settled through the page
    second = start_from_page(page, layer, fx.CLEAN)
    assert fx.git(repo, "log", "-1", "--format=%s", "main").startswith(f"chore: Integrate run {fx.CLEAN} (Add a price table)")
    assert fx.git(repo, "rev-list", "--parents", "-n", "1", "main").split()[1] == landed, "it started from the first one's result"
    assert second != new
    for run in (fx.REJECTED, fx.PROTECTED, fx.CHANGED):
        settle_abandoned(page, run)
    for run, button in ((fx.LANDED, "settle-check-landed"), (fx.REPLACED, "settle-find-replaced")):
        open_(page, f"/run/pantry/{run}")
        page.wait_for_selector(".settle-card")
        page.click(f".settle-card button.{button}")
        page.wait_for_selector(".settle-evidence .evidence-proposal")
        page.fill(".settle-card input.settle-who", "Ada Example")
        page.click(".settle-card button.settle-go")
        until(page, "!document.querySelector('.settle-card')", 15000)
    open_(page, "/runs")
    until(page, "document.querySelectorAll('#main tbody tr[data-key]').length === 0 && "
                "document.querySelector('#main tbody')?.innerText.includes('nothing needs you')", 30000)
    assert page.inner_text("#crumbs").strip() in ("0 need you", "nothing needs you", "")
    until(page, "document.querySelector('[data-count=\"needs-you\"]').innerText.trim() in {'0': 1, '': 1}", 15000)
    no_personal_paths(page)
    shot(page, "integrate-nothing-needs-you-1280.png")


def test_check_if_landed_shows_evidence_then_settles(scenario, tab):
    running, layer, repo = scenario
    page = tab(running)
    open_(page, f"/run/pantry/{fx.LANDED}")
    page.wait_for_selector(".settle-card")
    squash = fx.git(repo, "rev-parse", "refs/heads/main~1")
    page.click(".settle-card button.settle-check-landed")
    page.wait_for_selector(".settle-evidence .evidence-proposal")
    lines = page.evaluate("[...document.querySelectorAll('.settle-evidence li')].map((li) => li.innerText)")
    assert len(lines) == 1 and "the same change (equal patch-id) is a commit of main" in lines[0] and squash[:7] in lines[0]
    assert page.inner_text(".settle-evidence .evidence-proposal").startswith(f"Proposed: settle as landed by {squash[:7]}.")
    assert page.input_value(".settle-card select.settle-as") == "landed"
    assert page.input_value(".settle-card input.settle-by") == squash[:7]
    assert page.input_value(".settle-card textarea.settle-note-text") == "landed (patch-id) on main"
    assert page.inner_text(".settle-card button.settle-go") == f"Settle as landed by {squash[:7]}"
    assert not (layer / "agents" / fx.LANDED / "settlement.json").exists(), "nothing is written before the click"
    no_personal_paths(page)
    shot(page, "integrate-evidence-landed-1280.png")
    page.fill(".settle-card input.settle-who", "Ada Example")
    page.click(".settle-card button.settle-go")
    until(page, "!document.querySelector('.settle-card')", 15000)
    record = json.loads((layer / "agents" / fx.LANDED / "settlement.json").read_text())
    assert (record["as"], record["by"], record["note"], record["who"], record["via"]) == (
        "landed", squash[:7], "landed (patch-id) on main", "Ada Example", "dashboard")
    # a run that has not landed: the evidence says what was compared, and nothing is proposed
    open_(page, f"/run/pantry/{RUN}")
    page.wait_for_selector(".settle-card")
    page.click(".settle-card button.settle-check-landed")
    page.wait_for_selector(".settle-evidence .evidence-none")
    assert "is not on main" in page.inner_text(".settle-evidence") and "Nothing to propose" in page.inner_text(".settle-evidence")
    assert page.inner_text(".settle-card button.settle-go") == "Settle"


def test_find_what_replaced_it_proposes_run(scenario, tab):
    running, layer, _repo = scenario
    page = tab(running)
    open_(page, f"/run/pantry/{fx.REPLACED}")
    page.wait_for_selector(".settle-card")
    page.click(".settle-card button.settle-find-replaced")
    page.wait_for_selector(".settle-evidence .evidence-proposal")
    lines = page.evaluate("[...document.querySelectorAll('.settle-evidence li')].map((li) => li.innerText)")
    assert lines == [f"run {fx.REPLACEMENT} (passed, landed) names run {fx.REPLACED} in its request"]
    assert page.input_value(".settle-card select.settle-as") == "superseded"
    assert page.input_value(".settle-card input.settle-by") == fx.REPLACEMENT
    assert page.inner_text(".settle-card button.settle-go") == f"Settle as superseded by {fx.REPLACEMENT}"
    assert not (layer / "agents" / fx.REPLACED / "settlement.json").exists()
    no_personal_paths(page)
    shot(page, "integrate-evidence-replaced-1280.png")
    page.fill(".settle-card input.settle-who", "Ada Example")
    page.click(".settle-card button.settle-go")
    until(page, "!document.querySelector('.settle-card')", 15000)
    record = json.loads((layer / "agents" / fx.REPLACED / "settlement.json").read_text())
    assert (record["as"], record["by"], record["via"]) == ("superseded", fx.REPLACEMENT, "dashboard")
    assert record["note"] == f"superseded by {fx.REPLACEMENT}: Redo {fx.REPLACED} on the new price table"
    open_(page, f"/run/pantry/{RUN}")
    page.wait_for_selector(".settle-card")
    page.click(".settle-card button.settle-find-replaced")
    page.wait_for_selector(".settle-evidence .evidence-none")
    assert "no later run names it" in page.inner_text(".settle-evidence")


def test_refused_integration_says_why(scenario, tab):
    running, layer, _repo = scenario
    page = tab(running)
    open_preview(page, fx.CHANGED)
    page.wait_for_selector(".integrate-card .integrate-refused")
    text = page.inner_text(".integrate-card")
    assert "cannot be integrated" in text
    assert "branch-changed: a person changed chore/f0000005-round-the-total since the run ended" in text
    assert not page.query_selector(".integrate-card button.integrate-start")
    no_personal_paths(page)
    open_preview(page, fx.PROTECTED)
    assert "protected-path: run f0000004 changes deploy/release.cfg" in page.inner_text(".integrate-card")
    assert fx.integration_runs(layer) == []
