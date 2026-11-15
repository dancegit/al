"""Real-browser tests of the run page's Documents tab: list, rendered and raw view, download, an inert
<script> in a plan, a document that appears live, and the step panel's link to its document.

    uv run --with pytest --with playwright pytest kit/dashboard/tests/test_browser_documents.py

Skipped without Playwright or Chrome (nothing is downloaded). With AL_DASHBOARD_SHOTS=<directory>
the tests save run-documents.png and run-documents-step-link.png there."""
from __future__ import annotations

import json
import os
import urllib.parse
from pathlib import Path

import pytest

import fixtures

pytest.importorskip("playwright.sync_api")
from test_browser import browser, open_, page, site, until  # noqa: E402,F401 — fixtures; skips without Chrome

pytestmark = pytest.mark.browser

PLAN = """# Feature: show documents

Plans, **PR text** and review reports, readable in the page.

## Steps

- list the documents
- render Markdown as *text* nodes
- link `specs/` files

1. one
2. two

> a quote

```python
print("<b>not bold</b>")
```

| a | b |
|---|---|
"""


def shot(page, name: str) -> None:
    directory = os.environ.get("AL_DASHBOARD_SHOTS")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(directory) / name), full_page=False)


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


def running_run(agents: Path, run_id: str, plan: str = "", plan_passed: bool = True) -> Path:
    """A run in its build step (plan-feature passed); its plan is recorded when `plan` is given."""
    steps = [{"name": "classify", "kind": "run", "status": "passed", "started": "2026-11-01T12:00:00+00:00",
              "ended": "2026-11-01T12:00:10+00:00"}]
    if plan_passed:
        steps += [{"name": "plan-feature", "kind": "run", "status": "passed", "started": "2026-11-01T12:00:10+00:00",
                   "ended": "2026-11-01T12:00:40+00:00", "detail": plan},
                  {"name": "build", "kind": "run", "status": "running", "started": "2026-11-01T12:00:40+00:00", "ended": ""}]
    state = {"adw_id": run_id, "adw": "zte", "prompt": "Show run documents", "status": "running", "klass": "feature",
             "started": "2026-11-01T12:00:00+00:00", "steps": steps, "plan": plan,
             "outputs": {"plan-feature": plan} if plan else {}}
    path = agents / run_id / "state.json"
    write_state(path, state)
    return path


def test_documents_tab_lists_and_views_the_plan(page, site):
    _running, agents = site
    layer = agents.parent
    (layer / "specs" / "chore-a1000002.md").write_text(PLAN)
    open_(page, "/run/demo-shop/a1000002/documents")
    rows = page.locator("table.docs tbody tr")
    rows.first.wait_for()
    plan_row = page.locator("table.docs tr[data-path='specs/chore-a1000002.md']")
    pr_row = page.locator("table.docs tr[data-path='agents/a1000002/PR.md']")
    assert plan_row.count() == 1 and pr_row.count() == 1
    for row in (plan_row, pr_row):
        cells = row.locator("td")
        assert cells.nth(1).inner_text().strip() and cells.nth(2).inner_text().endswith("B")
        assert cells.nth(3).inner_text().startswith("20")
    assert "Documents (" in page.locator(".tabs a.active").inner_text()
    plan_row.locator("button[data-role=view]").click()
    page.locator(".doc-view .md h1").wait_for()
    assert page.locator(".doc-view .md h1").inner_text() == "Feature: show documents"
    assert page.locator(".doc-view .md h2").inner_text() == "Steps"
    assert page.locator(".doc-view .md ul li").count() == 3 and page.locator(".doc-view .md ol li").count() == 2
    assert page.locator(".doc-view .md strong").inner_text() == "PR text"
    assert page.locator(".doc-view .md pre code").inner_text() == 'print("<b>not bold</b>")'
    assert page.locator(".doc-view .md b").count() == 0
    shot(page, "run-documents.png")
    page.locator("[data-role=raw-toggle]").click()
    page.locator(".doc-view pre.doc-raw").wait_for()
    assert page.locator(".doc-view pre.doc-raw").text_content() == PLAN
    assert "raw=1" in page.url
    page.locator("[data-role=raw-toggle]").click()
    page.locator(".doc-view .md h1").wait_for()
    download = pr_row.locator("a[data-role=download]")
    href = download.get_attribute("href")
    assert href.startswith("/api/doc/download?") and "path=agents%2Fa1000002%2FPR.md" in href
    with page.expect_download() as info:
        download.click()
    assert info.value.suggested_filename == "PR.md"
    assert page.locator("a", has_text="Download all (.zip)").get_attribute("href") == "/api/doc/zip?layer=demo-shop&run=a1000002"


def test_markdown_script_is_inert(page, site):
    _running, agents = site
    layer = agents.parent
    hostile = ("# Hostile plan\n\n<script>window.__pwned=1</script>\n\n<img src=x onerror=\"window.__pwned=1\">\n\n"
               "[x](javascript:window.__pwned=1) and [ok](https://example.invalid/doc)\n")
    (layer / "specs" / "feature-a2000010-x.md").write_text(hostile)
    running_run(agents, "a2000010", plan=f"{fixtures.LAYER_DIR}/specs/feature-a2000010-x.md")
    open_(page, "/run/demo-shop/a2000010/documents?path=" + urllib.parse.quote("specs/feature-a2000010-x.md", safe=""))
    page.locator(".doc-view .md h1").wait_for()
    assert page.evaluate("typeof window.__pwned") == "undefined"
    assert page.locator(".md script").count() == 0 and page.locator(".md img").count() == 0
    assert "<script>window.__pwned=1</script>" in page.locator(".doc-view .md").inner_text()
    links = page.locator(".md a")
    assert links.count() == 2
    assert links.nth(0).get_attribute("href") == "#"
    assert links.nth(1).get_attribute("href") == "https://example.invalid/doc"
    for index in range(links.count()):
        assert links.nth(index).get_attribute("rel") == "noopener noreferrer"


def test_document_appears_live_without_reload(page, site):
    _running, agents = site
    layer = agents.parent
    state_path = running_run(agents, "a2000011")
    open_(page, "/run/demo-shop/a2000011/documents")
    page.locator(".empty", has_text="This run has produced no documents yet.").wait_for()
    page.evaluate("window.__marker = 'still here'")
    (layer / "specs" / "feature-a2000011-live.md").write_text("# Live plan\n")
    state = json.loads(state_path.read_text())
    plan = f"{fixtures.LAYER_DIR}/specs/feature-a2000011-live.md"
    write_state(state_path, {**state, "plan": plan, "outputs": {"plan-feature": plan}})
    until(page, "!!document.querySelector(\"table.docs tr[data-path='specs/feature-a2000011-live.md']\")", timeout=10000)
    assert page.evaluate("window.__marker") == "still here"
    assert "Documents (1)" in page.locator(".tabs a.active").inner_text()


def test_step_panel_links_its_document(page, site):
    _running, agents = site
    layer = agents.parent
    (layer / "specs" / "feature-a2000012-x.md").write_text(PLAN)
    running_run(agents, "a2000012", plan=f"{fixtures.LAYER_DIR}/specs/feature-a2000012-x.md")
    open_(page, "/run/demo-shop/a2000012/step/plan-feature")
    link = page.locator(".step-head a", has_text="document: feature-a2000012-x.md")
    link.wait_for()
    assert "/documents?path=specs%2Ffeature-a2000012-x.md" in link.get_attribute("href")
    shot(page, "run-documents-step-link.png")
    link.click()
    page.locator(".doc-view .md h1").wait_for()
    assert page.locator(".doc-view .md h1").inner_text() == "Feature: show documents"
    assert "/documents?path=" in page.url
