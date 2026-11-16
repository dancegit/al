"""The promotion card in a real Chrome: a promotion run waiting for people lists its three changes
with their criteria and proof; refusing one with a comment shows it as refused; the pack page that
build_pack generated opens with its pictures and a playable recording. Skipped without Playwright or Chrome (nothing is
downloaded). With AL_DASHBOARD_SHOTS=<directory> it saves promotion-card.png, promotion-refused.png
and promotion-pack.png there. Everything on the page is synthetic: the mock agent, a temporary
application, invented names."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import start_server, stop_server
from promotion_fixtures import build_promotion_site
from test_browser import CHROME, browser, open_, until, playwright_api  # noqa: F401 — fixtures and skips

pytestmark = pytest.mark.browser


def shot(page, name: str) -> None:
    directory = os.environ.get("AL_DASHBOARD_SHOTS")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(directory) / name), full_page=True)


@pytest.fixture()
def ppage(browser, tmp_path):  # noqa: F811
    site = build_promotion_site(tmp_path / "site")
    running = start_server(["--layer", str(site["layer"])])
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


def test_promotion_card_accept_refuse_and_continue(ppage):
    page, site = ppage
    open_(page, f"/run/shop/{site['run']}")
    page.wait_for_selector(".promotion-card")
    card = page.locator(".promotion-card")
    assert card.locator(".promotion-change").count() == 3
    assert "Promote sprint/2026-s47 → main" in card.locator("h2").text_content()
    assert "The cart holds items" in card.inner_text() and "cart.png" in card.inner_text()
    shot(page, "promotion-card.png")
    page.fill(".promotion-by", "Ada Example")
    page.fill(".promotion-comment", "not this sprint")
    page.check(".promotion-change[data-change='0000c002'] input[type=checkbox]")
    page.click("button:has-text('Refuse · revert')")
    until(page, "document.querySelector(\".promotion-change[data-change='0000c002'] .badge.warn\") !== null")
    refused = page.locator(".promotion-change[data-change='0000c002']")
    assert "refused by Ada Example" in refused.inner_text() and "not this sprint" in refused.inner_text()
    lines = (site["layer"] / "promotions" / site["run"] / "acceptance.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["comment"] == "not this sprint"
    shot(page, "promotion-refused.png")


def test_pack_page_shows_pictures_and_plays_the_recording(ppage):
    page, site = ppage
    # the pack exactly as build_pack wrote it: 0000c002 carries a recording with one keyframe
    page.goto(f"{page.base}/files/shop/artefacts/{site['run']}/pack/index.html")
    page.wait_for_selector("video")
    video = "video[controls][src='media/0000c002/search.webm']"
    assert page.locator(f"#change-0000c002 {video}").count() == 1
    proof = page.locator("figure", has=page.locator(video))
    assert proof.count() == 1 and proof.locator("figcaption").inner_text() == "search.webm"
    assert page.locator("h3:has-text('Add search (0000c002)') + figure").locator(video).count() == 1
    assert page.locator(".strip video[src='media/0000c002/search.webm']").count() == 1
    assert page.locator(".strip img[src='media/0000c002/frame-001.png']").count() == 1
    assert page.locator("img[src='media/0000c001/cart.png']").count() >= 1
    assert "Add a cart" in page.inner_text("body")
    shot(page, "promotion-pack.png")
