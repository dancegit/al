"""Real-browser tests of what a layer can run, as the page shows it: the Layers view's "Kit workflows"
group (each with its `kit` mark; one that is off dimmed, with the setting that turns it on), the
start form's run picker for `integrate` (the handed-over runs it applies to, or the reason there
are none), starting the integration from the form, and the kit run's page (the mark, its steps, the
link to the run it integrates). The scenario is integrate_fixtures (an invented pantry with real git
history; the layer's own adws/integrate.py runs with the mock agent).

    uv run --with pytest --with playwright pytest kit/dashboard/tests/test_browser_registry.py

Skipped without Playwright or Chrome (nothing is downloaded). With AL_DASHBOARD_SHOTS=<directory> the
tests save layers-kit-workflows-1280.png, start-integrate-picker-1280.png,
start-integrate-picker-empty-1280.png and kit-run-page-1280.png there."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

import dashboard as d
import integrate_fixtures as fx
from conftest import start_server, stop_server

pytest.importorskip("playwright.sync_api")
from test_browser import browser, open_, until  # noqa: E402,F401 — fixtures; skips without Chrome
from test_browser_integrate import tab, wait_record  # noqa: E402,F401 — the tab fixture checks page errors
from test_browser_workspace import no_personal_paths, shot  # noqa: E402

pytestmark = pytest.mark.browser
RUN = fx.ACCEPTANCE
APPLICABLE = [fx.ACCEPTANCE, fx.CLEAN, fx.PROTECTED, fx.CHANGED]


@pytest.fixture(scope="module")
def scenario_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("browser-registry") / "s"
    fx.build_scenario(root)
    return root


@pytest.fixture()
def copy(tmp_path: Path, scenario_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("AL_APP_ROOT", "AL_MODEL_SET", "AL_MODELS", "AL_MAX_COST", "AL_DASHBOARD_TOKEN", "AL_DECIDER"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "s"
    shutil.copytree(scenario_root, root, symlinks=True)
    return root / "pantry.al"


@pytest.fixture()
def scenario(copy: Path):
    running = start_server(["--layer", str(copy)])
    try:
        yield running, copy
    finally:
        stop_server(running)


def candidates(running) -> list[str]:
    kit = running.json("/api/startable?layer=pantry")["kit"]
    return [c["id"] for c in next(k for k in kit if k["name"] == "integrate").get("candidates") or []]


def wait_candidates(running, count: int, timeout: float = 30.0) -> None:
    """The watcher has looked at every run (landed and settled ones are not listed) before the form loads."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(candidates(running)) == count and running.json("/api/snapshot")["runs"]:
            buckets = {r["id"]: r.get("bucket") for r in running.json("/api/snapshot")["runs"]}
            if buckets.get(fx.LANDED) == "landed":
                return
        time.sleep(0.2)
    raise AssertionError(f"the picker never had {count} runs: {candidates(running)}")


def choose_integrate(page) -> None:
    open_(page, "/layers")
    page.wait_for_selector(".start-panel select.start-target")
    page.select_option(".start-panel select.start-target", "kit:integrate")
    page.wait_for_selector(".start-panel .start-kit:not([hidden])")


def start_from_form(page, layer: Path, run: str) -> str:
    """Choose `integrate` in the start form, pick `run`, confirm; wait until the integration ended."""
    choose_integrate(page)
    page.select_option(".start-kit select.start-run", run)
    page.wait_for_selector(".start-kit .integrate-card .integrate-preview .kv")
    page.wait_for_selector(".start-kit .integrate-card button.integrate-start:not([disabled])")
    page.click(".start-kit .integrate-card button.integrate-start")
    until(page, f"location.hash.startsWith('#/run/pantry/') && !location.hash.includes('{run}')", 30000)
    new = page.evaluate("location.hash.split('/')[3].split('?')[0]")
    record = wait_record(layer, run)
    assert record["run"] == new and record["status"] == "landed", (layer / "agents" / new / "launch.log").read_text()[-3000:]
    return new


def test_layers_view_shows_the_kit_workflows(scenario, tab):
    running, _layer = scenario
    page = tab(running)
    open_(page, "/layer/pantry")
    page.wait_for_selector('.kit-section[data-kit="integrate"]')
    found = page.evaluate("""() => ({
        group: document.querySelector('.kit-group h2')?.textContent || '',
        sections: [...document.querySelectorAll('.kit-section')].map((el) => ({
            name: el.dataset.kit, badge: el.querySelector('.card-head .badge.kit')?.innerText || '',
            off: el.classList.contains('off'), text: el.innerText, graph: !!el.querySelector('.graph-wrap svg, .graph-wrap') })),
        declared: [...document.querySelectorAll('.adw-section:not(.kit-section):not(.commands-section):not(.suite-section) h3')]
            .map((el) => el.innerText),
        commands: [...document.querySelectorAll('.commands-section .command-row')].map((el) => el.dataset.command),
        order: [...document.querySelectorAll('.adw-section, .run-section, .kit-group')].map((el) => el.className) })""")
    assert found["group"] == "Kit workflows"
    assert [s["name"] for s in found["sections"]] == ["integrate", "assign", "promote", "improve", "decider"]
    assert all(s["badge"] == "kit" for s in found["sections"])
    assert found["declared"] == ["plan-build", "ship-it"], "the declared workflows are still listed first"
    by_name = {s["name"]: s for s in found["sections"]}
    integrate = by_name["integrate"]
    assert not integrate["off"] and integrate["graph"]
    assert "al integrate LAYER RUN" in integrate["text"] and "templates/integrate.json (enabled): on" in integrate["text"]
    promote = by_name["promote"]                         # no stages in this layer: off, with what turns it on
    assert promote["off"] and "off" in promote["text"].split()
    assert "agentic.rkt (stages): off" in promote["text"]
    assert "turn it on: declare stages, e.g. `(delivery sprint)` in agentic.rkt, then `al gen`" in promote["text"]
    assert 'al improve LAYER "…"' in by_name["improve"]["text"] and not by_name["improve"]["off"]
    assert by_name["decider"]["off"] and "al decider LAYER --set suggest|auto" in by_name["decider"]["text"]
    assert found["commands"] == ["classify", "plan-chore", "plan-bug", "plan-feature", "review", "implement"]
    kit_at = next(i for i, name in enumerate(found["order"]) if "kit-group" in name)
    assert all("kit-section" not in name for name in found["order"][:kit_at]), "the kit group comes after the declared ones"
    no_personal_paths(page)
    page.evaluate("document.querySelector('.kit-group').scrollIntoView(); window.scrollBy(0, -64)")   # below the top bar
    shot(page, "layers-kit-workflows-1280.png")


def test_start_form_offers_integrate_with_a_run_picker(scenario, tab):
    running, layer = scenario
    wait_candidates(running, 4)
    page = tab(running)
    open_(page, "/layers")
    page.wait_for_selector(".start-panel select.start-target")
    groups = page.evaluate("""() => [...document.querySelectorAll('.start-panel select.start-target optgroup')].map((g) =>
        ({ label: g.label, options: [...g.querySelectorAll('option')].map((o) => ({ value: o.value, text: o.text, disabled: o.disabled })) }))""")
    assert [g["label"] for g in groups][0] == "Workflows" and groups[-1]["label"] == "Kit workflows"
    kit = {o["value"]: o for o in groups[-1]["options"]}
    assert list(kit) == ["kit:integrate", "kit:assign", "kit:promote", "kit:improve", "kit:decider"]
    assert not kit["kit:integrate"]["disabled"] and kit["kit:integrate"]["text"] == "/integrate"
    assert kit["kit:promote"]["disabled"] and kit["kit:promote"]["text"] == "/promote (off)"
    assert kit["kit:improve"]["disabled"] and kit["kit:decider"]["disabled"]
    assert not kit["kit:assign"]["disabled"] and kit["kit:assign"]["text"] == "/assign"
    # the /NAME menu lists the kit entries too, marked
    page.fill(".start-panel textarea.start-prompt", "/in")
    page.wait_for_selector(".start-panel .start-complete li")
    menu = page.evaluate("""() => [...document.querySelectorAll('.start-panel .start-complete li')].map((li) =>
        ({ value: li.dataset.value, kit: !!li.querySelector('.badge.kit') }))""")
    assert {"value": "kit:integrate", "kit": True} in menu
    page.fill(".start-panel textarea.start-prompt", "")
    # choosing it replaces the prompt box with the picker
    page.select_option(".start-panel select.start-target", "kit:integrate")
    page.wait_for_selector(".start-panel .start-kit select.start-run")
    state = page.evaluate("""() => ({
        prompt: document.querySelector('.start-panel .start-input').hidden,
        go: document.querySelector('.start-panel .start-actions').hidden,
        runs: [...document.querySelectorAll('.start-kit select.start-run option')].map((o) => ({ value: o.value, text: o.text })),
        badge: document.querySelector('.start-kit .badge.kit')?.innerText || '' })""")
    assert state["prompt"] is True and state["go"] is True and state["badge"] == "kit"
    assert [r["value"] for r in state["runs"]] == ["", *sorted(APPLICABLE, reverse=True)]
    listed = {r["value"]: r["text"] for r in state["runs"]}
    assert listed[RUN] == f"{RUN} — Give stock a discount → main"
    assert fx.REJECTED not in listed and fx.LANDED not in listed and fx.REPLACED not in listed
    no_personal_paths(page)
    shot(page, "start-integrate-picker-1280.png")
    # a run the runtime refuses says so in the form, as on the run's page
    page.select_option(".start-kit select.start-run", fx.PROTECTED)
    page.wait_for_selector(".start-kit .integrate-card .integrate-refused")
    assert "protected-path" in page.inner_text(".start-kit .integrate-card .integrate-refused")
    assert not page.query_selector(".start-kit .integrate-card button.integrate-start")
    # choosing the acceptance run shows what will happen; confirming starts it and opens its page
    page.select_option(".start-kit select.start-run", RUN)
    page.wait_for_selector(".start-kit .integrate-card .integrate-preview .kv")
    preview = page.inner_text(".start-kit .integrate-card .integrate-preview")
    assert "stock.py" in preview and "prepare → resolve → test → review → ship" in preview
    page.wait_for_selector(".start-kit .integrate-card button.integrate-start:not([disabled])")
    page.click(".start-kit .integrate-card button.integrate-start")
    until(page, f"location.hash.startsWith('#/run/pantry/') && !location.hash.includes('{RUN}')", 30000)
    new = page.evaluate("location.hash.split('/')[3].split('?')[0]")
    record = wait_record(layer, RUN)
    assert record["run"] == new and record["status"] == "landed", (layer / "agents" / new / "launch.log").read_text()[-3000:]
    assert fx.state_of(layer, new)["adw"] == "integrate"
    # back in the form the run is gone from the picker, and a workflow still takes a prompt
    wait_candidates(running, 3)
    choose_integrate(page)
    page.wait_for_selector(".start-panel .start-kit select.start-run")
    assert RUN not in page.evaluate("[...document.querySelectorAll('.start-kit select.start-run option')].map((o) => o.value)")
    page.select_option(".start-panel select.start-target", "workflow:plan-build")
    assert page.evaluate("document.querySelector('.start-panel .start-kit').hidden && "
                         "!document.querySelector('.start-panel .start-input').hidden && "
                         "!document.querySelector('.start-panel .start-actions').hidden")


def test_start_form_says_why_there_is_no_run_to_pick(copy, tab):
    for run in APPLICABLE:    # every handed-over run that could be integrated is settled by a person
        d.write_settlement(copy, run, as_="landed", who="Ada Example", via="dashboard", status="handed-over")
    running = start_server(["--layer", str(copy)])
    try:
        wait_candidates(running, 0)
        page = tab(running)
        choose_integrate(page)
        page.wait_for_selector(".start-panel .start-kit .start-none")
        assert page.inner_text(".start-panel .start-kit .start-none").strip() == (
            "no run of this layer is handed over because of a merge conflict or a moved target")
        assert not page.query_selector(".start-kit select.start-run")
        assert page.evaluate("document.querySelector('.start-panel .start-input').hidden")
        no_personal_paths(page)
        shot(page, "start-integrate-picker-empty-1280.png")
    finally:
        stop_server(running)


def test_kit_run_page_shows_the_mark_its_steps_and_what_it_integrates(scenario, tab):
    running, layer = scenario
    wait_candidates(running, 4)
    page = tab(running)
    new = start_from_form(page, layer, RUN)
    open_(page, f"/run/pantry/{new}")
    page.wait_for_selector("#main .run-head .badge.kit")
    until(page, "document.querySelector('#main .run-head')?.innerText.includes('passed') || "
                "!!document.querySelector('#main .run-head .pill.s-passed')", 60000)
    head = page.evaluate("""() => ({
        title: document.querySelector('#main .run-head h1')?.innerText || '',
        kit: document.querySelector('#main .run-head .line1 .badge.kit')?.innerText || '',
        link: document.querySelector('#main .run-head a.acts-on')?.getAttribute('href') || '',
        text: document.querySelector('#main .run-head a.acts-on')?.innerText || '',
        steps: [...new Set([...document.querySelectorAll('#main [data-step]')].map((el) => el.dataset.step))] })""")
    assert head["title"] == "integrate" and head["kit"] == "kit"
    assert head["link"] == f"#/run/pantry/{RUN}" and head["text"] == f"integrates {RUN}"
    body = page.inner_text("#main")
    for step in ("prepare", "resolve", "test", "review", "ship"):
        assert step in body, step
    no_personal_paths(page)
    shot(page, "kit-run-page-1280.png")
    # the link goes to the run it integrates; a declared workflow's run has no mark
    page.click("#main .run-head a.acts-on")
    until(page, f"location.hash.startsWith('#/run/pantry/{RUN}')", 10000)
    page.wait_for_selector("#main .run-head h1")
    until(page, "document.querySelector('#main .run-head h1')?.innerText === 'ship-it'", 10000)
    assert not page.query_selector("#main .run-head .badge.kit")
