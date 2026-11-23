"""The voice helper's dock in a real Chromium: the microphone button in the top bar (every page, key `v`)
opens the voice panel in a dock that stays open across pages, with one live panel at a time, a layer
picker when several layers declare voice, and the reason when voice is off. Same harness as
test_browser_voice.py (fake microphone, fake engines, the mock helper); skipped without Playwright or
Chrome.

With AL_DASHBOARD_SHOTS=<directory> it saves voice-dock-1280.png and voice-dock-390.png: the whole page
with the dock open."""
from __future__ import annotations

import pytest

import start_layer
import voice_fixtures
from test_browser import CHROME, open_, page, site  # noqa: F401 — fixtures of a layer without voice
from test_browser_voice import REQUEST, SPEECH, Voice, helper_rules, pw, shots_dir, until, voice  # noqa: F401 — fixtures

pytestmark = pytest.mark.browser

DOCK_PANEL = "#voice-dock .voice-panel[data-session='ready']"


@pytest.fixture()
def browser(pw):  # noqa: F811
    """The Chromium of test_browser.py's `page` fixture, launched from this module's one Playwright (`pw`):
    two Playwright instances cannot run in one module."""
    instance = pw.chromium.launch(executable_path=CHROME, headless=True)
    yield instance
    instance.close()


def open_dock(page) -> None:
    page.click("#voice-button")
    page.wait_for_selector("#voice-dock:not([hidden])", timeout=10000)


def page_shot(page, name: str) -> None:
    """The whole page (dock open) as DIR/name."""
    directory = shots_dir()
    if directory:
        page.screenshot(path=str(directory / name))


def dock_hidden(page) -> bool:
    return page.evaluate("document.getElementById('voice-dock').hidden")


def goto(page, route: str) -> None:
    """Another page of the dashboard by its hash; waits until the view has rendered something."""
    page.evaluate(f"location.hash = '#{route}'")
    until(page, f"location.hash === '#{route}' && document.getElementById('main').childElementCount > 0")


def test_header_button_opens_the_dock_from_any_page(voice):
    page = voice.open(1280, route="/now", wait_panel=False)
    button = page.locator("#voice-button")
    button.wait_for(state="visible", timeout=10000)
    assert button.get_attribute("aria-expanded") == "false"
    assert dock_hidden(page) and page.locator(".voice-panel").count() == 0
    open_dock(page)
    page.wait_for_selector(DOCK_PANEL, timeout=15000)
    assert button.get_attribute("aria-expanded") == "true"
    assert page.get_attribute("#voice-dock", "data-layer") == "talk"
    assert page.evaluate("document.activeElement === document.querySelector('#voice-dock .voice-talk')")
    for control in (".voice-talk", ".voice-modes", ".voice-keep", ".voice-text", ".voice-send"):
        assert page.locator(f"#voice-dock {control}").count() == 1, control
    voice.type_("what can you do")
    page.wait_for_selector(".voice-entry.voice-helper", timeout=30000)
    assert "about the runs" in page.locator(".voice-caption-helper").inner_text()
    page_shot(page, "voice-dock-1280.png")


def test_dock_fits_a_narrow_screen(voice):
    page = voice.open(390, route="/now", wait_panel=False)
    page.locator("#voice-button").wait_for(state="visible", timeout=10000)
    box = page.locator("#voice-button").bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 390, box
    assert page.evaluate("document.querySelector('.top').scrollWidth <= document.querySelector('.top').clientWidth + 1")
    before = page.evaluate("document.documentElement.scrollWidth")
    open_dock(page)
    page.wait_for_selector(DOCK_PANEL, timeout=15000)
    dock = page.locator("#voice-dock").bounding_box()
    assert dock["x"] >= 0 and dock["x"] + dock["width"] <= 391 and dock["y"] >= 0, dock
    # nothing in the dock is cut off or sticks out, and the dock does not widen the page (the Now page's
    # own content is measured before the dock opens: the dock is not answerable for it)
    assert page.evaluate("document.getElementById('voice-dock').scrollWidth <= document.getElementById('voice-dock').clientWidth + 1")
    assert page.evaluate("[...document.querySelectorAll('#voice-dock *')].every(e => e.getBoundingClientRect().right <= 391 && e.getBoundingClientRect().left >= 0)")
    assert page.evaluate("document.documentElement.scrollWidth") <= max(before, 391)
    page_shot(page, "voice-dock-390.png")


def test_dock_stays_open_across_pages_and_shares_the_conversation(voice):
    page = voice.open(1280, route="/now", wait_panel=False)
    page.locator("#voice-button").wait_for(state="visible", timeout=10000)
    open_dock(page)
    page.wait_for_selector(DOCK_PANEL, timeout=15000)
    voice.type_("what can you do")
    page.wait_for_selector(".voice-entry.voice-helper", timeout=30000)
    page.evaluate("window.__dockPanel = document.querySelector('.voice-panel')")
    for route in ("/runs", "/layers"):
        goto(page, route)
        assert page.evaluate("window.__dockPanel.isConnected") and not dock_hidden(page), route
    goto(page, "/layer/talk")
    page.wait_for_selector("#main .card", timeout=10000)
    assert page.evaluate("document.querySelectorAll('.voice-panel').length") == 1      # one live panel: the docked one
    assert page.evaluate("document.querySelector('.voice-panel') === window.__dockPanel && !!window.__dockPanel.closest('#voice-dock')")
    assert page.evaluate("document.querySelector('#main .voice-host').childElementCount") == 0
    page.click(".voice-dock-close")
    until(page, "document.getElementById('voice-dock').hidden")
    assert page.get_attribute("#voice-button", "aria-expanded") == "false"
    page.wait_for_selector("#main .voice-panel[data-session='ready']", timeout=15000)
    until(page, "document.querySelector('#main .voice-transcript').textContent.includes('what can you do')")   # same conversation
    page.click("#voice-button")
    page.wait_for_selector(DOCK_PANEL, timeout=15000)
    assert page.locator("#main .voice-panel").count() == 0


def test_dock_keyboard(voice):
    page = voice.open(1280, route="/now", wait_panel=False)
    page.locator("#voice-button").wait_for(state="visible", timeout=10000)
    page.keyboard.press("v")
    page.wait_for_selector("#voice-dock:not([hidden])", timeout=10000)
    page.wait_for_selector(DOCK_PANEL, timeout=15000)
    page.keyboard.press("Escape")
    assert dock_hidden(page)
    assert page.evaluate("location.hash") == "#/now"
    assert page.evaluate("document.activeElement.id") == "voice-button"
    page.keyboard.press("v")
    page.wait_for_selector(DOCK_PANEL, timeout=15000)
    page.focus(".voice-text")
    page.keyboard.type("v")
    assert page.input_value(".voice-text") == "v" and not dock_hidden(page)      # a letter typed in the field is text
    page.keyboard.press("Escape")
    assert not dock_hidden(page) and page.evaluate("document.activeElement.id") == "voice-dock"   # first: leave the field
    page.keyboard.press("Escape")
    assert dock_hidden(page)
    page.keyboard.press("?")
    assert "open or close it from any page" in page.locator("#overlay").inner_text()


def test_closing_the_dock_closes_the_microphone(voice):
    page = voice.open(1280, transcripts=[REQUEST], mic=SPEECH, route="/now", wait_panel=False)
    page.locator("#voice-button").wait_for(state="visible", timeout=10000)
    open_dock(page)
    page.wait_for_selector(DOCK_PANEL, timeout=15000)
    voice.hold(1.2)
    until(page, "!!document.querySelector(\".voice-panel[data-mic='open']\") || window.__streams.length >= 1", timeout=15000)
    assert page.evaluate("window.__streams.length") >= 1
    page.click(".voice-dock-close")
    until(page, "window.__streams.every(s => s.getTracks().every(t => t.readyState === 'ended'))")
    assert page.locator(".voice-panel").count() == 0


def test_dock_picks_the_layer_of_the_page(voice, voice_site):
    chat = start_layer.build_layer(voice_site["talk"].parent, "chat", voice_site["app"], None, port_base=28800, voice=True)
    voice_fixtures.helper_mock(chat, [], {"say": "chat here"})
    page = voice.open(1280, layers=(chat,), route="/layer/chat", wait_panel=False)
    page.wait_for_selector("#conn.conn-live", timeout=15000)
    page.locator("#voice-button").wait_for(state="visible", timeout=10000)
    open_dock(page)
    assert page.get_attribute("#voice-dock", "data-layer") == "chat"      # the layer of the page the person is on
    options = page.evaluate("[...document.querySelectorAll('select.voice-dock-layer option')].map(o => o.value)")
    assert options == ["chat", "talk"]                                     # both, in the snapshot's order (by key)
    assert page.input_value("select.voice-dock-layer") == "chat"
    page.select_option("select.voice-dock-layer", "talk")
    page.wait_for_selector("#voice-dock[data-layer='talk'] .voice-panel[data-layer='talk'][data-session='ready']", timeout=15000)
    assert page.locator("#voice-dock .voice-panel").count() == 1
    goto(page, "/now")
    page.reload()
    page.wait_for_selector("#conn.conn-live", timeout=15000)
    page.locator("#voice-button").wait_for(state="visible", timeout=10000)
    open_dock(page)
    assert page.get_attribute("#voice-dock", "data-layer") == "talk"      # remembered in this browser


def test_dock_says_why_voice_is_off(voice):
    page = voice.open(1280, args=("--no-voice",), fake=False, route="/now", wait_panel=False)
    button = page.locator("#voice-button")
    button.wait_for(state="visible", timeout=10000)
    open_dock(page)
    off = page.locator("#voice-dock .voice-dock-off")
    assert off.count() == 1 and "--no-voice" in off.inner_text()
    assert page.locator(".voice-panel").count() == 0


def test_no_voice_layer_no_button(page):  # noqa: F811 — the fixture of test_browser.py (layer demo-shop, no voice)
    open_(page, "/now")
    until(page, "document.getElementById('main').childElementCount > 0")
    assert page.locator("#voice-button").is_hidden()
    page.keyboard.press("v")
    assert page.evaluate("document.getElementById('voice-dock').hidden")
