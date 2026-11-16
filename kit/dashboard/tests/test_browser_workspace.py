"""Real-browser tests of the Repos view: the workspace's repositories with their state, the selection kept
in the URL, a filter, the adopt preview's diff (text only) with its confirmation, and a job whose output
streams and whose status ends `passed`; without the al tool every action is disabled and says where it
looked.

    uv run --with pytest --with playwright pytest kit/dashboard/tests/test_browser_workspace.py

Skipped without Playwright or Chrome (nothing is downloaded). With AL_DASHBOARD_SHOTS=<directory> the
tests save workspace-repos.png, workspace-adopt-preview.png and workspace-job.png there."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

import dashboard as d
import workspace_fixtures as wf
from conftest import start_server, stop_server

pytest.importorskip("playwright.sync_api")
from test_browser import browser, open_, until  # noqa: E402,F401 — fixtures; skips without Chrome

pytestmark = pytest.mark.browser

DIFF = "\n".join(["al adopt: 4 piece(s) of existing agent material found (dry run: nothing is written)",
                  "  would write: adopted/REPORT.md", "--- /dev/null", "+++ b/adopted/REPORT.md", "@@ -0,0 +1,2 @@",
                  "+# What al adopt found", "+<script>window.injected = 1</script>"])


def shot(page, name: str) -> None:
    directory = os.environ.get("AL_DASHBOARD_SHOTS")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(directory) / name), full_page=False)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    wf.build(root)
    return root


def serve(tmp_path: Path, root: Path, al: Path | str):
    return start_server(["--workspace", str(root), "--jobs-dir", str(tmp_path / "jobs"), "--al", str(al)])


@pytest.fixture()
def tab(browser):
    pages = []

    def make(running):
        context = browser.new_context(viewport={"width": 1280, "height": 800})
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


def no_personal_paths(page) -> None:
    text = page.evaluate("document.body.innerText")
    home = str(Path.home())
    assert home not in text and os.environ.get("USER", "\0") not in text.split()


def test_repos_view_lists_states_and_keeps_the_selection_in_the_url(tmp_path, workspace, tab):
    al = wf.fake_al(tmp_path / "bin" / "al")
    running = serve(tmp_path, workspace, al)
    try:
        page = tab(running)
        open_(page, "/repos")
        page.wait_for_selector(".repo-row")
        badges = {row.get_attribute("data-repo"): row.locator(".badge").first.inner_text()
                  for row in page.locator(".repo-row").all()}
        assert badges == {"comet": "setup", "fern": "none", "pantry": "layer", "quill": "setup"}
        assert page.locator(".views a.active").inner_text().endswith("Repos")
        page.locator('.repo-row[data-repo="comet"]').click()
        until(page, "location.hash === '#/repo/comet'")
        page.wait_for_selector('.al-panel [data-action="adopt"] code.cmd')
        assert "al scout ws/comet" in page.locator('.al-panel [data-action="scout"] code.cmd').inner_text()
        assert page.locator('.al-panel [data-action="gen"] button').is_disabled()
        shot(page, "workspace-repos.png")
        no_personal_paths(page)
        page.reload()
        page.wait_for_selector('.repo-row.selected[data-repo="comet"]')
        page.wait_for_selector('.al-panel [data-action="adopt"]')
        page.fill(".repo-filter", "fe")
        assert [row.get_attribute("data-repo") for row in page.locator(".repo-row").all()] == ["fern"]
    finally:
        stop_server(running)


def test_adopt_preview_diff_and_confirm(tmp_path, workspace, tab):
    al = wf.fake_al(tmp_path / "bin" / "al", out=DIFF, lines=["step output 1", "step output 2"], line_sleep=0.3)
    running = serve(tmp_path, workspace, al)
    try:
        page = tab(running)
        open_(page, "/repo/comet")
        page.locator('.al-panel [data-action="adopt"] button').click()
        page.wait_for_selector(".action-confirm .diff .diff-add")
        lines = page.locator(".action-confirm .diff .diff-add").all_inner_texts()
        assert "+<script>window.injected = 1</script>" in lines
        assert page.evaluate("window.injected") is None
        assert page.locator(".action-confirm .diff script").count() == 0
        assert page.locator(".action-confirm .diff .diff-hunk").count() == 1
        assert "al adopt ws/comet.al --app ws/comet --dry-run --diff" not in page.locator(".action-confirm .commands").inner_text()
        assert "al init ws/comet" in page.locator(".action-confirm .commands").inner_text()
        shot(page, "workspace-adopt-preview.png")
        no_personal_paths(page)
        page.locator('.action-confirm [data-role="confirm"]').click()
        until(page, "location.hash.startsWith('#/job/')")
        page.wait_for_selector(".job-step")
        until(page, "document.querySelector('.log') && document.querySelector('.log').textContent.includes('step output 2')", 20000)
        until(page, "document.querySelector('.run-head .pill') && document.querySelector('.run-head .pill').textContent === 'passed'", 30000)
        steps = page.locator(".job-step code.cmd").all_inner_texts()
        assert steps[0] == "al init ws/comet" and steps[2] == "al adopt ws/comet.al"
        shot(page, "workspace-job.png")
        no_personal_paths(page)
    finally:
        stop_server(running)


def test_actions_disabled_without_al_say_where_it_looked(tmp_path, workspace, tab, monkeypatch):
    monkeypatch.setattr(d, "KIT_AL", tmp_path / "nowhere" / "tools" / "al")
    bin_dir = tmp_path / "only-git"
    bin_dir.mkdir()
    os.symlink(shutil.which("git"), bin_dir / "git")
    monkeypatch.setenv("PATH", str(bin_dir))
    running = serve(tmp_path, workspace, "/nonexistent")
    try:
        page = tab(running)
        open_(page, "/repo/fern")
        page.wait_for_selector('.al-panel [data-action="scout"]')
        buttons = page.locator(".al-panel .action button")
        assert buttons.count() == len(d.ACTIONS)
        assert all(buttons.nth(i).is_disabled() for i in range(buttons.count()))
        why = page.locator('.al-panel [data-action="scout"] .why').inner_text()
        assert why.startswith("needs the al tool; looked for: /nonexistent")
        assert "The al tool was not found" in page.locator(".al-panel").inner_text()
    finally:
        stop_server(running)
