"""Every workflow says where its agent and where its checks run. `al explain` has three wordings:
the agent inside the container, the agent outside it (naming `#:agent-inside #f`), and no container
at all (on your machine, in a private copy, with the Nix shell named when there is one). The
workflow table of the generated README carries the same two facts as two columns."""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import LAYER_TEMPLATE, al

BOX_IN = '(environment worktree) (environment podman #:agent-inside #t)'
BOX_OUT = '(environment worktree) (environment podman #:agent-inside #f)'
NIX = '(environment worktree) (environment nix-devshell #:packages ("python312"))'

INSIDE = "The agent runs inside the container; checks run inside the container."
OUTSIDE = ("The agent runs on your machine, outside the container, with your own access (`#:agent-inside #f`); "
           "checks run inside the container.")
NO_BOX = "The agent and the checks run on your machine, in a private copy of the repository."
NO_BOX_NIX = "The agent and the checks run on your machine, inside the Nix shell, in a private copy of the repository."
HEADER = "| ADW | Prompt input | Trigger | Environment | Agent runs in | Checks run in | Review | Steps |"


def _layer(app, environments: str, stack: str) -> Path:
    layer = app.parent / "notes.al"
    layer.mkdir()
    source = LAYER_TEMPLATE.format(root="../notes", placement=f"(layer sibling)\n  {environments}")
    (layer / "agentic.rkt").write_text(source.replace("(adw change", f"(adw change #:environment ({stack})"))
    al("gen", layer)
    return layer


def _workflow(layer, tmp_path) -> str:
    out = tmp_path / "explained.md"
    al("explain", layer, "--md", out)
    text = out.read_text()
    return text[text.index("### Workflow: change"):]


def _row(layer) -> list[str]:
    line = next(ln for ln in (layer / "README.md").read_text().splitlines() if ln.startswith("| `change` |"))
    return [cell.strip() for cell in line.split("|")][1:-1]


def test_container_with_the_agent_inside(app, tmp_path) -> None:
    text = _workflow(_layer(app, BOX_IN, "worktree podman"), tmp_path)
    assert INSIDE in text
    assert "on your machine" not in next(ln for ln in text.splitlines() if "The agent runs" in ln)


def test_container_with_the_agent_outside_names_the_setting(app, tmp_path) -> None:
    assert OUTSIDE in _workflow(_layer(app, BOX_OUT, "worktree podman"), tmp_path)


def test_no_container_runs_on_your_machine_in_a_private_copy(app, tmp_path) -> None:
    text = _workflow(_layer(app, "(environment worktree)", "worktree"), tmp_path)
    assert NO_BOX in text
    sentence = text[text.index("The agent and the checks run"):].split(".")[0]
    assert "container" not in sentence


def test_no_container_with_a_nix_shell_mentions_it(app, tmp_path) -> None:
    assert NO_BOX_NIX in _workflow(_layer(app, NIX, "worktree nix-devshell"), tmp_path)


def test_a_declared_container_the_workflow_does_not_list_is_not_claimed(app, tmp_path) -> None:
    text = _workflow(_layer(app, BOX_IN, "worktree"), tmp_path)
    assert NO_BOX in text and "inside the container" not in text


def test_terminal_output_states_the_placement(app) -> None:
    out = al("explain", _layer(app, BOX_OUT, "worktree podman")).stdout
    assert ("outside the container, with your own access (#:agent-inside #f); checks run inside the container."
            in " ".join(out.split()))


@pytest.mark.parametrize("environments, stack, agent_cell, checks_cell", [
    (BOX_IN, "worktree podman", "container", "container"),
    (BOX_OUT, "worktree podman", "your machine (`#:agent-inside #f`)", "container"),
    ("(environment worktree)", "worktree", "your machine", "your machine"),
    (NIX, "worktree nix-devshell", "your machine (Nix shell)", "your machine (Nix shell)"),
])
def test_readme_table_says_where_agent_and_checks_run(app, environments, stack, agent_cell, checks_cell) -> None:
    layer = _layer(app, environments, stack)
    assert HEADER in (layer / "README.md").read_text().splitlines()
    assert _row(layer)[4:6] == [agent_cell, checks_cell]
