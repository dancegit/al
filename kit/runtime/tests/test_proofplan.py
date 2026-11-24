"""The plan's Proof plan table (proofplan.py): parsing, which cmd proves a row, the provability check
(proven-here, proven-on:ENV, needs-declaration, needs-person, unprovable) and the run-scoped proof
folder whose files all reach the reviewers."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import convergence_fixtures as cf  # noqa: E402

from adw_modules import approvals, checkenv, ir, proofplan, steps  # noqa: E402
from adw_modules.errors import StepFailed  # noqa: E402
from adw_modules.state import ADWState  # noqa: E402

RUN = "b0001234"
PLAN = """# Feature: x

### Proof plan
| # | Criterion | Proven by | Writes | Needs |
|---|---|---|---|---|
| C1 | the page shows it | `unit` `tests/test_page.py::test_shows` `tests/test_page.py::test_other` | shots/*.png (30), proof:report.json, proof:clips/*.webm (2) | tool:chromium, approval:vocabulary |
| C2 | it is fast | `test -f feature.py && echo ok` | - | none |

## What could go wrong
| not | a | proof | table |
"""


@pytest.fixture(autouse=True)
def no_host_id(monkeypatch):
    monkeypatch.delenv("AL_HOST_ID", raising=False)


def _stack(cwd: Path, podman: dict | None = None, env: dict | None = None):
    return SimpleNamespace(podman_option=podman, cwd=cwd, sandbox=None, read_only=False,
                           wrap=lambda cmd: ["bash", "-c", cmd], process_env=lambda: dict(env or {}))


def _layer(make_layer, **options):
    return ir.load(cf.converge_layer(make_layer, **options))


def _row(**fields):
    base = {"id": "C1", "criterion": "it works", "command": "unit", "tests": [], "writes": [], "proof": [],
            "needs": []}
    return {**base, **fields}


def _evaluate(layer, row, stack=None, state=None):
    state = state or ADWState(adw_id=RUN, adw=cf.ADW)
    return proofplan.evaluate(layer, state, layer.adw(cf.ADW), stack or _stack(Path(".")), row)


# ---------------------------------------------------------------- parsing

def test_parse_table_rows_tests_writes_proof_counts_needs():
    parsed = proofplan.parse(PLAN)
    assert parsed["table"] == "parsed"
    first, second = parsed["rows"]
    assert first == {"id": "C1", "criterion": "the page shows it", "command": "unit",
                     "tests": ["tests/test_page.py::test_shows", "tests/test_page.py::test_other"],
                     "writes": [{"glob": "shots/*.png", "count": 30}],
                     "proof": [{"glob": "report.json", "count": 1}, {"glob": "clips/*.webm", "count": 2}],
                     "needs": ["tool:chromium", "approval:vocabulary"]}
    assert second["command"] == "test -f feature.py && echo ok"
    assert (second["tests"], second["writes"], second["proof"], second["needs"]) == ([], [], [], [])
    assert "invalid" not in first and "invalid" not in second


def test_parse_absent_table():
    assert proofplan.parse("# Plan\n\n| a | b |\n|---|---|\n| 1 | 2 |\n") == {"table": "absent"}
    assert proofplan.parse("") == {"table": "absent"}
    header_only = "| # | Criterion | Proven by | Writes | Needs |\n|---|---|---|---|---|\n\nprose"
    assert proofplan.parse(header_only) == {"table": "invalid", "rows": []}


def test_invalid_rows_are_kept_with_why():
    text = cf.table([("X1", "", "no backticks", "-", "teleport, approval:Bad Word"),
                     ("C3", "fine", "`unit`", "-", "network")])
    bad, good = proofplan.parse(text)["rows"]
    assert bad["invalid"] == ("the id 'X1' is not C<n>; the criterion is empty; Proven by names no command in "
                              "backticks; unknown need teleport, Word, approval:Bad")
    assert "invalid" not in good


# ---------------------------------------------------------------- what proves a row

def test_command_resolved_by_check_name_cmd_name_and_run_string(make_layer):
    layer_dir = cf.converge_layer(make_layer)
    data = json.loads((layer_dir / "agentic-layer.json").read_text())
    data["codebase"]["commands"].append({"name": "lint", "run": "ruff check .", "timeout": 30})
    data["adws"][0]["steps"][2] = {"name": "unit-tests", "kind": "check", "cmd": "unit"}
    (layer_dir / "agentic-layer.json").write_text(json.dumps(data))
    layer = ir.load(layer_dir)
    adw = layer.adw(cf.ADW)
    by_step = proofplan.resolve_command(layer, adw, "unit-tests")
    by_cmd = proofplan.resolve_command(layer, adw, "unit")
    by_run = proofplan.resolve_command(layer, adw, cf.UNIT_OK)
    assert by_step[0]["name"] == by_cmd[0]["name"] == by_run[0]["name"] == "unit-tests"
    assert by_step[1]["name"] == by_cmd[1]["name"] == by_run[1]["name"] == "unit"
    no_step = proofplan.resolve_command(layer, adw, "ruff check .")
    assert no_step[0] is None and no_step[1]["name"] == "lint"
    assert proofplan.resolve_command(layer, adw, "make proof") == (None, None)


PYTEST_UNIT = "(cd kit/runtime && env -u AL_APP_ROOT uv run --with pytest pytest -q)"


def test_a_narrowed_declared_cmd_is_proven_by_it_with_the_selection(make_layer):
    layer = _layer(make_layer, unit=PYTEST_UNIT)
    prefix = "(cd kit/runtime && env -u AL_APP_ROOT uv run --with pytest pytest"
    for text, selection in ((f"{prefix} -q tests/test_x.py)", ["-q", "tests/test_x.py"]),
                            (f"{prefix} -q -rA tests/test_y.py -k some_case)",
                             ["-q", "-rA", "tests/test_y.py", "-k", "some_case"])):
        result = _evaluate(layer, _row(command=text))
        assert result["outcome"] == "proven-here", (text, result)
        assert result["cmd"] == "unit" and result["selection"] == selection
        check, cmd = proofplan.resolve_command(layer, layer.adw(cf.ADW), text)
        assert check["name"] == "unit" and cmd["name"] == "unit" and cmd["run"] == text


def test_a_narrowed_raco_cmd_is_proven_by_it(make_layer):
    unit = 'PLTCOLLECTS="$PWD/racket:" raco test racket/demo/tests/core-test.rkt'
    layer = _layer(make_layer, unit=unit)
    other = _evaluate(layer, _row(command='PLTCOLLECTS="$PWD/racket:" raco test racket/demo/tests/other-test.rkt'))
    assert other["outcome"] == "proven-here" and other["selection"] == ["racket/demo/tests/other-test.rkt"]
    elsewhere = _evaluate(layer, _row(command='PLTCOLLECTS="$PWD/racket:" raco test racket/elsewhere/x-test.rkt'))
    assert elsewhere["outcome"] == "needs-declaration"


def test_another_program_directory_or_shell_stays_needs_declaration(make_layer):
    layer = _layer(make_layer, unit=PYTEST_UNIT)
    prefix = "(cd kit/runtime && env -u AL_APP_ROOT uv run --with pytest pytest"
    for text in ("(cd kit/dashboard && env -u AL_APP_ROOT uv run --with pytest pytest -q tests/test_x.py)",
                 "(cd kit/runtime && env -u AL_APP_ROOT uv run --with pytest python -m pytest -q)",
                 f"{prefix} -q ../other/tests)", f"{prefix} -q /tmp/tests)",
                 f"{prefix} -q tests/test_x.py; touch x)", f'{prefix} -q -k "$(id)")', "make proof"):
        result = _evaluate(layer, _row(command=text))
        assert (result["outcome"], result["party"]) == ("needs-declaration", "layer author"), text


def test_a_line_break_is_never_a_narrowed_command(make_layer):
    assert proofplan.narrowed("uv run pytest -q", "uv run pytest -q\ntouch pwned") is None
    assert proofplan.narrowed("uv run pytest -q", "uv run pytest -q\rtouch pwned") is None
    assert proofplan.narrowed(PYTEST_UNIT, PYTEST_UNIT[:-1] + " tests/test_x.py\ntouch x)") is None
    layer = _layer(make_layer, unit=PYTEST_UNIT)
    prefix = "(cd kit/runtime && env -u AL_APP_ROOT uv run --with pytest pytest"
    result = _evaluate(layer, _row(command=f"{prefix} -q tests/test_x.py\ntouch x)"))
    assert result["outcome"] == "needs-declaration"
    assert proofplan.narrowed("uv run pytest -q", "uv run pytest -q tests/x") == ["-q", "tests/x"]


BUN = "systemd-run --user --scope --quiet --slice=demo.slice bun"


def _bun_layer(make_layer):
    return _layer(make_layer, commands=[{"name": "unit", "run": f"{BUN} run test", "timeout": 30,
                                         "targeted": f"{BUN} test {{tests}}"}])


def test_a_bun_targeted_form_row_is_proven_by_its_cmd(make_layer):
    layer = _bun_layer(make_layer)
    for text, selection in (
            (f"{BUN} test tests/console.test.ts", ["tests/console.test.ts"]),
            (f'{BUN} test -t "lists strategies" tests/console.test.ts tests/other.spec.ts',
             ["-t", "lists strategies", "tests/console.test.ts", "tests/other.spec.ts"])):
        result = _evaluate(layer, _row(command=text))
        assert result["outcome"] == "proven-here", (text, result)
        assert result["cmd"] == "unit" and result["selection"] == selection
        check, cmd = proofplan.resolve_command(layer, layer.adw(cf.ADW), text)
        assert check["name"] == "unit" and cmd["name"] == "unit" and cmd["run"] == text
        assert cmd["selection"] == selection


def test_a_pytest_targeted_form_row_is_proven_by_its_cmd(make_layer):
    layer = _layer(make_layer, commands=[{
        "name": "unit", "run": "make test", "timeout": 30, "testRoot": "kit/runtime",
        "targeted": "(cd kit/runtime && uv run --with pytest pytest -q {tests})"}])
    text = "(cd kit/runtime && uv run --with pytest pytest -q -rA tests/test_x.py::test_a)"
    result = _evaluate(layer, _row(command=text))
    assert result["outcome"] == "proven-here", result
    assert result["cmd"] == "unit" and result["selection"] == ["-q", "-rA", "tests/test_x.py::test_a"]
    check, cmd = proofplan.resolve_command(layer, layer.adw(cf.ADW), text)
    assert check["name"] == "unit" and cmd["name"] == "unit" and cmd["run"] == text


def test_a_targeted_form_row_with_anything_else_stays_needs_declaration(make_layer):
    layer = _bun_layer(make_layer)
    for text in (f"{BUN} test --coverage tests/console.test.ts",
                 f"{BUN} test ../tests/console.test.ts",
                 f"{BUN} test /tmp/console.test.ts",
                 f"{BUN} test src/console.ts",
                 f"{BUN} test",
                 f"{BUN} test tests/console.test.ts; touch x",
                 f'{BUN} test tests/console.test.ts -t "$(id)"',
                 f"{BUN.replace('demo.slice', 'other.slice')} test tests/console.test.ts",
                 "make proof"):
        result = _evaluate(layer, _row(command=text))
        assert (result["outcome"], result["party"]) == ("needs-declaration", "layer author"), text
    assert proofplan.narrowed_form("bun test {tests} {files}", "bun test a.test.ts") is None
    assert proofplan.narrowed_form('sh -c "bun test {tests}"', 'sh -c "bun test a.test.ts"') is None


# ---------------------------------------------------------------- outcomes

def test_outcome_proven_here(make_layer):
    layer = _layer(make_layer)
    result = _evaluate(layer, _row())
    assert (result["outcome"], result["party"], result["why"]) == ("proven-here", "", "")


def test_outcome_proven_on_check_environment(make_layer, monkeypatch):
    layer_dir = cf.converge_layer(make_layer)
    data = json.loads((layer_dir / "agentic-layer.json").read_text())
    data["adws"][0]["steps"][2]["in"] = "box"
    data["environments"].append({"id": "box", "kind": "podman", "status": "stable", "network": "slirp"})
    (layer_dir / "agentic-layer.json").write_text(json.dumps(data))
    layer = ir.load(layer_dir)
    box = _stack(Path("."), podman={"id": "box", "network": "slirp"})
    monkeypatch.setattr(checkenv, "stack_for", lambda layer, state, step, primary: box)
    monkeypatch.setattr(proofplan, "which", lambda stack, name: stack is box)
    assert _evaluate(layer, _row(needs=["tool:node", "network"]))["outcome"] == "proven-on:box"


def test_needs_declaration_missing_artefact_glob_prints_declaration(make_layer):
    layer = _layer(make_layer, artefacts=[{"id": "shots", "kind": "image", "glob": "shots/*.png", "maxFiles": 40,
                                           "status": "stable"}], review_artefacts=["shots"])
    assert _evaluate(layer, _row(writes=[{"glob": "shots/*.png", "count": 30}]))["outcome"] == "proven-here"
    result = _evaluate(layer, _row(writes=[{"glob": "out/report.html", "count": 1}]))
    assert (result["outcome"], result["party"]) == ("needs-declaration", "layer author")
    assert result["why"] == "out/report.html matches no artefact the reviewers are given"
    assert result["fix"] == ('(artefact html #:id out #:glob "out/report.html") and add out to #:artefacts of '
                             "(review … #:id policy), or write it under proof: (proof:report.html)")


def test_needs_declaration_image_limit_below_count(make_layer):
    layer = _layer(make_layer, artefacts=[{"id": "shots", "kind": "image", "glob": "shots/*.png", "maxFiles": 12,
                                           "status": "stable"}], review_artefacts=["shots"])
    result = _evaluate(layer, _row(writes=[{"glob": "shots/*.png", "count": 30}]))
    assert result["outcome"] == "needs-declaration"
    assert result["fix"] == ("#:max-files 30 on (artefact image #:id shots …), or write them under proof: "
                             "(proof:*.png)")


def test_needs_declaration_missing_package_in_podman(make_layer, monkeypatch):
    layer = _layer(make_layer)
    monkeypatch.setattr(proofplan, "which", lambda stack, name: False)
    box = _stack(Path("."), podman={"id": "box", "network": "none"})
    result = _evaluate(layer, _row(needs=["tool:chromium"]), stack=box)
    assert (result["outcome"], result["party"]) == ("needs-declaration", "layer author")
    assert result["fix"] == "add the package that provides `chromium` to (environment podman #:id box #:packages …)"
    network = _evaluate(layer, _row(needs=["network"]), stack=box)
    assert network["fix"] == "#:network slirp on (environment podman #:id box …)"
    engine = _evaluate(layer, _row(needs=["engine:podman"]), stack=box)
    assert engine["fix"] == "#:nested-engine #t on (environment podman #:id box …)"


def test_needs_person_unapproved_and_pre_approved_passes(make_layer):
    layer = _layer(make_layer)
    state = ADWState(adw_id=RUN, adw=cf.ADW)
    result = _evaluate(layer, _row(needs=["approval:edit:docs/DESIGN.md"]), state=state)
    assert (result["outcome"], result["party"], result["approvals"]) == ("needs-person", "approver",
                                                                         ["edit:docs/DESIGN.md"])
    assert result["question"] == f"may this run edit:docs/DESIGN.md? answer: al approve LAYER {RUN} edit:docs/DESIGN.md"
    approvals.record(state, ["edit:docs/*"], "ana", "start")
    assert _evaluate(layer, _row(needs=["approval:edit:docs/DESIGN.md"]), state=state)["outcome"] == "proven-here"
    secret = _evaluate(layer, _row(needs=["secret:API_TOKEN"]), state=state)
    assert (secret["outcome"], secret["party"], secret["question"]) == ("needs-person", "approver",
                                                                        "provide secret API_TOKEN")
    present = _evaluate(layer, _row(needs=["secret:API_TOKEN"]), stack=_stack(Path("."), env={"API_TOKEN": "x"}))
    assert present["outcome"] == "proven-here"


def test_unprovable_host_and_missing_tool_on_host(make_layer, monkeypatch):
    layer = _layer(make_layer)
    host = _evaluate(layer, _row(needs=["host:gpu-1"]))
    assert (host["outcome"], host["party"]) == ("unprovable", "operator")
    assert host["fix"] == "run this task on a machine provisioned as gpu-1 (al deploy --as gpu-1)"
    monkeypatch.setenv("AL_HOST_ID", "gpu-1")
    assert _evaluate(layer, _row(needs=["host:gpu-1"]))["outcome"] == "proven-here"
    tool = _evaluate(layer, _row(needs=["tool:al-no-such-tool"]))
    assert (tool["outcome"], tool["party"]) == ("unprovable", "operator")
    assert _evaluate(layer, _row(needs=["tool:sh"]))["outcome"] == "proven-here"
    malformed = _evaluate(layer, _row(id="X", invalid="the id 'X' is not C<n>"))
    assert (malformed["outcome"], malformed["party"]) == ("unprovable", "agent")
    unresolved = _evaluate(layer, _row(command="make proof"))
    assert (unresolved["outcome"], unresolved["party"]) == ("needs-declaration", "layer author")
    assert unresolved["fix"] == '(cmd make-proof "make proof") and (check make-proof #:cmd make-proof) in (adw converge …)'


def test_check_stops_with_the_table_and_the_first_party(make_layer, app_repo, tmp_path):
    layer = _layer(make_layer)
    plan = tmp_path / "plan.md"
    plan.write_text(cf.table([("C1", "a", "`unit`", "-", "approval:vocabulary"),
                              ("C2", "b", "`make proof`", "-", "none")]))
    state = ADWState(adw_id=RUN, adw=cf.ADW, plan=str(plan), worktree=str(app_repo))
    with pytest.raises(StepFailed) as stop:
        proofplan.check(layer, state, layer.adw(cf.ADW), _stack(app_repo))
    lines = stop.value.reason.splitlines()
    assert lines[0] == "proof:needs-declaration: 2 criterion(s) cannot be proven here; party: layer author"
    assert lines[1].startswith("C1 | needs-person | vocabulary is a decision only a person may take | may this run")
    assert lines[2].startswith("C2 | needs-declaration | `make proof` is no cmd or check of the layer | (cmd make-proof")
    stopped = state.proof_plan["stopped"]
    assert (stopped["at"], stopped["party"], stopped["approvals"]) == ("prove", "layer author", ["vocabulary"])
    assert [row["state"] for row in state.proof_plan["rows"]] == ["planned", "planned"]


# ---------------------------------------------------------------- the run-scoped proof

def test_proof_dir_is_git_ignored_and_collected_in_full(make_layer, app_repo):
    layer = _layer(make_layer)
    state = ADWState(adw_id=RUN, adw=cf.ADW, worktree=str(app_repo))
    state.proof_plan = {"table": "parsed", "rows": [_row(proof=[{"glob": "shots/*.png", "count": 30}]),
                                                    _row(id="C2", proof=[{"glob": "report.json", "count": 1}])]}
    folder = proofplan.ensure_dir(state, layer)
    assert folder == app_repo / ".al-proof" and (folder / ".gitignore").read_text() == "*\n"
    (folder / "shots").mkdir()
    for index in range(30):
        (folder / "shots" / f"screen-{index:02d}.png").write_bytes(b"png%d" % index)
    (folder / "report.json").write_text("{}")
    (folder / "stray.txt").write_text("not declared")
    assert subprocess.run(["git", "-C", str(app_repo), "status", "--porcelain"], capture_output=True,
                          text=True, check=True).stdout == ""
    collected = proofplan.collect_proof(layer, state, "review")
    assert len([item for item in collected if item["id"] == "proof-image"]) == 30
    manifest = json.loads((Path(layer.layer_dir) / "artefacts" / RUN / "manifest.json").read_text())
    images = [entry for entry in manifest if entry["id"] == "proof-image"]
    assert len(images) == 30 and {entry["type"] for entry in images} == {"image"}
    assert [entry["path"] for entry in manifest if entry["id"] == "proof-file"] == [
        f"artefacts/{RUN}/proof-file/report.json"]
    assert not [entry for entry in manifest if "stray" in entry["path"]]


def test_plan_without_table_records_absent_and_no_step(make_layer, tmp_path):
    layer = _layer(make_layer)
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\nNo table here.\n")
    state = ADWState(adw_id=RUN, adw=cf.ADW, plan=str(plan))
    steps._prove(layer, state, layer.adw(cf.ADW), _stack(tmp_path))
    assert state.proof_plan == {"plan": str(plan), "table": "absent"}
    assert state.steps == []
