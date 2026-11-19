"""`al explain` for a layer with `(placement …)`: where runs may execute, why a workflow stays on
the home machine, and nothing about hosts for a layer that declares none."""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import LAYER_TEMPLATE, PLACEMENT_LAYER, al


@pytest.fixture
def placement_layer(app: Path):
    """<app>/agentic generated from PLACEMENT_LAYER (changed by `edit` first); nothing is deployed."""
    def build(edit=lambda text: text) -> Path:
        al("init", app, "--in-repo")
        layer = app / "agentic"
        (layer / "agentic.rkt").write_text(edit(PLACEMENT_LAYER).format(root="..", placement='(layer in-repo #:dir "agentic")'))
        al("gen", layer)
        return layer
    return build


def _explained(layer: Path) -> str:
    """What `al explain` prints for the terminal, on one line (it wraps paragraphs)."""
    return " ".join(al("explain", layer).stdout.split())


def test_explain_says_where_runs_may_execute(placement_layer):
    text = _explained(placement_layer())
    assert "Where runs execute" in text
    assert "Runs may execute on 3 machines: home, build-1 and build-2 (home is the machine that holds this layer)" in text
    assert "picks the one with the lowest share of its runs in use" in text
    assert "At most 2 runs at once on each host" in text
    assert "can only push its own branch" in text and "only the home machine changes the default branch" in text
    assert "AL_GIT_SOURCE" in text and "AL_HOST_<ID>" in text
    assert "al hosts LAYER status" in text and "al host LAYER HOST doctor" in text and "--restart RUN" in text
    assert "It may run on: home, build-1, build-2." in text
    assert "It runs on home only" not in text and "Runs on home only" not in text


def test_explain_says_why_a_workflow_stays_home(placement_layer):
    layer = placement_layer(lambda text: text.replace(
        '(cmd test "./test.sh" #:timeout 60 #:description "the tests pass")',
        '(cmd test "./test.sh" #:timeout 60 #:description "the tests pass" #:needs-home #t)'))
    text = _explained(layer)
    assert "It runs on home only: cmd test needs home (#:needs-home)." in text
    assert "Runs on home only: change (cmd test needs home (#:needs-home))" in text
    assert "It may run on:" not in text


def test_explain_names_a_reserved_host_and_the_first_rule(placement_layer):
    layer = placement_layer(lambda text: text.replace(
        "#:max-runs-per-host 2)", "#:prefer first #:reserve ((host build-2) (classes (chore bug))))\n"
                                  "  (concurrency slots #:max-runs 3)"))
    text = _explained(layer)
    assert "picks the first one with a free run" in text
    assert "build-2 is kept for chore and bug runs whose class is fixed when they start" in text
    assert "At most 3 runs, 2 agent calls and 2 checks at the same time on each host, shared by everything that can start a run there." in text


def test_explain_is_unchanged_without_placement(in_repo):
    text = _explained(in_repo)
    assert "host" not in text.lower()
    assert "Where runs execute" not in text and "It may run on" not in text and "home only" not in text


def test_the_concurrency_sentence_is_unchanged_without_placement(app):
    al("init", app, "--in-repo")
    layer = app / "agentic"
    (layer / "agentic.rkt").write_text(LAYER_TEMPLATE.format(
        root="..", placement='(layer in-repo #:dir "agentic")\n  (concurrency slots #:max-runs 3)'))
    al("gen", layer)
    text = _explained(layer)
    assert "At most 3 runs, 2 agent calls and 2 checks at the same time, shared by everything that can start a run." in text
    assert "host" not in text.lower()


def test_explain_warns_about_published_ports_on_hosts(placement_layer):
    layer = placement_layer(lambda text: text.replace("(environment podman)", "(environment podman #:publish (8000))"))
    assert "publishes ports: keep #:max-runs-per-host 1" in _explained(layer)
    one = placement_layer(lambda text: text.replace("(environment podman)", "(environment podman #:publish (8000))")
                          .replace("#:max-runs-per-host 2", "#:max-runs-per-host 1"))
    assert "publishes ports" not in _explained(one)
