"""The names of the screenshots the browser tests save to AL_DASHBOARD_SHOTS. Reads the test files only;
no browser is needed."""
import re
from pathlib import Path

SHOT_CALL = re.compile(r'shot\(\s*\w+\s*,\s*"([^"/]+\.png)"')
SCREENSHOT_PATH = re.compile(r'/\s*"([^"/]+\.png)"')


def shot_names() -> set[str]:
    names: set[str] = set()
    for path in Path(__file__).parent.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        names.update(SHOT_CALL.findall(text))
        for line in text.splitlines():
            if "screenshot(" in line:
                names.update(SCREENSHOT_PATH.findall(line))
    return names


def test_delivery_request_shots_are_among_the_twelve_handed_over():
    # The review's `screenshots` artefact (kit/dashboard/shots/*.png) hands over the first 12 shots by
    # name, and C10's Writes glob (kit/dashboard/shots/delivery-request-*.png) must be among them.
    names = shot_names()
    wanted = {"delivery-request-run.png", "delivery-request-stages.png"}
    assert wanted <= names, f"not saved by any test: {sorted(wanted - names)}"
    first = sorted(names)[:12]
    assert wanted <= set(first), f"outside the first twelve: {sorted(wanted - set(first))}; first twelve: {first}"
