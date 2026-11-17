"""Real-browser tests of layer generation from the page: the placement picker of create and adopt in the
Repos view's al panel (beside or inside the repository, with the resulting path and its checks) and its
line in the confirmation dialog; and guided generation (`#/generate`), four steps (repository, placement,
options, review) that end in the same POST /api/action preview and confirmation as the buttons.

    uv run --with pytest --with playwright pytest kit/dashboard/tests/test_browser_generate.py

Skipped without Playwright or Chrome (nothing is downloaded). With AL_DASHBOARD_SHOTS=<directory> the
tests save generate-placement-panel.png, generate-wizard-repo.png, generate-wizard-placement.png,
generate-wizard-options.png, generate-wizard-review.png and generate-wizard-job.png there."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

import dashboard as d
import workspace_fixtures as wf
from conftest import start_server, stop_server

pytest.importorskip("playwright.sync_api")
from test_browser import browser, open_, until  # noqa: E402,F401 — fixtures; skips without Chrome
from test_browser_workspace import no_personal_paths, serve, shot, tab, workspace  # noqa: E402,F401 — fixtures

pytestmark = pytest.mark.browser

STEP = '.wizard [data-step="{}"]'
NEXT = '.wizard [data-role="next"]'
BACK = '.wizard [data-role="back"]'
START = '.wizard [data-role="start"]'


def posts(page) -> list[tuple[str, dict]]:
    """Record every POST the page sends from now on: (path, body)."""
    sent: list[tuple[str, dict]] = []

    def record(request) -> None:
        if request.method == "POST":
            sent.append((request.url.split("://", 1)[1].split("/", 1)[1], json.loads(request.post_data or "{}")))
    page.on("request", record)
    return sent


def step(page, name: str):
    page.wait_for_selector(STEP.format(name))
    assert page.locator(".wizard-steps [aria-current='step']").get_attribute("data-crumb") == name
    return page.locator(STEP.format(name))


def choose_repo(page, key: str) -> None:
    page.locator(f'.wizard-repo[data-repo="{key}"] input[type="radio"]').check()


def choose_placement(page, name: str) -> None:
    page.locator(f'{STEP.format("placement")} .placement[data-placement="{name}"] input[type="radio"]').check()


def job_record(tmp_path: Path, page) -> dict:
    job_id = page.evaluate("location.hash.split('/')[2].split('?')[0]")
    return json.loads((tmp_path / "jobs" / job_id / "job.json").read_text())


def test_al_panel_offers_the_placement_and_shows_its_path(tmp_path, workspace, tab):
    al = wf.fake_al(tmp_path / "bin" / "al")
    running = serve(tmp_path, workspace, al)
    try:
        page = tab(running)
        sent = posts(page)
        open_(page, "/repo/fern")
        row = page.locator('.al-panel [data-action="create"]')
        row.locator('[data-role="placement"]').wait_for()
        radios = row.locator('[data-role="placement"] input[type="radio"]')
        assert radios.count() == 2
        assert row.locator('.placement[data-placement="beside"] input').is_checked()
        assert row.locator("code.cmd").first.inner_text() == "al init ws/fern"
        assert row.locator('.placement[data-placement="beside"] .placement-path').inner_text() == "ws/fern.al"
        row.locator('.placement[data-placement="inside"] input').check()
        inside = row.locator('.placement[data-placement="inside"]')
        assert inside.locator(".placement-path").inner_text() == "ws/fern/agentic"
        assert inside.locator(".checks .check").all_inner_texts() == ["will be created", "writable", "not a layer yet"]
        assert "committed with the code" in inside.inner_text()
        assert row.locator("code.cmd").first.inner_text() == "al init ws/fern --in-repo"
        assert "al scout ws/fern --into ws/fern/agentic" in row.locator(".commands").inner_text()
        assert page.locator('.al-panel [data-action="scout"] [data-role="placement"]').count() == 0
        row.scroll_into_view_if_needed()
        shot(page, "generate-placement-panel.png")
        no_personal_paths(page)
        row.locator(".action-head button").click()
        placement = page.locator('.action-confirm [data-role="placement"]')
        placement.wait_for()
        assert "inside the repository" in placement.inner_text() and "ws/fern/agentic" in placement.inner_text()
        assert page.locator(".action-confirm .commands code.cmd").first.inner_text() == "al init ws/fern --in-repo"
        assert page.locator(".action-confirm .writes li").all_inner_texts() == ["ws/fern/agentic"]
        page.wait_for_selector(".action-confirm .choices .choice .choice-help")     # the existing dialog explains each choice too
        no_personal_paths(page)
        page.locator('.action-confirm [data-role="confirm"]').click()
        until(page, "location.hash.startsWith('#/job/')")
        record = job_record(tmp_path, page)
        assert record["placement"] == "inside" and record["steps"][0]["argv"][-1] == "--in-repo"
        assert [(path, body.get("placement")) for path, body in sent] == [("api/action", "inside"), ("api/action", "inside")]
        page.wait_for_selector('.run-head [data-role="placement"]')
        assert page.locator('.run-head [data-role="placement"]').inner_text() == "placement: inside the repository"
        no_personal_paths(page)
    finally:
        stop_server(running)


def test_guided_generation_walks_four_steps_and_starts_the_job(tmp_path, workspace, tab):
    al = wf.fake_al(tmp_path / "bin" / "al", choices=wf.FORGEJO_CHOICES, hold={"create": 1.0},
                    writes={"author": {"{arg2}/agentic.rkt": "#lang al\n"}})
    running = serve(tmp_path, workspace, al)
    fern = os.path.realpath(workspace / "fern")
    try:
        page = tab(running)
        sent = posts(page)
        open_(page, "/repos")
        page.locator('.repo-tools [data-role="guided"]').click()
        until(page, "location.hash === '#/generate'")
        assert page.locator(".views a.active").inner_text().endswith("Repos")

        panel = step(page, "repo")
        page.wait_for_selector('.wizard-repo[data-repo="fern"]')
        assert not panel.locator('.wizard-repo[data-repo="fern"] input').is_disabled()
        assert not panel.locator('.wizard-repo[data-repo="quill"] input').is_disabled()
        assert panel.locator('.wizard-repo[data-repo="pantry"] input').is_disabled()
        assert "already has a layer" in panel.locator('.wizard-repo[data-repo="pantry"]').inner_text()
        assert page.locator(NEXT).is_disabled()
        choose_repo(page, "fern")
        until(page, "location.hash === '#/generate/fern'")
        assert panel.locator('[data-role="adopt"]').count() == 0       # nothing to adopt in fern
        shot(page, "generate-wizard-repo.png")
        no_personal_paths(page)
        page.locator(NEXT).click()

        panel = step(page, "placement")
        panel.locator('.placement[data-placement="inside"]').wait_for()
        assert panel.locator(".placement .placement-path").all_inner_texts() == ["ws/fern.al", "ws/fern/agentic"]
        for name in ("beside", "inside"):
            assert panel.locator(f'.placement[data-placement="{name}"] .checks .check').all_inner_texts() == [
                "will be created", "writable", "not a layer yet"]
        assert panel.locator('.placement[data-placement="beside"] input').is_checked()
        shot(page, "generate-wizard-placement.png")
        no_personal_paths(page)
        choose_placement(page, "inside")
        page.locator(NEXT).click()

        panel = step(page, "options")
        assert "detecting choices" in panel.locator('[data-role="preview-status"]').inner_text()
        assert page.locator(NEXT).is_disabled()
        page.wait_for_selector(f'{STEP.format("options")} .choices select[data-choice="forge"]')
        assert panel.locator('select[data-choice="forge"]').input_value() == "forgejo"
        assert panel.locator(".choice").count() == len(d.CHOICE_RULES)
        helps = panel.locator(".choice .choice-help").all_inner_texts()
        assert len(helps) == len(d.CHOICE_RULES) and all(text.strip() for text in helps)
        assert "who decides a change is done" in panel.locator('.choice[data-key="review"] .choice-help').inner_text()
        assert ".forgejo/workflows" in panel.locator('.choice[data-key="forge"] .fact').inner_text()
        page.select_option(f'{STEP.format("options")} select[data-choice="review"]', "agent")
        panel.locator('.choice[data-key="review"]').scroll_into_view_if_needed()
        shot(page, "generate-wizard-options.png")
        no_personal_paths(page)
        page.locator(NEXT).click()

        panel = step(page, "review")
        summary = panel.locator(".wizard-summary")
        assert summary.locator('[data-row="repository"]').inner_text().endswith("fern")
        assert "create" in summary.locator('[data-row="action"]').inner_text()
        placement = summary.locator('[data-row="placement"]').inner_text()
        assert "inside the repository" in placement and "ws/fern/agentic" in placement
        review = summary.locator('[data-row="choice-review"]')
        assert "agent" in review.inner_text() and review.locator(".changed-mark").is_visible()
        assert summary.locator('[data-row="choice-forge"] .changed-mark').count() == 0
        assert "forgejo" in summary.locator('[data-row="choice-forge"]').inner_text()
        commands = panel.locator(".commands code.cmd").all_inner_texts()
        assert commands[0] == "al init ws/fern --in-repo" and len(commands) == 7
        assert panel.locator(".writes li").all_inner_texts() == ["ws/fern/agentic"]
        shot(page, "generate-wizard-review.png")
        no_personal_paths(page)
        assert wf.calls(al) == [["create", fern, "--choices", "--json"]]      # nothing but the read-only preview so far
        assert not (workspace / "fern" / "agentic").exists()

        page.locator(START).click()
        until(page, "location.hash.startsWith('#/job/')")
        page.wait_for_selector('.run-head [data-role="placement"]')
        assert page.locator('.run-head [data-role="placement"]').inner_text() == "placement: inside the repository"
        until(page, "document.querySelector('.run-head .pill') && document.querySelector('.run-head .pill').textContent === 'passed'", 30000)
        page.wait_for_selector('.job-changed [data-role="repo-changes"]')
        changes = page.locator('.job-changed [data-role="repo-changes"]')
        assert "the layer was written inside the repository: 1 path(s); commit agentic/ with the code" in changes.inner_text()
        assert "faint" in changes.get_attribute("class")
        shot(page, "generate-wizard-job.png")
        no_personal_paths(page)
        record = job_record(tmp_path, page)
        assert record["placement"] == "inside" and record["choices"] == {"review": "agent"} and record["action"] == "create"
        assert [path for path, _body in sent] == ["api/action", "api/action"]
        assert sent[0][1] == {"repo": "fern", "action": "create", "placement": "inside"}
        assert sent[1][1] == {"repo": "fern", "action": "create", "placement": "inside", "confirm": sent[1][1]["confirm"],
                              "choices": {"review": "agent"}}
    finally:
        stop_server(running)


def test_guided_generation_back_keeps_the_answers_and_a_new_placement_asks_again(tmp_path, workspace, tab):
    al = wf.fake_al(tmp_path / "bin" / "al")
    running = serve(tmp_path, workspace, al)
    try:
        page = tab(running)
        sent = posts(page)
        open_(page, "/generate/fern")                       # the address preselects the repository
        step(page, "repo")
        page.wait_for_selector('.wizard-repo[data-repo="fern"] input:checked')
        page.locator(NEXT).click()
        step(page, "placement").locator('.placement[data-placement="inside"]').wait_for()
        choose_placement(page, "inside")
        page.locator(NEXT).click()
        step(page, "options")
        page.wait_for_selector(f'{STEP.format("options")} select[data-choice="review"]')
        page.select_option(f'{STEP.format("options")} select[data-choice="review"]', "agent")
        assert len(sent) == 1
        page.locator(BACK).click()
        panel = step(page, "placement")
        panel.locator('.placement[data-placement="inside"] input:checked').wait_for()
        page.locator(BACK).click()
        step(page, "repo")
        assert page.locator('.wizard-repo[data-repo="fern"] input').is_checked()
        page.locator(NEXT).click()
        step(page, "placement").locator('.placement[data-placement="inside"] input:checked').wait_for()
        page.locator(NEXT).click()                           # the same three answers: the preview and the edited form are kept
        step(page, "options")
        page.wait_for_selector(f'{STEP.format("options")} select[data-choice="review"]')
        assert page.locator(f'{STEP.format("options")} select[data-choice="review"]').input_value() == "agent"
        assert len(sent) == 1
        page.locator(BACK).click()
        step(page, "placement").locator('.placement[data-placement="beside"]').wait_for()
        choose_placement(page, "beside")
        page.locator(NEXT).click()                           # another placement: detected again
        step(page, "options")
        page.wait_for_selector(f'{STEP.format("options")} select[data-choice="review"]')
        assert [body.get("placement") for _path, body in sent] == ["inside", "beside"]
        assert page.locator(f'{STEP.format("options")} select[data-choice="review"]').input_value() == "human"
        page.locator(NEXT).click()
        panel = step(page, "review")
        placement = panel.locator('.wizard-summary [data-row="placement"]').inner_text()
        assert "beside the repository" in placement and "ws/fern.al" in placement
        assert panel.locator(".commands code.cmd").first.inner_text() == "al init ws/fern"
        assert panel.locator(".wizard-summary .changed-mark").count() == 0
        no_personal_paths(page)
        assert all(c[0] == "create" and "--choices" in c for c in wf.calls(al))
    finally:
        stop_server(running)


def test_guided_generation_disables_an_unusable_placement(tmp_path, workspace, tab):
    (workspace / "fern.al").mkdir()
    (workspace / "fern.al" / "agentic-layer.json").write_text("{}\n")
    al = wf.fake_al(tmp_path / "bin" / "al")
    running = serve(tmp_path, workspace, al)
    try:
        page = tab(running)
        open_(page, "/generate/fern")
        page.wait_for_selector('.wizard-repo[data-repo="fern"] input:checked')
        page.locator(NEXT).click()
        panel = step(page, "placement")
        beside = panel.locator('.placement[data-placement="beside"]')
        beside.wait_for()
        assert beside.locator("input").is_disabled()
        assert "ws/fern.al is already a layer" in beside.locator(".why").inner_text()
        assert beside.locator(".checks .check").all_inner_texts() == ["exists: files there are kept", "writable", "already a layer"]
        assert panel.locator('.placement[data-placement="inside"] input').is_checked()
        assert not page.locator(NEXT).is_disabled()
        no_personal_paths(page)
        (workspace / "fern" / "agentic").mkdir()
        (workspace / "fern" / "agentic" / "notes.txt").write_text("the team's own notes\n")
        wf.commit(workspace / "fern", "fern: notes in agentic/")
        page.reload()
        page.wait_for_selector("#conn.conn-live", timeout=10000)
        page.wait_for_selector('.wizard-repo[data-repo="fern"] input:checked')
        page.locator(NEXT).click()
        panel = step(page, "placement")
        inside = panel.locator('.placement[data-placement="inside"]')
        inside.wait_for()
        assert inside.locator("input").is_disabled() and panel.locator('.placement[data-placement="beside"] input').is_disabled()
        assert "already has tracked files" in inside.locator(".why").inner_text()
        assert panel.locator(".placement input:checked").count() == 0
        assert page.locator(NEXT).is_disabled()
        assert wf.calls(al) == []
    finally:
        stop_server(running)


def test_guided_generation_adopts_an_existing_setup_by_default(tmp_path, workspace, tab):
    from test_browser_workspace import DIFF
    al = wf.fake_al(tmp_path / "bin" / "al", out=DIFF)
    running = serve(tmp_path, workspace, al)
    try:
        page = tab(running)
        sent = posts(page)
        open_(page, "/generate")
        panel = step(page, "repo")
        page.wait_for_selector('.wizard-repo[data-repo="comet"]')
        choose_repo(page, "comet")
        adopt = panel.locator('[data-role="adopt"]')
        adopt.wait_for()
        assert adopt.is_checked()
        assert "adopt the existing agent setup (rules, prompts, workflows)" in panel.inner_text()
        page.locator(NEXT).click()
        step(page, "placement").locator('.placement[data-placement="beside"] input:checked').wait_for()
        page.locator(NEXT).click()
        panel = step(page, "options")
        page.wait_for_selector(f'{STEP.format("options")} .diff .diff-add')
        assert "+# What al adopt found" in panel.locator(".diff .diff-add").all_inner_texts()
        assert page.evaluate("window.injected") is None and panel.locator(".diff script").count() == 0
        page.wait_for_selector(f'{STEP.format("options")} .choices .choice')
        page.locator(NEXT).click()
        panel = step(page, "review")
        assert "adopt" in panel.locator('.wizard-summary [data-row="action"]').inner_text()
        no_personal_paths(page)
        page.locator(START).click()
        until(page, "location.hash.startsWith('#/job/')")
        record = job_record(tmp_path, page)
        assert record["action"] == "adopt" and record["placement"] == "beside" and record["preview"] is False
        assert [body["action"] for _path, body in sent] == ["adopt", "adopt"]
        # unchecked, the same repository gets a plain create
        open_(page, "/generate/comet")
        panel = step(page, "repo")
        panel.locator('[data-role="adopt"]').wait_for()
        panel.locator('[data-role="adopt"]').uncheck()
        page.locator(NEXT).click()
        step(page, "placement").locator('.placement[data-placement="beside"]').wait_for()
        assert "al init ws/comet" in page.locator(STEP.format("placement")).inner_text()
    finally:
        stop_server(running)


def test_guided_generation_says_why_it_is_off(tmp_path, workspace, tab, monkeypatch):
    al = wf.fake_al(tmp_path / "bin" / "al")
    running = start_server(["--workspace", str(workspace), "--jobs-dir", str(tmp_path / "jobs"), "--al", str(al), "--no-actions"])
    try:
        page = tab(running)
        open_(page, "/generate")
        page.wait_for_selector(f'{STEP.format("repo")} .why')
        assert "actions are off (--no-actions)" in page.locator(f'{STEP.format("repo")} .why').inner_text()
        assert page.locator(f"{NEXT}:not([disabled])").count() == 0
        assert page.locator(START).count() == 0
    finally:
        stop_server(running)
    monkeypatch.setattr(d, "KIT_AL", tmp_path / "nowhere" / "tools" / "al")
    bin_dir = tmp_path / "only-git"
    bin_dir.mkdir()
    os.symlink(shutil.which("git"), bin_dir / "git")
    monkeypatch.setenv("PATH", str(bin_dir))
    running = serve(tmp_path, workspace, "/nonexistent")
    try:
        page = tab(running)
        open_(page, "/generate/fern")
        page.wait_for_selector(f'{STEP.format("repo")} .why')
        assert page.locator(f'{STEP.format("repo")} .why').inner_text().startswith("needs the al tool; looked for: /nonexistent")
        assert page.locator(f"{NEXT}:not([disabled])").count() == 0
    finally:
        stop_server(running)
