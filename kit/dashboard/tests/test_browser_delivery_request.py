"""The delivery request in a real Chrome: a delivered run's page shows its request (a pull request on a
forge: the link and the `open` badge) in the Gate and ship block, and the layer page's promotions show
the stage pair's file request (agents/requests/sprint-main.md). Skipped without Playwright or Chrome
(nothing is downloaded). With AL_DASHBOARD_SHOTS=<directory> it saves delivery-request-run.png and
delivery-request-stages.png there. Everything on the page is synthetic: the mock agent, a temporary
application, invented names."""
from __future__ import annotations

import json

import pytest

from conftest import start_server, stop_server
from promotion_fixtures import SPRINT, build_promotion_site
from test_browser import CHROME, browser, open_, until, playwright_api  # noqa: F401 — fixtures and skips
from test_browser_promotion import shot

pytestmark = pytest.mark.browser

URL = "https://forge.example.org/acme/webshop/pulls/3"


@pytest.fixture()
def rpage(browser, tmp_path):  # noqa: F811
    site = build_promotion_site(tmp_path / "site", open_promotion=False)
    layer = site["layer"]
    # the first run's cycle opened a pull request on a forge; the stage pair's record is the file request
    path = layer / "agents" / "0000c001" / "state.json"
    state = json.loads(path.read_text())
    state["delivery_request"] = {"kind": "promotion", "from": "sprint", "to": "main", "source": SPRINT,
                                 "target": "main", "medium": "forge", "number": 3, "url": URL, "state": "open",
                                 "action": "opened", "updated_at": "2026-11-16T10:00:00+00:00"}
    path.write_text(json.dumps(state, indent=2))
    record = json.loads((layer / "agents" / "requests" / "sprint-main.json").read_text())
    assert record["path"] == "agents/requests/sprint-main.md"
    running = start_server(["--layer", str(layer)])
    context = browser.new_context(viewport={"width": 1280, "height": 1000})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(f"{msg.text} {(msg.location or {}).get('url', '')}")
            if msg.type == "error" else None)
    page.base = running.base  # type: ignore[attr-defined]
    try:
        yield page, site
    finally:
        context.close()
        stop_server(running)
    assert not [e for e in errors if "favicon" not in e], errors


def test_run_page_and_promotions_show_the_delivery_request(rpage):
    page, _site = rpage
    open_(page, "/run/shop/0000c001")
    page.wait_for_selector("[data-role=delivery-request]")
    line = page.locator("[data-role=delivery-request]").first
    link = line.locator(f"a[href='{URL}']")
    assert link.count() == 1 and link.get_attribute("target") == "_blank" and "noopener" in link.get_attribute("rel")
    assert line.locator(".badge.req-open").text_content() == "open"
    assert f"{SPRINT} → main" in line.inner_text()
    shot(page, "delivery-request-run.png")
    open_(page, "/layer/shop")
    page.wait_for_selector(".promotions-section .promotion-row [data-role=delivery-request]")
    row = page.locator(".promotions-section .promotion-row").first
    assert "agents/requests/sprint-main.md" in row.locator("[data-role=delivery-request] code").text_content()
    href = row.locator("[data-role=delivery-request] a").get_attribute("href")
    assert href == "/files/shop/agents/requests/sprint-main.md"
    assert row.locator(".badge.req-open").count() == 1
    shot(page, "delivery-request-stages.png")
