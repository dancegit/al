"""`al check` says up front what happens to the result of a workflow that can change files but has
neither a commit nor a ship step: the runtime keeps it on a local branch. A warning, never a failure."""
from __future__ import annotations

from conftest import LAYER_TEMPLATE, al

WARNING = ("warning: workflow `only-plan` can change files but has neither a commit nor a ship step: "
           "its result is kept on a local branch")


def test_check_warns_about_a_workflow_kept_on_a_branch(app) -> None:
    layer = app.parent / "notes.al"
    layer.mkdir()
    source = LAYER_TEMPLATE.format(root="../notes", placement="(layer sibling)")
    source = source.replace("(adw change", '(adw only-plan\n    (steps (run plan #:command plan #:args ("$prompt"))))\n  (adw change')
    (layer / "agentic.rkt").write_text(source)
    al("gen", layer)
    cp = al("check", layer)
    lines = cp.stdout.splitlines()
    assert cp.returncode == 0 and lines[-1] == "check: ok", cp.stdout
    assert WARNING in lines, cp.stdout
    assert not any("`change`" in line for line in lines if line.startswith("warning:")), cp.stdout
