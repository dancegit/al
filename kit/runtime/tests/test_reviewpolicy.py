"""The local ship-without-review limit (adw_modules/reviewpolicy.py): the precedence (the override
if set, else the declared value), the validation, the file with who and when, and the gate's line."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from adw_modules import reviewpolicy
from adw_modules.errors import LayerError

AUTO = {"id": "zero-touch", "kind": "auto", "maxDiffLines": 2000}
HUMAN = {"id": "human", "kind": "human"}
AT = "2026-11-23T10:00:00Z"


def setting_file(layer: Path) -> Path:
    return layer / reviewpolicy.SETTING_FILE


def put(layer: Path, text: str) -> Path:
    path = setting_file(layer)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def leftovers(layer: Path) -> list[str]:
    return sorted(p.name for p in (layer / "agents" / "settings").glob(".review.*.tmp"))


def test_no_setting_means_the_declared_limit(tmp_path: Path) -> None:
    setting = reviewpolicy.read_setting(tmp_path)
    assert setting == {"maxDiffLines": {}, "set": {}}
    assert reviewpolicy.effective(AUTO, setting) == {
        "option": "zero-touch", "declared": 2000, "limit": 2000, "source": "declared"}
    option, info = reviewpolicy.apply(tmp_path, AUTO)
    assert option["maxDiffLines"] == 2000
    assert info["limit"] == 2000 and info["source"] == reviewpolicy.SOURCE_DECLARED and "note" not in info


def test_override_wins_over_the_declared_limit(tmp_path: Path) -> None:
    reviewpolicy.write_setting(tmp_path, "zero-touch", 3000, who="Ada", at=AT)
    setting = reviewpolicy.read_setting(tmp_path)
    assert setting["maxDiffLines"] == {"zero-touch": 3000}
    assert reviewpolicy.effective(AUTO, setting) == {
        "option": "zero-touch", "declared": 2000, "limit": 3000, "source": "local override"}
    given = dict(AUTO)
    option, info = reviewpolicy.apply(tmp_path, given)
    assert option is not given and option["maxDiffLines"] == 3000
    assert given == AUTO and given["maxDiffLines"] == 2000   # the option passed in is not mutated
    assert info == {"option": "zero-touch", "declared": 2000, "limit": 3000, "source": "local override"}


def test_override_of_another_or_a_non_auto_option_is_not_applied(tmp_path: Path) -> None:
    reviewpolicy.write_setting(tmp_path, "other", 3000, who="Ada", at=AT)
    option, info = reviewpolicy.apply(tmp_path, AUTO)
    assert option["maxDiffLines"] == 2000 and info["source"] == "declared" and info["limit"] == 2000

    reviewpolicy.write_setting(tmp_path, "human", 3000, who="Ada", at=AT)
    assert reviewpolicy.apply(tmp_path, HUMAN) == (HUMAN, {})
    assert reviewpolicy.apply(tmp_path, HUMAN)[0] is HUMAN
    info = reviewpolicy.effective(HUMAN, reviewpolicy.read_setting(tmp_path))
    assert info["source"] == "declared" and info["limit"] == 0


@pytest.mark.parametrize("value, good", [
    (1, True), (50000, True),
    (0, False), (-1, False), (50001, False), ("3000", False), (2.5, False), (True, False), (None, False),
])
def test_limit_validation(tmp_path: Path, value: object, good: bool) -> None:
    if good:
        assert reviewpolicy.check_limit(value) == value
        reviewpolicy.write_setting(tmp_path, "zero-touch", value, who="Ada", at=AT)
        assert reviewpolicy.read_setting(tmp_path)["maxDiffLines"] == {"zero-touch": value}
    else:
        with pytest.raises(ValueError, match="whole number from 1 to 50000"):
            reviewpolicy.check_limit(value)
        if value is not None:   # None is the reset of write_setting, not a value
            with pytest.raises(ValueError, match="whole number from 1 to 50000"):
                reviewpolicy.write_setting(tmp_path, "zero-touch", value, who="Ada", at=AT)
        assert not setting_file(tmp_path).exists() and leftovers(tmp_path) == []
    for who in ("", "   ", "x" * 81, "Ada\nLovelace", None):
        with pytest.raises(ValueError, match="who: a name of 1 to 80 characters"):
            reviewpolicy.check_who(who)
    with pytest.raises(ValueError, match="who"):
        reviewpolicy.write_setting(tmp_path / "other", "zero-touch", 3000, who="", at=AT)
    assert not setting_file(tmp_path / "other").exists()
    assert reviewpolicy.check_who("  Ada ") == "Ada" and reviewpolicy.check_who("x" * 80) == "x" * 80


def test_write_then_reset_records_who_and_when_and_removes_the_file(tmp_path: Path) -> None:
    path = reviewpolicy.write_setting(tmp_path, "zero-touch", 3000, who="Ada", via="cli", at=AT)
    assert path == setting_file(tmp_path)
    assert path.read_text(encoding="utf-8") == reviewpolicy.render_setting(
        {"zero-touch": 3000}, {"zero-touch": {"at": AT, "via": "cli", "who": "Ada"}})
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1

    reviewpolicy.write_setting(tmp_path, "nightly", 500, who="Grace")   # the time is now, in UTC
    setting = reviewpolicy.read_setting(tmp_path)
    assert setting["maxDiffLines"] == {"zero-touch": 3000, "nightly": 500}
    assert setting["set"]["zero-touch"] == {"at": AT, "via": "cli", "who": "Ada"}
    stamp = setting["set"]["nightly"]
    assert stamp["who"] == "Grace" and stamp["via"] == "cli"
    assert len(stamp["at"]) == len(AT) and stamp["at"].endswith("Z") and stamp["at"][10] == "T"

    reviewpolicy.write_setting(tmp_path, "zero-touch", None)   # reset one: the other stays
    setting = reviewpolicy.read_setting(tmp_path)
    assert setting["maxDiffLines"] == {"nightly": 500} and list(setting["set"]) == ["nightly"]

    reviewpolicy.write_setting(tmp_path, "nightly", None)   # reset the last: the file goes
    assert not path.exists()
    assert reviewpolicy.read_setting(tmp_path) == {"maxDiffLines": {}, "set": {}}

    reviewpolicy.write_setting(tmp_path, "zero-touch", 3000, who="Ada", at=AT)
    reviewpolicy.write_setting(tmp_path, None, None)   # reset all
    assert not path.exists()
    assert leftovers(tmp_path) == []


def test_the_request_example_file_is_read(tmp_path: Path) -> None:
    put(tmp_path, '{"version": 1, "maxDiffLines": {"zero-touch": 3000}}')
    setting = reviewpolicy.read_setting(tmp_path)
    assert setting == {"maxDiffLines": {"zero-touch": 3000}, "set": {}}
    option, info = reviewpolicy.apply(tmp_path, AUTO)
    assert option["maxDiffLines"] == 3000 and info["source"] == "local override"


@pytest.mark.parametrize("text", [
    "{not json",
    '["zero-touch", 3000]',
    '{"version": 1, "maxDiffLines": {"zero-touch": 0}}',
    '{"version": 1, "maxDiffLines": {"zero-touch": 60000}}',
    '{"version": 1, "maxDiffLines": [3000]}',
    '{"version": 1, "maxDiffLines": {"zero-touch": 3000}, "set": {"zero-touch": "Ada"}}',
    '{"version": 1, "maxDiffLines": {"zero-touch": 3000}, "set": {"zero-touch": {"who": 7}}}',
])
def test_broken_setting_raises_with_the_fix_and_apply_falls_back_to_declared(tmp_path: Path, text: str) -> None:
    path = put(tmp_path, text)
    with pytest.raises(LayerError) as caught:
        reviewpolicy.read_setting(tmp_path)
    assert str(path) in str(caught.value) and "al review LAYER --reset" in str(caught.value)

    option, info = reviewpolicy.apply(tmp_path, AUTO)   # never raises a limit, never crashes the gate
    assert option["maxDiffLines"] == 2000
    assert info["limit"] == 2000 and info["source"] == "declared" and info["declared"] == 2000
    assert info["note"].startswith("the local setting is ignored: ") and str(path) in info["note"]

    reviewpolicy.write_setting(tmp_path, "zero-touch", 3000, who="Ada", at=AT)   # a broken file is replaced
    assert reviewpolicy.read_setting(tmp_path)["maxDiffLines"] == {"zero-touch": 3000}


def test_gate_line_names_the_limit_and_its_source(tmp_path: Path) -> None:
    reviewpolicy.write_setting(tmp_path, "zero-touch", 3000, who="Ada", at=AT)
    assert reviewpolicy.gate_line(reviewpolicy.apply(tmp_path, AUTO)[1]) == \
        "gate: max-diff-lines 3000 (local override; declared 2000)"
    reviewpolicy.write_setting(tmp_path, None, None)
    assert reviewpolicy.gate_line(reviewpolicy.apply(tmp_path, AUTO)[1]) == "gate: max-diff-lines 2000 (declared)"
    path = put(tmp_path, "{not json")
    line = reviewpolicy.gate_line(reviewpolicy.apply(tmp_path, AUTO)[1])
    assert line.startswith("gate: max-diff-lines 2000 (declared); the local setting is ignored: ")
    assert str(path) in line and "al review LAYER --reset" in line
