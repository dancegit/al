"""Checks that say what they cover, as a user of tools/al sees them: the fixture layer compiles
with its one warning, `al gen` writes the expanded check steps and the runtime module, `al explain`
says what a check covers and when it runs, and `al verify` and `al sandbox doctor` report checks
without a coverage declaration and source paths no check covers."""
from __future__ import annotations

import filecmp
import json
import os
import shutil
from pathlib import Path

import pytest

from conftest import LAYER_TEMPLATE, ROOT, al, commit, git
from test_sandbox_cli import ENVIRONMENTS, podman  # noqa: F401 — the podman stub fixture

pytestmark = pytest.mark.skipif(shutil.which("racket") is None, reason="racket is not on PATH")

FIXTURE = ROOT / "racket" / "al" / "tests" / "fixtures" / "affected-checks.rkt"
WARNING = ("review `zero-touch` has #:require affected and #:ship merge: a required check no changed path touches "
           "is not run before the change ships without a person; a wrong #:covers then ships unproven code")

# LAYER_TEMPLATE with a check that covers the source file, one that declares nothing, and a source
# path no check covers.
COVER_LAYER = (LAYER_TEMPLATE
               .replace('(cmd test "./test.sh" #:timeout 60 #:description "the tests pass")',
                        '(cmd test "./test.sh" #:timeout 60 #:description "the tests pass" #:covers ("app.py"))\n'
                        '    (cmd lint "true")')
               .replace('(paths #:source ("app.py")', '(paths #:source ("app.py" "lib")')
               .replace("(check test #:cmd test)", "(check test #:cmd test) (check lint #:cmd lint)"))
assert COVER_LAYER.count("#:covers") == 1 and "(check lint #:cmd lint)" in COVER_LAYER and '"lib")' in COVER_LAYER


def _fixture_layer(app: Path) -> Path:
    layer = app.parent / "notes.al"
    layer.mkdir()
    (layer / "agentic.rkt").write_text(FIXTURE.read_text().replace('#:root "."', '#:root "../notes"'))
    return layer


def _sibling(app: Path, source: str, name: str = "notes.al") -> Path:
    layer = app.parent / name
    layer.mkdir()
    (layer / "agentic.rkt").write_text(source.format(root="../notes", placement="(layer sibling)"))
    al("gen", layer)
    return layer


def test_fixture_compiles_with_the_documented_warning(tmp_path) -> None:
    shutil.copy(FIXTURE, tmp_path / "agentic.rkt")
    cp = al("compile", tmp_path)
    assert cp.returncode == 0
    warnings = [line for line in cp.stderr.splitlines() if "warning:" in line]
    assert len(warnings) == 1 and warnings[0].endswith(f"warning: {WARNING}"), cp.stderr
    ir = json.loads(cp.stdout)
    cmds = {c["name"]: c for c in ir["codebase"]["commands"]}
    assert (cmds["suite-a"]["covers"], cmds["suite-a"]["always"], cmds["suite-a"]["reuse"]) == (["a"], False, True)
    assert (cmds["flaky"]["always"], cmds["flaky"]["reuse"]) == (True, False)
    assert next(r for r in ir["reviews"] if r["id"] == "zero-touch")["require"] == "affected"
    assert next(r for r in ir["reviews"] if r["id"] == "human").get("require") is None   # only the gate has it


def test_gen_writes_the_runtime_module_and_the_expanded_steps(app) -> None:
    layer = _fixture_layer(app)
    cp = al("gen", layer)
    assert WARNING in cp.stderr
    assert (layer / "adws" / "adw_modules" / "affected.py").read_bytes() == (
        ROOT / "kit" / "runtime" / "adw_modules" / "affected.py").read_bytes()
    ir = json.loads((layer / "agentic-layer.json").read_text())
    for adw in ir["adws"]:
        checks = [step for step in adw["steps"] if step["kind"] == "check"]
        # (check tests #:cmds affected): one ordinary check step per cmd with #:covers, named after it
        assert [(s["name"], s["cmd"], s["whenAffected"]) for s in checks] == [
            ("suite-a", "suite-a", True), ("suite-b", "suite-b", True), ("docs", "docs", True), ("flaky", "flaky", True)]
        assert not any(step["name"] == "tests" or "cmds" in step for step in adw["steps"])
        assert {step["kind"] for step in adw["steps"]} <= {"run", "check", "review", "ship"}


def test_gen_of_a_layer_without_the_clauses_differs_only_by_defaulted_ir_keys(tmp_path) -> None:
    """A layer that declares nothing equals, file for file, the same layer with the defaults written
    out: the new clauses change no generated file unless they are used."""
    trees = []
    for name, cmd in (("plain", '(cmd test "./test.sh" #:timeout 60 #:description "the tests pass")'),
                      ("explicit", '(cmd test "./test.sh" #:timeout 60 #:description "the tests pass" '
                                   "#:covers () #:always #t #:reuse #t)")):
        home = tmp_path / name
        (home / "notes").mkdir(parents=True)
        git(home / "notes", "init", "-q", "-b", "main")
        (home / "notes" / "app.py").write_text("NOTES = []\n")
        commit(home / "notes", "the app")
        source = LAYER_TEMPLATE.replace('(cmd test "./test.sh" #:timeout 60 #:description "the tests pass")', cmd)
        assert cmd in source
        trees.append(_sibling(home / "notes", source))
    plain, explicit = trees

    def files(layer: Path) -> list[str]:
        return sorted(p.relative_to(layer).as_posix() for p in layer.rglob("*")
                      if p.is_file() and ".git/" not in p.relative_to(layer).as_posix() + "/"
                      and "__pycache__" not in p.parts)

    assert files(plain) == files(explicit)
    # the definition itself is what differs, and with it the records that hash or quote it
    differing = [name for name in files(plain) if not filecmp.cmp(plain / name, explicit / name, shallow=False)]
    assert "agentic-layer.json" not in differing
    assert not [name for name in differing if name.startswith(("adws/", "commands/", "hops/", "env/", "templates/"))], differing
    assert "agentic.rkt" in differing
    ir = json.loads((plain / "agentic-layer.json").read_text())
    (cmd,) = ir["codebase"]["commands"]
    assert (cmd["covers"], cmd["always"], cmd["reuse"]) == ([], True, True)
    assert all(step["whenAffected"] is False for adw in ir["adws"] for step in adw["steps"] if step["kind"] == "check")


def test_explain_says_what_a_check_covers_and_when_it_runs(app, tmp_path) -> None:
    layer = _fixture_layer(app)
    out = tmp_path / "explained.md"
    al("explain", layer, "--md", out)
    text = out.read_text()
    assert "- `suite-a` runs `sh a/test.sh`; covers `a` (only when affected)" in text
    assert "- `docs` runs `sh check-docs.sh`; covers `README.md` and `docs` (only when affected)" in text
    assert "- `flaky` runs `sh flaky.sh`; covers `a` (never reused)" in text
    assert "check `suite-a`: run the `suite-a` command, only when the change touches what it covers" in text
    assert ("the commands `suite-a`, `suite-b` and `docs` pass again on the final result "
            "(the gate runs only the required checks the change affects)") in text
    assert "→ suite-a → suite-b → docs → flaky →" in text      # the steps (check tests #:cmds affected) stands for


def test_explain_of_a_layer_without_the_clauses_says_nothing_new(sibling, tmp_path) -> None:
    out = tmp_path / "explained.md"
    al("explain", sibling, "--md", out)
    text = out.read_text()
    assert "- `test` runs `./test.sh` — the tests pass\n" in text
    assert "covers" not in text and "only when" not in text and "never reused" not in text


def test_verify_reports_undeclared_checks_and_uncovered_source_paths(app) -> None:
    layer = _sibling(app, COVER_LAYER)
    cp = al("verify", layer)
    lines = cp.stdout.splitlines()
    assert lines[-1].startswith("verify: 2/2 ok")            # the result stays the last line
    assert lines[-3:-1] == [
        "coverage: 1 check declares no #:covers and runs for every change: lint",
        "coverage: source path lib is covered by no check: a change there runs every check"]
    assert "coverage:" in al("verify", "--help").stdout


def test_verify_says_when_no_check_declares_coverage(sibling) -> None:
    cp = al("verify", sibling)
    lines = cp.stdout.splitlines()
    assert lines[-2] == "coverage: no check declares #:covers, so every check runs for every change"
    assert lines[-1].startswith("verify: 1/1 ok")


def test_verify_json_output_is_unchanged(app) -> None:
    layer = _sibling(app, COVER_LAYER)
    cp = al("verify", layer, "--json")
    rows = json.loads(cp.stdout)                               # stdout is the rows and nothing else
    assert [row["name"] for row in rows] == ["test", "lint"]
    assert set(rows[0]) == {"name", "cmd", "exit", "seconds", "log", "at", "workflow", "stack"}
    assert "coverage: 1 check declares no #:covers and runs for every change: lint" in cp.stderr.splitlines()
    # a layer generated before this feature has no affected module: verify prints no coverage line
    module = layer / "adws" / "adw_modules" / "affected.py"
    module.unlink()
    for later in ("prepare.py", "envsource.py"):               # modules a layer of that time did not have either
        (module.parent / later).unlink(missing_ok=True)
    shutil.rmtree(module.parent / "__pycache__", ignore_errors=True)
    for path in (layer / "adws" / "adw_modules").glob("*.py"):   # … and nothing that imports it
        text = path.read_text()
        if "affected" in text and path.name in ("steps.py", "runner.py"):
            path.write_text("\n".join(line for line in text.splitlines() if "affected" not in line) + "\n")
    old = al("verify", layer, "--cmd", "lint")
    assert "verify: 1/1 ok" in old.stdout and "coverage:" not in old.stdout + old.stderr


def test_sandbox_doctor_prints_the_coverage_lines(app, podman) -> None:  # noqa: F811
    source = COVER_LAYER.replace("  (command plan ", ENVIRONMENTS + "  (command plan ", 1).replace(
        '(adw change #:description "Plan, build, test; a person reviews"',
        '(adw change #:description "Plan, build, test; a person reviews" #:environment (worktree box)')
    layer = app.parent / "notes.al"
    layer.mkdir()
    (layer / "agentic.rkt").write_text(source.format(root="../notes", placement="(layer sibling)"))
    al("gen", layer)
    data = json.loads(podman["state"].read_text())
    rest = ("AL-DOCTOR\tcommand:lint\tok\t/usr/bin/true\nAL-DOCTOR\tgit\tok\trev-parse and status work in the worktree\n"
            "AL-DOCTOR\tinit\tok\tcatatonit\nAL-DOCTOR\tdisk\tok\t90000 MB free\n")
    data["doctor_output"] = "AL-DOCTOR\tcommand:test\tok\t./test.sh\n" + rest
    podman["state"].write_text(json.dumps(data))
    # the doctor also looks for the agent CLI on this machine; a stand-in on PATH keeps this test about
    # the coverage lines on a machine without one (continuous integration has none)
    agent_bin = app.parent / "agent-bin"
    agent_bin.mkdir()
    (agent_bin / "claude").write_text("#!/bin/sh\necho 0.0.0\n")
    (agent_bin / "claude").chmod(0o755)
    env = dict(podman["env"], PATH=f"{agent_bin}{os.pathsep}{podman['env'].get('PATH', os.environ['PATH'])}")
    cp = al("sandbox", layer, "doctor", env=env)
    lines = cp.stdout.splitlines()
    assert lines[-1].startswith("doctor: ") and "0 missed" in lines[-1]     # the summary stays the last line
    assert lines[-3:-1] == [
        "coverage: 1 check declares no #:covers and runs for every change: lint",
        "coverage: source path lib is covered by no check: a change there runs every check"]
    # the exit code is the doctor's alone: a miss still fails it, with the coverage lines printed
    data["doctor_output"] = "AL-DOCTOR\tcommand:test\tmiss\t./test.sh is not an executable file\n" + rest
    podman["state"].write_text(json.dumps(data))
    missed = al("sandbox", layer, "doctor", env=env, ok=False)
    assert missed.returncode == 1 and "1 missed" in missed.stdout.splitlines()[-1]
    assert missed.stdout.splitlines()[-2].startswith("coverage: source path lib")
