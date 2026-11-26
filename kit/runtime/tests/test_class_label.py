"""A class label at the start of a request (runner.request_label) and how runner._new_state fixes
the class from it: `--class` and a prompt source's class win, a class the workflow does not take
is left to the classify agent."""
from __future__ import annotations

import pytest

from adw_modules import runner

BODY = "The stop box is static; it must follow the real stop path."
ALL = {"classes": ["chore", "bug", "feature"]}

LABELLED = [
    ("# BUGFIX run for the charts layer", "bug"), ("BUGFIX: x", "bug"), ("Bugfix for x", "bug"), ("Bug: x", "bug"),
    ("bug: x", "bug"), ("[bug] x", "bug"), ("zte BUGFIX x", "bug"), ("URGENT BUGFIX x", "bug"), ("Fix: x", "bug"),
    ("## Bug: x", "bug"),
    ("Chore: x", "chore"), ("[chore] x", "chore"),
    ("Feature: x", "feature"), ("[feature] x", "feature"),
]
UNLABELLED = ["Fix the login redirect", "Feature flags are broken", "Bugs in the parser", "Debug: x", ""]


def _state(prompt: str, adw: dict = ALL, *options: str, klass: str | None = None):
    args = runner._parser("demo").parse_args([prompt, *options])
    return runner._new_state("demo", adw, args, runner.Request(args.prompt, "cli", klass, None, None))


@pytest.mark.parametrize("line,klass", LABELLED)
def test_each_label_maps_to_its_class(line, klass):
    assert runner.request_label(f"{line}\n\n{BODY}") == (klass, line)
    assert runner.request_label(f"\n  \n{line}\n\n{BODY}") == (klass, line)
    assert runner.request_label(line) == (klass, line)


def test_the_label_line_is_clipped_and_emphasis_marks_do_not_hide_it():
    assert runner.request_label("**Bug:** the total is wrong") == ("bug", "**Bug:** the total is wrong")
    long = "Bug: " + "x" * 200
    assert runner.request_label(long) == ("bug", long[:80])


@pytest.mark.parametrize("prompt", ["Improve the chart\n\nBug: the box is static", "Add stop paths\n# BUGFIX notes"])
def test_a_label_in_the_body_does_not_count(prompt):
    assert runner.request_label(prompt) is None


@pytest.mark.parametrize("prompt", UNLABELLED)
def test_no_label_leaves_the_class_to_the_agent(prompt):
    assert runner.request_label(prompt) is None
    assert runner.request_label(f"{prompt}\n\n{BODY}") is None
    state = _state(f"{prompt}\n\n{BODY}")
    assert state.class_fixed is False and state.class_from == "" and state.klass == "chore"


def test_new_state_fixes_the_class_from_the_label():
    state = _state(f"# BUGFIX run for the charts layer\n\n{BODY}")
    assert (state.klass, state.class_fixed, state.class_from) == ("bug", True, "request-label")
    assert _state(f"[feature] x\n\n{BODY}", {}).klass == "feature", "a workflow without a class list takes any class"


def test_option_and_source_class_win_over_the_label():
    prompt = f"# BUGFIX run for the charts layer\n\n{BODY}"
    option = _state(prompt, ALL, "--class", "feature")
    assert (option.klass, option.class_fixed, option.class_from) == ("feature", True, "")
    source = _state(prompt, ALL, klass="chore")
    assert (source.klass, source.class_fixed, source.class_from) == ("chore", True, "")


def test_a_label_the_workflow_does_not_accept_is_left_to_the_agent():
    state = _state(f"Bug: the total is wrong\n\n{BODY}", {"classes": ["chore"]})
    assert (state.klass, state.class_fixed, state.class_from) == ("chore", False, "")
