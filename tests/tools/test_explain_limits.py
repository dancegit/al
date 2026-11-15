"""Unit tests for how `al explain` words the (budget limits …) block when it carries a subscription
usage threshold (#:limit-stop-percent, #:on-limit, #:limit-wait-minutes), and for the note that
dollars are API-equivalent on a subscription. explain.py is loaded by its file path; no racket,
network, agent or subprocess, and nothing is written to disk.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

EXPLAIN = Path(__file__).resolve().parents[2] / "tools" / "explain.py"

_spec = importlib.util.spec_from_file_location("al_explain_limits", EXPLAIN)
explain = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(explain)  # type: ignore[union-attr]

NOTE = ("On a subscription login the dollar figures are API-equivalent estimates, not what you pay; a run that the "
        "subscription's limit stops is handed to a person with the window and its reset time")
STOP = ("When the agent CLI reports that 90% of a subscription usage window is used (for Claude Code: the 5-hour or "
        "the weekly window), no new agent step starts and the run is handed to a person, with the window and its "
        "reset time as the reason.")
WAIT = ("When the agent CLI reports that 90% of a subscription usage window is used (for Claude Code: the 5-hour or "
        "the weekly window), no new agent step starts: the run waits until that window resets, at most 120 minutes, "
        "and then continues; if the reset is further away, the run is handed to a person.")


def _budget(**fields) -> dict:
    return {"budget": {"id": "limits", "kind": "limits", "status": "stable", "runUsd": None, "stepUsd": None,
                       "dayUsd": None, **fields}}


def test_no_threshold_says_nothing_about_usage_windows():
    before = ("At most $5 per run and $20 a day for the whole layer. A run that goes over is stopped after the agent "
              "call that crossed the limit and handed to a person, with the reason. Once the day's limit is reached, "
              "nothing new starts.")
    for extra in ({}, {"limitStopPercent": 0, "onLimit": "stop", "limitWaitMinutes": 300},
                  {"limitStopPercent": None}, {"limitStopPercent": True}, {"limitStopPercent": "90"}):
        title, text, _bullets = explain.budget_text(_budget(runUsd=5, dayUsd=20, **extra))
        assert title == "What it may cost"
        assert text == before                       # a layer from before the threshold reads as it did
        assert "usage window" not in text
    _title, text, _bullets = explain.budget_text({"budget": None})
    assert "usage window" not in text and text.startswith("No spending limits.")


def test_threshold_with_stop_is_described_in_plain_words():
    _title, text, _bullets = explain.budget_text(_budget(runUsd=5, limitStopPercent=90, onLimit="stop",
                                                         limitWaitMinutes=300))
    assert text.startswith("At most $5 per run. A run that goes over is stopped")
    assert text.endswith(" " + STOP)
    assert "90% of a subscription usage window" in text and "handed to a person" in text
    assert "waits" not in text


def test_threshold_with_wait_names_the_maximum():
    _title, text, _bullets = explain.budget_text(_budget(dayUsd=20, limitStopPercent=90, onLimit="wait",
                                                         limitWaitMinutes=120))
    assert text.endswith(" " + WAIT)
    assert "waits until that window resets, at most 120 minutes" in text
    _title, text, _bullets = explain.budget_text(_budget(limitStopPercent=80, onLimit="wait"))
    assert "80% of a subscription usage window" in text and "at most 300 minutes" in text   # the default


def test_a_threshold_without_dollar_limits_reads_well():
    _title, text, _bullets = explain.budget_text(_budget(limitStopPercent=90, onLimit="stop", limitWaitMinutes=300))
    assert text == "No spending limit in dollars. " + STOP
    assert "At most no limit" not in text and "goes over" not in text
    _title, text, _bullets = explain.budget_text(_budget())
    assert text == "No spending limit in dollars."


def test_the_api_equivalent_note_is_always_there():
    for ir in (_budget(runUsd=5), _budget(limitStopPercent=90), {"budget": None}, {}):
        _title, _text, bullets = explain.budget_text(ir)
        assert NOTE in bullets
    _title, _text, bullets = explain.budget_text(_budget(runUsd=5))
    assert bullets[:2] == ["Costs are what the agents report; codex and custom agents report none and count as $0",
                           "`--max-cost USD` replaces the per-run limit for one run"]
