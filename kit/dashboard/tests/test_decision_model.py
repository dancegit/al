"""The stand-in decider's decision on the run page: the run detail model carries `decision` (mode,
action, from step, summary, status, last answer, decision.md), hostile text in it is data and never
markup, and the page shows a Decision block. A suggestion is answered on the page too (see
test_standin_http.py), through the same entry as `al answer`, an e-mail reply and the webhook.

    uv run --with pytest --with playwright pytest kit/dashboard/tests/test_decision_model.py

The browser test is skipped without Playwright or Chrome (nothing is downloaded). With
AL_DASHBOARD_SHOTS=<directory> it saves decision-block.png there."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import dashboard as d
from conftest import start_server, stop_server

RUN = "a1000002"   # the fixture's failed run that was handed to a person (PR.md)
HOSTILE = '<img src=x onerror="window.__pwned=1"> resume <b>now</b>'


def _with_decision(fx: dict[str, Path], summary: str = "The test check is flaky; run it again from build.") -> None:
    run_dir = fx["agents"] / RUN
    state = json.loads((run_dir / "state.json").read_text())
    state["decision"] = {"mode": "suggest", "mode_from": "local setting", "action": "resume-with", "from_step": "build",
                         "stopped_at": "test", "summary": summary, "explanation": "The refund test fails on a fixture.",
                         "instructions": "Rebuild the refund fixture first.", "code": "Abc_123-xyz9", "rounds": 0,
                         "status": "resuming", "at": "2026-11-01T10:20:00+00:00",
                         "answers": [{"kind": "go", "text": "", "via": "mail", "at": "2026-11-01T10:30:00+00:00"}]}
    (run_dir / "state.json").write_text(json.dumps(state))
    (run_dir / "decision.md").write_text("# Decision for run a1000002\n\n- mode: suggest\n- action: resume-with\n")


def _detail(fx: dict[str, Path]) -> dict:
    model = d.Model([fx["layer"]], [])
    model.discover()
    model.tick()
    return model.run_detail("demo-shop", RUN)


def test_run_detail_carries_the_decision(fx):
    assert _detail(fx)["decision"] is None                         # a run the decider never looked at
    _with_decision(fx)
    decision = _detail(fx)["decision"]
    assert {k: decision[k] for k in ("mode", "action", "from_step", "status", "file")} == {
        "mode": "suggest", "action": "resume-with", "from_step": "build", "status": "resuming", "file": "decision.md"}
    assert decision["summary"] == "The test check is flaky; run it again from build."
    assert decision["answer"] == {"kind": "go", "via": "mail", "at": "2026-11-01T10:30:00+00:00", "text": ""}
    assert "code" not in decision                                       # the answer code stays in state.json
    # what the stand-in's suggestion card shows: its kind, why, how sure it is and what it read
    assert decision["explanation"] == "The refund test fails on a fixture."
    assert (decision["kind"], decision["confidence"], decision["read"], decision["situation"]) == ("", None, [], "")
    state = json.loads((fx["agents"] / RUN / "state.json").read_text())
    state["decision"].update(kind="resume", confidence=0.75, read=["why it stopped", "PR.md"], situation="stopped")
    (fx["agents"] / RUN / "state.json").write_text(json.dumps(state))
    decision = _detail(fx)["decision"]
    assert (decision["kind"], decision["confidence"], decision["read"], decision["situation"]) == (
        "resume", 0.75, ["why it stopped", "PR.md"], "stopped")


def test_a_hostile_decision_is_data_not_markup(fx):
    _with_decision(fx, summary=HOSTILE)
    assert _detail(fx)["decision"]["summary"] == HOSTILE
    running = start_server(["--layer", str(fx["layer"])])
    try:
        status, _headers, body = running.get(f"/api/run?layer=demo-shop&run={RUN}")
        assert status == 200
        assert json.loads(body)["decision"]["summary"] == HOSTILE
        assert b"<img" not in body                                   # JSON escapes markup: <
    finally:
        stop_server(running)


@pytest.mark.browser
def test_the_page_shows_the_decision_block(fx):
    from test_browser import CHROME, open_, until, playwright_api  # noqa: F401 — skips without Chrome
    _with_decision(fx, summary="The test check is flaky; run it again from build. " + HOSTILE)
    running = start_server(["--layer", str(fx["layer"])])
    try:
        with playwright_api.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROME, headless=True)
            page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.base = running.base  # type: ignore[attr-defined]
            open_(page, f"/run/demo-shop/{RUN}")
            until(page, "!!document.querySelector('.decision-block')")
            block = page.locator(".decision-block")
            assert block.locator("h3").inner_text() == "Decision"
            assert block.locator(".badge").inner_text() == "answered: go"
            text = block.inner_text()
            for needle in ("suggest", "resume-with", "build", "resuming", "go via mail", "decision.md", "<img"):
                assert needle in text, (needle, text)
            assert block.locator("a", has_text="decision.md").get_attribute("href").endswith(f"/agents/{RUN}/decision.md")
            assert page.evaluate("window.__pwned") is None and block.locator("img").count() == 0
            shots = os.environ.get("AL_DASHBOARD_SHOTS")
            if shots:
                Path(shots).mkdir(parents=True, exist_ok=True)
                page.evaluate("document.querySelector('.decision-block').scrollIntoView({block: 'center'})")  # live page: re-query
                page.screenshot(path=str(Path(shots) / "decision-block.png"), full_page=False)
            assert not errors, errors
            browser.close()
    finally:
        stop_server(running)
