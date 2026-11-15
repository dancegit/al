"""`al verify` on a layer checked in with the application: work in progress in the checkout (the
developer's, or another process's, even while verify runs) does not fail it; what only a git
command can change in the checkout (HEAD, branch, index) still does. Its report keeps the latest
result of every cmd, with the time it was run."""
from __future__ import annotations

import json
import os

from conftest import LAYER_TEMPLATE, al, commit, status

CMDS = ('(cmd ok "true")\n'
        '    (cmd scribble "echo edited-while-verify-ran > \\"$NOTES_APP/notes.txt\\"")\n'
        '    (cmd stage "git -C \\"$NOTES_APP\\" add notes.txt")\n'
        '    (cmd test "./test.sh"')


def _layer(app):
    al("init", app, "--in-repo")
    layer = app / "agentic"
    source = LAYER_TEMPLATE.format(root="..", placement='(layer in-repo #:dir "agentic")')
    (layer / "agentic.rkt").write_text(source.replace('(cmd test "./test.sh"', CMDS))
    al("gen", layer)
    commit(app, "the agentic layer", "agentic")
    assert status(app) == ""
    return layer


def _verify(layer, app, *names, ok=True):
    args = [arg for name in names for arg in ("--cmd", name)]
    return al("verify", layer, *args, env={"NOTES_APP": str(app)}, ok=ok)


def _report(layer) -> dict[str, dict]:
    return {row["name"]: row for row in json.loads((layer / "agents" / "verify" / "report.json").read_text())}


def test_work_in_progress_does_not_fail_verify_but_a_changed_index_does(app) -> None:
    layer = _layer(app)
    (app / "scratch.txt").write_text("the developer's own work\n")
    (app / "app.py").write_text("NOTES = ['work in progress']\n")
    _verify(layer, app, "ok")                                                  # work in progress before verify
    cp = _verify(layer, app, "scribble")                                       # … and a file edited while it ran
    assert "changed in the checkout while verify ran" in cp.stdout and "notes.txt" in cp.stdout, cp.stdout
    cp = _verify(layer, app, "stage", ok=False)                                # the index: only git does that
    assert cp.returncode == 1 and "CHANGED DURING VERIFY (index)" in cp.stderr, cp.stderr


def test_the_report_keeps_the_latest_result_of_every_cmd(app) -> None:
    layer = _layer(app)
    _verify(layer, app, "ok", "test")
    first = _report(layer)
    assert set(first) == {"ok", "test"} and all(row["at"].endswith("Z") for row in first.values())
    cp = al("verify", layer, "--cmd", "ok", "--json", env={"NOTES_APP": str(app)})
    assert [row["name"] for row in json.loads(cp.stdout)] == ["ok"]           # --json: this invocation only
    second = _report(layer)
    assert list(second) == ["ok", "test"]                                      # declaration order, nothing lost
    assert second["test"] == first["test"] and second["ok"]["at"] >= first["ok"]["at"]
    assert (layer / second["test"]["log"]).is_file()
    help_text = al("verify", "--help").stdout
    assert "report.json" in help_text and "latest result of every cmd" in help_text
    assert os.path.isfile(layer / "agents" / "verify" / "report.json")
