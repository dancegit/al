"""Real-browser test of the Automatic switch of "Needs you": the switch beside "Give all N to agents",
its confirmation (and Cancel, which changes nothing), the runs the stand-in's loop then gives to an
agent and the one it leaves with its reason, as report lines under the switch, and switching it off.
The scenario is assign_fixtures (the invented pantry with real git history; the layer's own
adws/assign.py runs with the mock agent).

    uv run --with pytest --with playwright pytest kit/dashboard/tests/test_browser_assign_auto.py

Skipped without Playwright or Chrome (nothing is downloaded). With AL_DASHBOARD_SHOTS=<directory> the
test saves assign-auto-confirm-1280.png and assign-auto-on-1280.png there."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import assign_fixtures as af
import dashboard as d
import integrate_fixtures as fx
from conftest import start_server, stop_server

pytest.importorskip("playwright.sync_api")
from test_browser import browser, open_, until  # noqa: E402,F401 — fixtures; skips without Chrome
from test_browser_assign import needs_you, tab, wait_ended  # noqa: E402,F401 — `tab` is a fixture
from test_browser_workspace import no_personal_paths, shot  # noqa: E402

pytestmark = pytest.mark.browser
SETTING = "agents/settings/assign.json"
BUTTON = "button.assign-auto"


@pytest.fixture(scope="module")
def scenario_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("browser-assign-auto") / "s"
    af.build_scenario(root)
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


def switch_is(page, state: str, timeout: int = 30000) -> None:
    pressed = "true" if state == "on" else "false"
    until(page, f"(() => {{ const b = document.querySelector('{BUTTON}'); return b && b.innerText.trim() === 'Automatic: {state}'"
                f" && b.getAttribute('aria-pressed') === '{pressed}' && !b.disabled; }})()", timeout)


def test_automatic_switch_gives_the_runs_and_says_what_stays(scenario, tab):
    running, layer, _repo = scenario
    for run in af.KINDS:
        if run not in (af.FAILED_CHECK, af.PERSON):
            d.write_settlement(layer, run, as_="abandoned", who="Ada Example", via="dashboard",
                               status=fx.state_of(layer, run)["status"])
    page = tab(running)
    open_(page, "/runs")
    needs_you(page, 2)
    switch_is(page, "off")
    assert "automatic on" not in page.inner_text("#crumbs")
    assert "gives every run of this layer under Needs you to an agent, once" in page.get_attribute(BUTTON, "title")
    # switching on asks first; Cancel changes nothing
    page.click(BUTTON)
    page.wait_for_selector(".start-confirm")
    words = page.inner_text(".start-confirm")
    assert "Give every run of pantry to an agent automatically?" in words and "2 runs wait now." in words
    assert "what only a person may decide stays with you" in words and "until you switch it off" in words
    assert page.inner_text(".start-confirm button.confirm-go").strip() == "Switch on"
    no_personal_paths(page)
    shot(page, "assign-auto-confirm-1280.png")
    page.click(".start-confirm button.confirm-cancel")
    until(page, "document.getElementById('overlay').hidden")
    switch_is(page, "off")
    assert not (layer / SETTING).exists() and not list((layer / "agents").glob("f*/assign.json"))
    # switching on: the loop gives what can be given, oldest stop first, and says what stays
    page.click(BUTTON)
    page.wait_for_selector(".start-confirm button.confirm-go")
    page.click(".start-confirm button.confirm-go")
    switch_is(page, "on")
    assert (layer / SETTING).is_file()
    until(page, "document.querySelectorAll('.assign-auto-log div').length === 2", 60000)
    lines = page.evaluate("[...document.querySelectorAll('.assign-auto-log div')].map((el) => el.innerText)")
    assert lines[0].startswith(f"{af.PERSON}: stays with you: needs-a-person: "), lines
    assert lines[1] == f"{af.FAILED_CHECK} → resumed in place (check)", lines
    until(page, "document.querySelector('#crumbs .assign-auto-count')?.innerText.includes('automatic on')")
    no_personal_paths(page)
    shot(page, "assign-auto-on-1280.png")
    attempts = af.record_of(layer, af.FAILED_CHECK)["attempts"]
    assert [(a["by"], a["kind"], a["run"]) for a in attempts] == [("decider", "check", af.FAILED_CHECK)]
    assert not (layer / "agents" / af.PERSON / "assign.json").exists()
    wait_ended(layer, af.FAILED_CHECK)
    # switching off asks nothing
    page.click(BUTTON)
    switch_is(page, "off")
    assert page.evaluate("document.getElementById('overlay').hidden")
    assert not (layer / SETTING).exists()
    assert "automatic on" not in page.inner_text("#crumbs")
