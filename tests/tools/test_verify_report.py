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


STACKS = ('  (environment worktree #:id a)\n'
          '  (environment worktree #:id b #:dir "trees-b")\n')


def _stacked_layer(app):
    """Three workflows on two environment stacks: `change` and `third` on `a`, `other` on `b`."""
    al("init", app, "--in-repo")
    layer = app / "agentic"
    source = LAYER_TEMPLATE.format(root="..", placement='(layer in-repo #:dir "agentic")')
    source = source.replace('(cmd test "./test.sh"', '(cmd ok "true")\n    (cmd test "./test.sh"')
    source = source.replace("  (command plan ", STACKS + "  (command plan ", 1)
    source = source.replace('(adw change #:description "Plan, build, test; a person reviews"',
                            '(adw other #:environment (b) (steps (check test #:cmd test)))\n'
                            '  (adw third #:environment (a) (steps (check test #:cmd test)))\n'
                            '  (adw change #:description "Plan, build, test; a person reviews" #:environment (a)')
    (layer / "agentic.rkt").write_text(source)
    al("gen", layer)
    (layer / ".gitignore").write_text((layer / ".gitignore").read_text() + "trees-b/\n")
    commit(app, "the agentic layer", "agentic")
    assert status(app) == ""
    return layer


def test_verify_runs_every_distinct_stack_once_and_names_it(app) -> None:
    layer = _stacked_layer(app)
    old = [{"name": "ok", "cmd": "true", "exit": 0, "seconds": 0.0, "log": "agents/verify/ok.log",
            "at": "2026-01-01T00:00:00Z"}]                                        # a report from before stacks
    (layer / "agents" / "verify").mkdir(parents=True, exist_ok=True)
    (layer / "agents" / "verify" / "report.json").write_text(json.dumps(old))
    cp = _verify(layer, app, "ok", "test")
    lines = cp.stdout.splitlines()
    headers = [line for line in lines if line.startswith("verify: 2 cmd(s)")]
    assert len(headers) == 2, cp.stdout                                            # `third` shares `a`: not again
    assert "ADW `other` (b)" in headers[0] and "ADW `third` (a)" in headers[1]
    results = [line.strip() for line in lines if line.strip().startswith(("ok  ", "FAIL"))]
    assert [line.split()[1] + " " + line.split()[-1] for line in results] == [
        "ok [b]", "test [b]", "ok [a]", "test [a]"], cp.stdout
    assert lines[-1].startswith("verify: 4/4 ok")
    rows = json.loads((layer / "agents" / "verify" / "report.json").read_text())
    assert [(r["name"], r.get("stack", ""), r.get("workflow")) for r in rows] == [
        ("ok", "", None), ("ok", "a", "third"), ("ok", "b", "other"), ("test", "a", "third"), ("test", "b", "other")]
    assert all((layer / r["log"]).is_file() for r in rows[1:])
    assert rows[1]["log"] != rows[2]["log"]                                        # one log per cmd and stack


def test_verify_workflow_runs_only_that_stack(app) -> None:
    layer = _stacked_layer(app)
    cp = al("verify", layer, "--cmd", "ok", "--workflow", "other", "--json", env={"NOTES_APP": str(app)})
    rows = json.loads(cp.stdout)
    assert [(r["name"], r["stack"], r["workflow"]) for r in rows] == [("ok", "b", "other")]
    assert "ADW `other` (b)" in cp.stderr and "[b]" not in cp.stderr               # one stack: no labels needed
    cp = al("verify", layer, "--workflow", "nope", ok=False)
    assert cp.returncode == 1 and "no workflow `nope`; workflows: other, third, change" in cp.stderr, cp.stderr
