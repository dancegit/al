"""Real-browser tests of the runs list that opens on what needs someone: the chips with their counts,
the reason and next action of each open run, what landed or settled the others, a failure group
with one "Start all again", and the settle form on a run page. The scenario is settle_fixtures (an
invented orchard with real git history).

    uv run --with pytest --with playwright pytest kit/dashboard/tests/test_browser_runs_settle.py

Skipped without Playwright or Chrome (nothing is downloaded). With AL_DASHBOARD_SHOTS=<directory> the
tests save runs-needs-you-1280.png, runs-needs-you-390.png, runs-landed-1280.png,
runs-settled-1280.png, runs-failure-group-1280.png and run-settle-form-1280.png there."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

import settle_fixtures as sf
from conftest import start_server, stop_server

pytest.importorskip("playwright.sync_api")
from test_browser import browser, open_, until  # noqa: E402,F401 — fixtures; skips without Chrome
from test_browser_workspace import no_personal_paths, shot  # noqa: E402
from test_start import wait_state  # noqa: E402

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def scenario_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("runs-settle") / "s"
    sf.build_scenario(root)
    return root


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AL_APP_ROOT", "AL_MODEL_SET", "AL_MODELS", "AL_MAX_COST", "AL_DASHBOARD_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def scenario(tmp_path: Path, scenario_root: Path, clean_env):
    root = tmp_path / "s"
    shutil.copytree(scenario_root, root, symlinks=True)
    running = start_server(["--layer", str(root / "orchard.al")])
    try:
        yield running, root / "orchard.al"
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
        assert not [e for e in page.errors if "favicon" not in e], page.errors


def rows(page) -> list[dict]:
    return page.evaluate("""[...document.querySelectorAll('#main tbody tr[data-key]')].map((tr) => ({
        key: tr.dataset.key, text: tr.innerText,
        reason: (tr.querySelector('td.reason') || {}).innerText || '',
        next: (tr.querySelector('td.next') || {}).innerText || '',
        landing: (tr.querySelector('.landing') || {}).innerText || ''}))""")


def chips(page) -> list[str]:
    return page.evaluate("[...document.querySelectorAll('.chips .chip')].map((c) => c.textContent.replace(/\\s+/g, ' ').trim())")


def settled_counts(page) -> None:
    """Wait until every landed check has run (no run says `checking`)."""
    until(page, "document.querySelectorAll('.chips .chip').length === 5 && "
                "document.querySelector('.chips .chip[data-view=landed]').innerText.includes('(8)')", 20000)


def test_runs_opens_on_needs_you_with_only_open_runs(scenario, tab):
    running, _layer = scenario
    page = tab(running)
    open_(page, "/runs")
    settled_counts(page)
    until(page, "document.querySelectorAll('#main tbody tr[data-key]').length === 6")
    got = rows(page)
    ids = sorted(r["key"].split("/", 1)[1] for r in got)
    assert ids == sorted(sf.OPEN + [sf.WAITING, sf.OLD_FAILURE])
    assert all(r["reason"].strip() and r["next"].strip() for r in got), got
    by_id = {r["key"].split("/", 1)[1]: r for r in got}
    assert by_id[sf.PR_RUN]["next"] == "review PR #7"
    assert by_id[sf.OLD_FAILURE]["next"] == "Start again"
    assert by_id[sf.WAITING]["reason"] == "waiting at approve"
    assert page.inner_text("#crumbs").strip() == "6 need you"
    assert chips(page) == ["Needs you (6)", "Running (5)", "Landed (8)", "Settled (7)", "All (27)"]
    assert page.inner_text('[data-count="needs-you"]').strip() == "6"
    shot(page, "runs-needs-you-1280.png")
    narrow = tab(running, 390, 844)
    open_(narrow, "/runs")
    until(narrow, "document.querySelectorAll('#main tbody tr[data-key]').length === 6", 20000)
    shot(narrow, "runs-needs-you-390.png")


def test_landed_and_settled_chips_show_what_landed_them(scenario, tab):
    running, _layer = scenario
    page = tab(running)
    open_(page, "/runs")
    settled_counts(page)
    page.click(".chips .chip[data-view=landed]")
    until(page, "document.querySelectorAll('#main tbody tr[data-key]').length === 8")
    assert "view=landed" in page.url
    landed = rows(page)
    for row in landed:
        assert re.fullmatch(r"landed in [0-9a-f]{7} \((ancestor|patch-id|paths)\), \d{4}-\d\d-\d\d", row["landing"].strip()), row
    assert {re.search(r"\((.+)\)", r["landing"]).group(1) for r in landed} == {"ancestor", "patch-id"}
    shot(page, "runs-landed-1280.png")
    page.click(".chips .chip[data-view=settled]")
    until(page, "document.querySelectorAll('#main tbody tr[data-key]').length === 7")
    settled = {r["key"].split("/", 1)[1]: r["landing"].strip() for r in rows(page)}
    for run_id in sf.INTEGRATED:
        assert settled[run_id] == f"superseded by {sf.INTEGRATION}"
    for old, new in zip(sf.FAILED, sf.RESTARTS):
        assert settled[old].startswith(f"replaced by {new}")
    shot(page, "runs-settled-1280.png")
    page.click(".chips .chip[data-view=all]")
    until(page, "document.querySelectorAll('#main tbody tr[data-key]').length === 27")


def test_failure_group_row_and_start_all_again(tmp_path, tab, clean_env):
    sc = sf.build_group_scenario(tmp_path / "g")
    layer = sc["layer"]
    running = start_server(["--layer", str(layer)])
    try:
        page = tab(running)
        posts: list[dict] = []
        page.on("request", lambda req: posts.append(json.loads(req.post_data or "{}"))
                if req.method == "POST" and req.url.endswith("/api/start") else None)
        open_(page, "/runs")
        until(page, "document.querySelectorAll('#main tr.group-row').length === 1")
        assert page.evaluate("document.querySelectorAll('#main tbody tr[data-key]').length") == 0   # collapsed
        group = page.inner_text("#main tr.group-row")
        assert "5 runs: preflight, same reason" in group
        assert page.inner_text("#crumbs").strip() == "5 need you"
        page.click("#main tr.group-row button.toggle")
        until(page, "document.querySelectorAll('#main tbody tr.group-member').length === 5")
        shot(page, "runs-failure-group-1280.png")
        page.click("#main tr.group-row button.start-all")
        page.wait_for_selector(".start-confirm")
        assert "d0000001" in page.inner_text(".start-confirm")
        page.click(".start-confirm .confirm-go")
        until(page, "document.querySelectorAll('#main .start-report .ok').length === 6", 60000)   # a header, then one per run
        assert sorted(p.get("again") for p in posts) == sf.FAILED and len(posts) == 5   # one POST per run
        assert all(p.get("request_key") == f"again-{p['again']}" and p.get("layer") == "orchard" for p in posts)
        notes = page.evaluate("[...document.querySelectorAll('#main .start-report .ok')].slice(1).map((n) => n.innerText)")
        pairs = dict(line.split(" → ") for line in notes)
        assert sorted(pairs) == sf.FAILED
        for old, new in pairs.items():
            assert json.loads((layer / "agents" / new / "state.json").read_text())["replaces"] == old
        started = list(pairs.values())
        until(page, "document.querySelectorAll('#main tr.group-row').length === 0", 20000)   # all replaced now
        shot(page, "runs-failure-group-started-1280.png")
        for new in started:
            wait_state(layer, new)
    finally:
        stop_server(running)


def test_settle_form_on_run_page_moves_run(scenario, tab):
    running, layer = scenario
    page = tab(running)
    run = sf.OPEN[0]
    open_(page, f"/run/orchard/{run}")
    page.wait_for_selector(".settle-card")
    page.select_option(".settle-card select.settle-as", "abandoned")
    page.fill(".settle-card textarea.settle-note-text", "the orchard map replaces this work")
    page.fill(".settle-card input.settle-who", "Ada")
    shot(page, "run-settle-form-1280.png")
    page.click(".settle-card button.settle-go")
    # the answer shows in the card until the page reloads the run, which then has no settle card
    until(page, "!document.querySelector('.settle-card') || !!document.querySelector('.settle-note .badge')", 15000)
    assert not page.query_selector(".settle-note .badge.warn")
    record = json.loads((layer / "agents" / run / "settlement.json").read_text())
    assert (record["as"], record["note"], record["who"], record["via"]) == (
        "abandoned", "the orchard map replaces this work", "Ada", "dashboard")
    until(page, "!document.querySelector('.settle-card')")   # settled: the form is gone
    assert "abandoned by Ada" in page.inner_text(".run-head")
    open_(page, "/runs")
    until(page, "document.querySelectorAll('#main tbody tr[data-key]').length === 5")
    assert run not in [r["key"].split("/", 1)[1] for r in rows(page)]
    assert page.inner_text("#crumbs").strip() == "5 need you"
    until(page, "document.querySelector('[data-count=\"needs-you\"]').innerText.trim() === '5'")


def test_no_personal_paths_on_runs_page(scenario, tab):
    running, _layer = scenario
    page = tab(running)
    open_(page, "/runs")
    settled_counts(page)
    for view in ("needs-you", "running", "landed", "settled", "all"):
        page.click(f".chips .chip[data-view={view}]")
        page.wait_for_selector(f".chips .chip.active[data-view={view}]")
        no_personal_paths(page)
    open_(page, f"/run/orchard/{sf.OLD_FAILURE}")
    page.wait_for_selector(".settle-card")
    assert page.is_visible(".run-head button.start-again")
    no_personal_paths(page)
