from __future__ import annotations

from adw_modules.templates import render, render_step_arg


def test_render_arguments_and_named_variables() -> None:
    rendered = render("${prompt} | $ARGUMENTS | $1/$2/$9 | ${missing} | ${adw_id}", {"prompt": "Build it", "adw_id": "1234abcd"}, ["one", "two"])
    assert rendered == "Build it | one two | one/two/ | ${missing} | 1234abcd"


def test_render_does_not_resubstitute_values() -> None:
    assert render("$1 ${plan}", {"plan": "$2 ${prompt}", "prompt": "x"}, ["$ARGUMENTS", "b"]) == "$ARGUMENTS $2 ${prompt}"


def test_render_ignores_names_outside_the_variable_set() -> None:
    assert render("${HOME} ${class}", {"HOME": "/root", "class": "bug"}, []) == "${HOME} bug"


def test_step_args_substitution() -> None:
    assert render_step_arg("$prompt $plan $adw_id $class $branch $out.plan-chore", {
        "prompt": "p", "plan": "spec.md", "adw_id": "1234abcd", "class": "bug", "branch": "bug/x",
    }, {"plan-chore": "result.md"}) == "p spec.md 1234abcd bug bug/x result.md"
    assert render_step_arg("$planning $out.unknown", {"plan": "x"}, {}) == "$planning $out.unknown"
