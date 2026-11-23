"""The "Ship without review" policy of a layer and the readable size stop of a run
(tests/review_limit_fixtures.py): each auto review option with the source of every value, the local
override beside the declared value, an override the gate does not read, and "diff N lines > limit M
(review option ID), F files changed" on the run's summary and its Needs-you row."""
from __future__ import annotations

import json
from pathlib import Path

import dashboard as d
import review_limit_fixtures as rf


def load(site: dict) -> d.Layer:
    layer = d.load_layer(site["layer"], set())
    assert layer is not None
    return layer


def option(view: dict, option_id: str = rf.OPTION) -> dict:
    return next(o for o in view["options"] if o["id"] == option_id)


def summary_of(layer: d.Layer, run_id: str = rf.RUN) -> dict:
    return d.RunModel(layer, run_id, d.AggCache()).summary()


def row_of(layer: d.Layer, summary: dict) -> dict:
    attention, _waiting = d.layer_attention(layer, [summary], d.Landing(), limit=None)
    return attention[summary["key"]]


def test_policy_lists_each_auto_option_with_sources(tmp_path: Path) -> None:
    view = load(rf.build_review_site(tmp_path)).review_policy_view()
    assert [o["id"] for o in view["options"]] == ["zero-touch"]   # the human option has no limit
    entry = option(view)
    assert entry["ship"] == {"value": "merge", "source": "layer definition"}
    assert entry["requireChecks"] == {"value": ["lint", "test"], "source": "layer definition"}
    assert entry["allowedClasses"] == {"value": ["chore", "bug"], "source": "al's default"}
    assert entry["maxDiffLines"] == {"value": 300, "declared": 300, "source": "layer definition",
                                     "declared_source": "layer definition", "local": None, "set": None,
                                     "ignored": False}
    assert (view["min"], view["max"], view["gate_reads"], view["error"]) == (1, 50000, True, "")
    assert (view["writable"], view["why"]) == (True, "")


def test_policy_shows_the_local_override_beside_the_declared_value(tmp_path: Path) -> None:
    view = load(rf.build_review_site(tmp_path, override=3000)).review_policy_view()
    limit = option(view)["maxDiffLines"]
    assert limit == {"value": 3000, "declared": 300, "source": "local override",
                     "declared_source": "layer definition", "local": 3000,
                     "set": {"who": "Ada", "via": "dashboard", "at": "2026-11-23T10:00:00Z"}, "ignored": False}
    assert option(view)["ship"] == {"value": "merge", "source": "layer definition"}   # the rest is as declared


def test_sources_fall_back_when_the_definition_cannot_be_read(tmp_path: Path) -> None:
    site = rf.build_review_site(tmp_path / "none", definition=False)
    entry = option(load(site).review_policy_view())
    assert {entry[key]["source"] for key in ("ship", "allowedClasses", "requireChecks", "maxDiffLines")} == {
        "compiled layer"}
    assert entry["maxDiffLines"]["declared_source"] == "compiled layer" and entry["maxDiffLines"]["value"] == 300

    # the review clause sits in an included file
    site = rf.build_review_site(tmp_path / "included")
    (site["layer"] / "agentic.rkt").write_text(
        '#lang al\n(agentic-layer #:name "demo-shop"\n  (review human)\n  (include "reviews.rkt")\n  )\n')
    (site["layer"] / "reviews.rkt").write_text(
        "(review auto #:id zero-touch #:ship merge #:max-diff-lines 300)\n")
    assert d.declared_review_keywords(site["layer"]) == {"zero-touch": {"id", "ship", "max-diff-lines"}}
    entry = option(load(site).review_policy_view())
    assert entry["maxDiffLines"]["source"] == "layer definition" and entry["ship"]["source"] == "layer definition"
    assert entry["requireChecks"]["source"] == "al's default"

    # an include that leaves the layer directory is not followed
    (tmp_path / "outside.rkt").write_text("(review auto #:id zero-touch #:max-diff-lines 300)\n")
    (site["layer"] / "agentic.rkt").write_text(
        '#lang al\n(agentic-layer #:name "demo-shop"\n  (include "../outside.rkt")\n  )\n')
    assert d.declared_review_keywords(site["layer"]) is None

    # a keyword inside a comment or a string does not count; an option without #:id is `auto`
    (site["layer"] / "agentic.rkt").write_text(
        '#lang al\n(agentic-layer #:name "demo-shop"\n'
        '  ;; (review auto #:id zero-touch #:max-diff-lines 300)\n'
        '  (review auto #:id zero-touch #:ship merge ; #:max-diff-lines 300\n'
        '          #:body "use #:max-diff-lines 300 (or more)" #:require-checks (lint test))\n'
        '  (review auto #:reviewers 1)\n'
        '  )\n')
    assert d.declared_review_keywords(site["layer"]) == {
        "zero-touch": {"id", "ship", "body", "require-checks"}, "auto": {"reviewers"}}
    entry = option(load(site).review_policy_view())
    assert entry["maxDiffLines"]["source"] == "al's default" and entry["requireChecks"]["source"] == "layer definition"

    # a definition without a (review auto …) form, and an option the definition does not hold
    (site["layer"] / "agentic.rkt").write_text('#lang al\n(agentic-layer #:name "demo-shop"\n  (review human)\n  )\n')
    assert d.declared_review_keywords(site["layer"]) is None
    (site["layer"] / "agentic.rkt").write_text(
        '#lang al\n(agentic-layer #:name "demo-shop"\n  (review auto #:id other #:max-diff-lines 9)\n  )\n')
    assert option(load(site).review_policy_view())["maxDiffLines"]["source"] == "compiled layer"


def test_broken_setting_is_an_error_and_not_used(tmp_path: Path) -> None:
    site = rf.build_review_site(tmp_path)
    path = site["layer"] / d.REVIEW_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "maxDiffLines": {"zero-touch": 0}}))
    view = load(site).review_policy_view()
    assert view["error"].startswith("agents/settings/review.json: ") and "al review LAYER --reset" in view["error"]
    assert str(tmp_path) not in view["error"]
    limit = option(view)["maxDiffLines"]
    assert (limit["value"], limit["source"], limit["local"], limit["set"], limit["ignored"]) == (
        300, "layer definition", None, None, False)
    path.write_text("{not json")
    assert "not JSON" in load(site).review_policy_view()["error"]


def test_not_writable_when_the_gate_does_not_read_the_limit(tmp_path: Path) -> None:
    site = rf.build_review_site(tmp_path / "old", gate_reads=False, override=3000)
    view = load(site).review_policy_view()
    assert view["gate_reads"] is False and view["writable"] is False
    assert view["why"] == "this layer's gate does not read the local limit: run `al gen LAYER`"
    limit = option(view)["maxDiffLines"]
    assert (limit["value"], limit["source"], limit["local"], limit["ignored"]) == (300, "layer definition", 3000, True)
    assert limit["set"]["who"] == "Ada"

    (site["layer"] / d.REVIEW_GATE_FILE).unlink()   # no gate file at all reads the same
    assert load(site).review_policy_view()["gate_reads"] is False

    view = load(rf.build_review_site(tmp_path / "new")).review_policy_view(read_only=True)
    assert (view["writable"], view["why"]) == (False, "the dashboard was started with --read-only")


def test_size_stop_on_the_summary_and_the_needs_you_row(tmp_path: Path) -> None:
    site = rf.build_review_site(tmp_path)
    layer = load(site)
    summary = summary_of(layer)
    assert summary["status"] == "handed-over"
    assert summary["outcome"]["size_stop"] == {"diff_lines": 2500, "limit": 300, "option": "zero-touch", "files": 12,
                                               "text": rf.TEXT}
    assert rf.TEXT == "diff 2500 lines > limit 300 (review option zero-touch), 12 files changed"
    assert summary["outcome"]["handover"] == ["diff-too-large:2500>300"]   # the raw reason stays for its readers
    row = row_of(layer, summary)
    assert (row["bucket"], row["reason"]) == ("needs-you", rf.TEXT)

    # other reasons stay as they are, in their order
    rf.write_run(site["layer"], "d1ff0002", reasons=["class-not-allowed:feature", "diff-too-large:2500>300"])
    summary = summary_of(layer, "d1ff0002")
    assert row_of(layer, summary)["reason"] == f"class-not-allowed:feature, {rf.TEXT}"

    # a gate record without changed_files drops the files part; one file is singular
    rf.write_run(site["layer"], "d1ff0003", files=None)
    stop = summary_of(layer, "d1ff0003")["outcome"]["size_stop"]
    assert (stop["files"], stop["text"]) == (None, "diff 2500 lines > limit 300 (review option zero-touch)")
    rf.write_run(site["layer"], "d1ff0004", files=1)
    stop = summary_of(layer, "d1ff0004")["outcome"]["size_stop"]
    assert (stop["files"], stop["text"]) == (1, "diff 2500 lines > limit 300 (review option zero-touch), 1 file changed")
    assert d.size_stop(["diff-too-large:9>4"], {}, "")["text"] == "diff 9 lines > limit 4"

    # a run stopped for another reason
    rf.write_run(site["layer"], "d1ff0005", reasons=["class-not-allowed:feature"])
    summary = summary_of(layer, "d1ff0005")
    assert summary["outcome"]["size_stop"] is None
    assert row_of(layer, summary)["reason"] == "class-not-allowed:feature"
    assert d.readable_reasons(["a", "diff-too-large:1>0"], None) == ["a", "diff-too-large:1>0"]


def test_policy_lines(tmp_path: Path) -> None:
    site = rf.build_review_site(tmp_path / "a")
    assert d.review_policy_lines(load(site).review_policy_view()) == [
        "zero-touch: max-diff-lines 300 (layer definition)"]
    (site["layer"] / "agentic.rkt").unlink()
    assert d.review_policy_lines(load(site).review_policy_view()) == [
        "zero-touch: max-diff-lines 300 (compiled layer)"]
    (site["layer"] / "agentic.rkt").write_text('(agentic-layer #:name "demo-shop" (review auto #:id zero-touch))\n')
    assert d.review_policy_lines(load(site).review_policy_view()) == [
        "zero-touch: max-diff-lines 300 (al's default)"]
    site = rf.build_review_site(tmp_path / "b", override=3000)
    assert d.review_policy_lines(load(site).review_policy_view()) == [
        "zero-touch: max-diff-lines 3000 (local override; declared 300)"]
    site = rf.build_review_site(tmp_path / "c", override=3000, gate_reads=False)
    assert d.review_policy_lines(load(site).review_policy_view()) == [
        "zero-touch: max-diff-lines 300 (layer definition; the local override 3000 is not read by this layer's gate: "
        "run al gen)"]
