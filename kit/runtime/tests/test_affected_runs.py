"""Affected checks and reused results through whole runs (runner.main, mock agent): the IR has the
shape of racket/al/tests/fixtures/affected-checks.rkt. Every cmd appends its name to a counter file
outside the worktree, so a test counts how often a command really ran.

With AL_AFFECTED_PROOF=DIR the summaries, the report and the decision events of three runs are
written to DIR (what the guide quotes); nothing is written anywhere else."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from adw_modules import affected, events, ir, state as state_module
from adw_modules.runner import main, summary_lines

TEXT = {"description": "", "file": "", "focus": [], "fabric": [], "relevant": [], "validation": []}
NO_COVERED = "no covered path changed"
PROOF = os.environ.get("AL_AFFECTED_PROOF", "").strip()


@pytest.fixture(autouse=True)
def no_settings_of_the_person(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for key in ("AL_DASHBOARD_URL", "ADW_FANOUT_PARENT", "AL_MODEL_SET", "AL_MODELS", "AL_MAX_COST"):
        monkeypatch.delenv(key, raising=False)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.org", *args],
                          check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def _cmd(counter: Path, name: str, covers: list[str], *, always: bool = False, reuse: bool = True,
         then: str = "true") -> dict:
    return {"name": name, "run": f"echo {name} >> {counter}; {then}", "timeout": 20, "description": "",
            "covers": covers, "always": always, "reuse": reuse}


def _check(name: str, **extra) -> dict:
    return {"name": name, "kind": "check", "cmd": name, "resolve": "", "retries": 0, "collect": [], "in": "",
            "maxSkipped": -1, "forbidSkipReason": "", "whenAffected": True, **extra}


def _layer(make_layer, tmp_path: Path, app_repo: Path, *, review: dict | None = None, steps: list[dict] | None = None,
           commands: list[dict] | None = None, extra: dict | None = None, keep: str = "never") -> Path:
    """zte: plan, build, the three affected checks, commit, review (patch + implement), ship."""
    counter = tmp_path / "ran.log"
    # main is not checked out in the app, so a merge really lands on it (shipped=True)
    _git(app_repo, "checkout", "-q", "--detach")
    return make_layer({
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "commands": [{"name": "plan", "template": "plan", "modelSet": "base", "output": "path", **TEXT},
                     {"name": "build", "template": "build", "modelSet": "base", "output": "text", **TEXT},
                     {"name": "commit", "template": "commit", "modelSet": "base", "output": "text", **TEXT},
                     {"name": "patch", "template": "patch", "modelSet": "base", "output": "path", **TEXT},
                     {"name": "implement", "template": "implement", "modelSet": "base", "output": "text", **TEXT},
                     {"name": "review", "template": "review", "modelSet": "base", "output": "json", **TEXT}],
        "codebase": {"commands": commands or [
            _cmd(counter, "suite-a", ["a"]), _cmd(counter, "suite-b", ["b"]),
            _cmd(counter, "docs", ["README.md", "docs"]),
            _cmd(counter, "flaky", ["a"], always=True, reuse=False)],
            "paths": {"source": ["a", "b"], "docs": ["README.md", "docs"], "protected": []}},
        "reviews": [{"id": "zero-touch", "kind": "auto", "status": "stable", "command": "review", "reviewers": 1,
                     "maxRetries": 1, "artefacts": [], "allowedClasses": ["chore", "bug"], "blockProtectedPaths": True,
                     "maxDiffLines": 100, "requireChecks": ["suite-a", "suite-b", "docs"], "require": "affected",
                     "ship": "merge", "deployCmd": "", **(review or {})}],
        "environments": [{"id": "worktree", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 27300,
                          "portsPerRun": 0, "keep": keep, "status": "stable"}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "adws": [{"name": "zte", "agent": "mock", "review": "zero-touch", "environment": ["worktree"],
                  "classes": ["chore", "bug"], "steps": steps or [
                      {"name": "plan", "kind": "run", "command": "plan"},
                      {"name": "build", "kind": "run", "command": "build"},
                      _check("suite-a"), _check("suite-b"), _check("docs"),
                      {"name": "commit", "kind": "run", "command": "commit"},
                      {"name": "review", "kind": "review", "patch": "patch", "implement": "implement"},
                      {"name": "ship", "kind": "ship"}]}],
        **(extra or {})})


def _script(layer: Path, name: str, data: dict) -> None:
    path = layer / "mock" / "zte" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _build(layer: Path, files: dict[str, str]) -> None:
    _script(layer, "build", {"files": files})
    _script(layer, "commit", {"commit": True})


def _state(layer: Path, adw_id: str) -> dict:
    return json.loads((layer / "agents" / adw_id / "state.json").read_text())


def _ran(tmp_path: Path) -> list[str]:
    counter = tmp_path / "ran.log"
    return counter.read_text().split() if counter.exists() else []


def _step(state: dict, name: str) -> dict:
    return next(record for record in state["steps"] if record["name"] == name)


def _ledger(state: dict, **where) -> list[dict]:
    return [entry for entry in state.get("check_results", [])
            if all(entry.get(key) == value for key, value in where.items())]


def _decisions(layer: Path, adw_id: str) -> list[dict]:
    return [e for e in events.read(layer / "agents" / adw_id / "events.jsonl") if e["type"] == "check.decision"]


def _summary(layer: Path, adw_id: str) -> list[str]:
    return summary_lines(ir.load(layer), state_module.load(layer, adw_id))


def _proof(name: str, text: str, tmp_path: Path) -> None:
    """Step 8 of the plan: with AL_AFFECTED_PROOF set, keep what a person would read."""
    if PROOF:
        Path(PROOF).mkdir(parents=True, exist_ok=True)
        (Path(PROOF) / name).write_text(text.replace(str(tmp_path), "<tmp>"), encoding="utf-8")


# ---------------------------------------------------------------- the acceptance fixture

def test_change_in_one_directory_runs_one_suite(make_layer, app_repo, tmp_path):
    layer = _layer(make_layer, tmp_path, app_repo)
    _build(layer, {"a/x.txt": "x\n"})
    assert main("zte", layer, ["Change a", "--adw-id", "afec0001"]) == 0
    state = _state(layer, "afec0001")
    assert _ran(tmp_path) == ["suite-a"]                       # once: not in the check steps, not at the gate
    ran = state["checks"]["suite-a"]
    assert (ran["exit"], ran["outcome"], ran["log"]) == (0, "passed", "agents/afec0001/checks/suite-a.1.log")
    assert len(ran["tree"]) == 40 and _step(state, "suite-a")["status"] == "passed"
    check_dir = layer / "agents" / "afec0001" / "checks"
    for name in ("suite-b", "docs"):
        record, step = state["checks"][name], _step(state, name)
        assert step["status"] == "not-affected", name
        assert step["detail"] == f"not affected: {NO_COVERED} (1 paths compared)"
        assert (record["outcome"], record["why"], record["paths"], record["paths_total"]) == (
            "not-affected", NO_COVERED, ["a/x.txt"], 1)
        assert record["tree"] == ran["tree"] and record["log"] == f"agents/afec0001/checks/{name}.not-affected.log"
        assert not list(check_dir.glob(f"{name}.[0-9]*.log"))   # no numbered log: the command never started
        log = (layer / record["log"]).read_text()
        assert log.startswith(f"al: check {name} not run: not affected ({NO_COVERED}; 1 paths compared)\n")
        assert "al:   a/x.txt" in log and f"al: tree {ran['tree']}" in log
        (entry,) = _ledger(state, check=name, phase="check")
        assert (entry["outcome"], entry["why"], entry["paths"], entry["exit"]) == ("not-affected", NO_COVERED, ["a/x.txt"], None)
    decisions = _decisions(layer, "afec0001")
    assert [(e["phase"], e["cmd"], e["outcome"]) for e in decisions] == [
        ("check", "suite-b", "not-affected"), ("check", "docs", "not-affected"),
        ("gate", "suite-a", "reused"), ("gate", "suite-b", "not-affected"), ("gate", "docs", "not-affected")]
    assert decisions[0]["paths"] == ["a/x.txt"] and decisions[0]["why"] == NO_COVERED and decisions[0]["owner"] == "suite-b"
    # no reader shows a check that was not run as passed
    all_events = events.read(layer / "agents" / "afec0001" / "events.jsonl")
    assert {e["status"] for e in all_events if e["type"] == "step.end" and e["step"] in ("suite-b", "docs")} == {"not-affected"}
    assert not [e for e in all_events if e["type"] in ("check.start", "check.end") and e["cmd"] != "suite-a"]
    lines = _summary(layer, "afec0001")
    checks_line = next(line for line in lines if line.startswith("checks:"))
    assert "suite-a=0" in checks_line and f"suite-b=not affected ({NO_COVERED}; 1 paths compared)" in checks_line
    assert "suite-b=0" not in checks_line and "docs=0" not in checks_line
    assert state["status"] == "passed" and state["ship"]["shipped"] is True
    _proof("one-suite-summary.txt", "\n".join(lines) + "\n", tmp_path)
    _proof("covered-path-events.jsonl", "".join(json.dumps(e, sort_keys=True) + "\n" for e in decisions), tmp_path)


def test_change_outside_both_runs_both(make_layer, app_repo, tmp_path):
    layer = _layer(make_layer, tmp_path, app_repo)
    _build(layer, {"other/x.txt": "x\n"})
    assert main("zte", layer, ["Change something else", "--adw-id", "afec0002"]) == 0
    state = _state(layer, "afec0002")
    assert _ran(tmp_path) == ["suite-a", "suite-b", "docs"]      # every check, once; the gate reuses all three
    why = "other/x.txt is covered by no check"
    for name in ("suite-a", "suite-b", "docs"):
        assert _step(state, name)["status"] == "passed" and state["checks"][name]["outcome"] == "passed"
        (entry,) = _ledger(state, check=name, phase="check")
        assert entry["outcome"] == "passed" and entry["why"] == why
    assert not _ledger(state, outcome="not-affected")
    assert [e["outcome"] for e in _ledger(state, phase="gate")] == ["reused"] * 3


def test_file_declared_by_two_suites_runs_everything(make_layer, app_repo, tmp_path):
    counter = tmp_path / "ran.log"
    layer = _layer(make_layer, tmp_path, app_repo, commands=[
        _cmd(counter, "suite-a", ["a", "shared"]), _cmd(counter, "suite-b", ["b", "shared"]),
        _cmd(counter, "docs", ["README.md", "docs"])])
    _build(layer, {"shared/util.txt": "x\n"})
    assert main("zte", layer, ["Change what both declare", "--adw-id", "afec0003"]) == 0
    state = _state(layer, "afec0003")
    assert _ran(tmp_path) == ["suite-a", "suite-b", "docs"]
    why = "shared/util.txt is declared by several checks (suite-a, suite-b)"
    assert {entry["why"] for entry in _ledger(state, phase="check")} == {why}
    assert not _ledger(state, outcome="not-affected")


def test_layer_change_runs_everything(make_layer, app_repo, tmp_path):
    """An in-repo layer: a file under the layer directory changes how agents work, so nothing is
    selected away, although no check's #:covers names that directory."""
    layer = _layer(make_layer, tmp_path, app_repo, extra={"layer": {"id": "in-repo", "kind": "in-repo",
                                                                    "status": "stable", "dir": "agentic"}},
                   steps=[{"name": "build", "kind": "run", "command": "build"},
                          _check("suite-a"), _check("suite-b"), _check("docs")])
    _script(layer, "build", {"files": {"agentic/commands/plan.md": "# plan\n", "a/x.txt": "x\n"}})
    assert main("zte", layer, ["Change the layer", "--adw-id", "afec0004"]) == 0
    state = _state(layer, "afec0004")
    assert _ran(tmp_path) == ["suite-a", "suite-b", "docs"]
    assert {entry["why"] for entry in _ledger(state, phase="check")} == {
        "the change touches the agentic layer (agentic/commands/plan.md)"}
    assert {_step(state, name)["status"] for name in ("suite-a", "suite-b", "docs")} == {"passed"}


def test_readme_only_change_runs_no_code_suite_and_still_ships(make_layer, app_repo, tmp_path):
    layer = _layer(make_layer, tmp_path, app_repo)
    _build(layer, {"README.md": "# The app\n\nReorganised.\n"})
    before = _git(app_repo, "rev-parse", "main")
    assert main("zte", layer, ["Reorganise the README", "--adw-id", "afec0005"]) == 0
    state = _state(layer, "afec0005")
    assert _ran(tmp_path) == ["docs"]                            # the cheap check, once; no code suite at all
    assert _step(state, "docs")["status"] == "passed"
    assert {_step(state, name)["status"] for name in ("suite-a", "suite-b")} == {"not-affected"}
    gate = {entry["check"]: entry for entry in _ledger(state, phase="gate")}
    assert {name: entry["outcome"] for name, entry in gate.items()} == {
        "suite-a": "not-affected", "suite-b": "not-affected", "docs": "reused"}
    assert gate["docs"]["from"] == {"step": "docs", "phase": "check", "log": "agents/afec0005/checks/docs.1.log"}
    for name in ("suite-a", "suite-b"):
        assert gate[name]["paths"] == ["README.md"] and gate[name]["why"] == NO_COVERED
        gate_log = (layer / "agents" / "afec0005" / "checks" / f"gate-{name}.log").read_text()
        assert gate_log.startswith(f"al: check {name} not run: not affected") and "al:   README.md" in gate_log
    # the tree the gate judged is the tree that landed on main
    shipped_tree = _git(app_repo, "rev-parse", "main^{tree}")
    assert {entry["tree"] for entry in gate.values()} == {shipped_tree}
    assert state["ship"]["gate"]["ship"] is True and state["ship"]["gate"]["reasons"] == []
    assert state["ship"]["shipped"] is True and state["status"] == "passed"
    assert _git(app_repo, "rev-parse", "main") != before
    assert "Reorganised." in _git(app_repo, "show", "main:README.md")
    lines = _summary(layer, "afec0005")
    gate_line = next(line for line in lines if line.startswith("gate:"))
    assert f"docs=reused from docs, same tree {shipped_tree[:8]}" in gate_line
    assert f"suite-a=not affected ({NO_COVERED}; 1 paths compared)" in gate_line
    _proof("readme-only-summary.txt", "\n".join(lines) + "\n", tmp_path)
    # merged without a forge there is no pull request text; this is the section a PR would carry
    _proof("readme-only-PR.md", affected.report_markdown(ir.load(layer), state_module.load(layer, "afec0005")), tmp_path)


# ---------------------------------------------------------------- reuse

REFUSED = {"success": False, "summary": "the README does not mention it",
           "issues": [{"severity": "blocker", "description": "undocumented", "resolution": "document it"}]}
APPROVED = {"success": True, "summary": "looks right", "issues": []}


def test_covered_path_runs_its_suite_once_not_three_times(make_layer, app_repo, tmp_path):
    """No patch round: the suite runs in its check step and never again. The gate, which requires
    everything here, takes the pass from the check step because the tree is the same."""
    layer = _layer(make_layer, tmp_path, app_repo, review={"require": "all"})
    _build(layer, {"a/x.txt": "x\n"})
    assert main("zte", layer, ["Change a", "--adw-id", "afec0006"]) == 0
    state = _state(layer, "afec0006")
    assert _ran(tmp_path).count("suite-a") == 1
    (gate_a,) = _ledger(state, check="suite-a", phase="gate")
    tree = state["checks"]["suite-a"]["tree"]
    assert gate_a["outcome"] == "reused" and gate_a["tree"] == tree
    assert gate_a["from"] == {"step": "suite-a", "phase": "check", "log": "agents/afec0006/checks/suite-a.1.log"}
    assert affected.summary_text(gate_a) == f"reused from suite-a, same tree {tree[:8]}"
    gate_log = (layer / "agents" / "afec0006" / "checks" / "gate-suite-a.log").read_text()
    assert gate_log.startswith(f"al: check suite-a not run: reused from suite-a, same tree {tree[:8]}\n")
    # the two the check steps did not run have no pass on the final tree: the gate runs them
    assert _ran(tmp_path) == ["suite-a", "suite-b", "docs"]
    assert [(e["check"], e["outcome"]) for e in _ledger(state, phase="gate")] == [
        ("suite-a", "reused"), ("suite-b", "passed"), ("docs", "passed")]
    assert state["ship"]["shipped"] is True


def test_review_round_that_leaves_the_suite_s_paths_alone_does_not_run_it_again(make_layer, app_repo, tmp_path):
    """One review round whose patch edits README.md only: check step and round together run suite-a
    once. The gate (require all) runs it again, and only because the tree is no longer the one it
    passed on."""
    layer = _layer(make_layer, tmp_path, app_repo, review={"require": "all"})
    _build(layer, {"a/x.txt": "x\n"})
    _script(layer, "review-1", {"output": json.dumps(REFUSED)})
    _script(layer, "review-2-1", {"output": json.dumps(APPROVED)})
    _script(layer, "implement-1", {"files": {"README.md": "# The app\n\nNow documented.\n"}, "commit": True})
    assert main("zte", layer, ["Change a", "--adw-id", "afec0007"]) == 0
    state = _state(layer, "afec0007")
    before_gate = [e for e in state["check_results"] if e["phase"] != "gate"]
    assert [(e["check"], e["phase"], e["outcome"]) for e in before_gate] == [
        ("suite-a", "check", "passed"), ("suite-b", "check", "not-affected"), ("docs", "check", "not-affected"),
        ("suite-a", "recheck", "not-affected"),       # compared with the tree it passed on: only README.md
        ("suite-b", "recheck", "not-affected"),       # decided again for the new tree: still nothing under b
        ("docs", "recheck", "passed")]                # README.md is what docs covers: it runs in the round
    recheck_a = before_gate[3]
    assert recheck_a["paths"] == ["README.md"] and recheck_a["tree"] != before_gate[0]["tree"]
    assert _step(state, "docs")["status"] == "passed" and state["checks"]["docs"]["outcome"] == "passed"
    assert _step(state, "suite-b")["status"] == "not-affected"
    assert state["checks"]["suite-a"]["outcome"] == "passed"   # the round did not replace the real result
    gate = {e["check"]: e for e in _ledger(state, phase="gate")}
    assert gate["suite-a"]["outcome"] == "passed" and gate["suite-a"]["tree"] != before_gate[0]["tree"]
    assert gate["docs"]["outcome"] == "reused" and gate["docs"]["from"]["phase"] == "recheck"
    assert _ran(tmp_path) == ["suite-a", "docs", "suite-a", "suite-b"]
    assert state["ship"]["shipped"] is True


def test_review_round_records_the_pass_it_carries(make_layer, app_repo, tmp_path):
    """The round that leaves suite-a's paths alone carries suite-a's pass to the new tree: a
    check.carried event and a `carried` entry on the record, whose outcome stays the real pass."""
    layer = _layer(make_layer, tmp_path, app_repo, review={"require": "all"})
    _build(layer, {"a/x.txt": "x\n"})
    _script(layer, "review-1", {"output": json.dumps(REFUSED)})
    _script(layer, "review-2-1", {"output": json.dumps(APPROVED)})
    _script(layer, "implement-1", {"files": {"README.md": "# The app\n\nNow documented.\n"}, "commit": True})
    assert main("zte", layer, ["Change a", "--adw-id", "afec0017"]) == 0
    state = _state(layer, "afec0017")
    (passed_a,) = _ledger(state, check="suite-a", phase="check")
    (recheck_a,) = _ledger(state, check="suite-a", phase="recheck")
    record = state["checks"]["suite-a"]
    assert record["outcome"] == "passed"
    carried = record["carried"]
    assert carried["from_tree"] == passed_a["tree"] and carried["changed"] == ["README.md"]
    assert carried["tree"] == recheck_a["tree"]
    lines = (layer / "agents" / "afec0017" / events.FILE_NAME).read_text(encoding="utf-8").splitlines()
    carried_events = [e for e in map(json.loads, lines) if e["type"] == "check.carried"]
    assert [e["step"] for e in carried_events] == ["suite-a"]
    assert "check.carried" in events.TYPES
    assert "carried" not in state["checks"]["suite-b"]   # it never passed, so it carries nothing


def test_review_round_reruns_only_what_the_patch_touched(make_layer, app_repo, tmp_path):
    layer = _layer(make_layer, tmp_path, app_repo)
    _build(layer, {"a/x.txt": "x\n"})
    _script(layer, "review-1", {"output": json.dumps(REFUSED)})
    _script(layer, "review-2-1", {"output": json.dumps(APPROVED)})
    _script(layer, "implement-1", {"files": {"b/y.txt": "y\n"}, "commit": True})
    assert main("zte", layer, ["Change a, then b", "--adw-id", "afec0008"]) == 0
    state = _state(layer, "afec0008")
    # suite-b was not affected by the build; the patch touched b, so the round runs it and its step passes
    assert _step(state, "suite-b")["status"] == "passed"
    assert _step(state, "suite-b")["detail"] == "exit 0, log agents/afec0008/checks/suite-b.1.log"
    assert state["checks"]["suite-b"]["outcome"] == "passed" and state["checks"]["suite-b"]["exit"] == 0
    (recheck_b,) = _ledger(state, check="suite-b", phase="recheck")
    assert recheck_b["outcome"] == "passed" and recheck_b["why"] == "covers b/y.txt"
    (recheck_a,) = _ledger(state, check="suite-a", phase="recheck")
    assert (recheck_a["outcome"], recheck_a["paths"], recheck_a["why"]) == ("not-affected", ["b/y.txt"], NO_COVERED)
    assert _step(state, "suite-a")["status"] == "passed"       # it did pass, on the tree before the patch
    assert _step(state, "docs")["status"] == "not-affected"
    # the gate judges the final tree against the run's base: a and b changed, docs did not
    assert {e["check"]: e["outcome"] for e in _ledger(state, phase="gate")} == {
        "suite-a": "passed", "suite-b": "reused", "docs": "not-affected"}
    assert _ledger(state, check="docs", phase="gate")[0]["paths"] == ["a/x.txt", "b/y.txt"]
    assert _ran(tmp_path) == ["suite-a", "suite-b", "suite-a"]
    assert state["ship"]["shipped"] is True


def test_non_deterministic_check_is_always_rerun(make_layer, app_repo, tmp_path):
    layer = _layer(make_layer, tmp_path, app_repo, review={"requireChecks": ["suite-a", "flaky"], "require": "all"},
                   steps=[{"name": "build", "kind": "run", "command": "build"},
                          _check("suite-a"), _check("flaky"),
                          {"name": "commit", "kind": "run", "command": "commit"},
                          {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}])
    _build(layer, {"a/x.txt": "x\n"})
    assert main("zte", layer, ["Change a", "--adw-id", "afec0009"]) == 0
    state = _state(layer, "afec0009")
    assert _ran(tmp_path) == ["suite-a", "flaky", "flaky"]      # #:reuse #f: the gate runs it on the same tree
    (gate_flaky,) = _ledger(state, check="flaky", phase="gate")
    assert gate_flaky["outcome"] == "passed" and gate_flaky["tree"] == state["checks"]["flaky"]["tree"]
    assert _ledger(state, check="suite-a", phase="gate")[0]["outcome"] == "reused"
    gate_events = [e for e in events.read(layer / "agents" / "afec0009" / "events.jsonl")
                   if e["type"] == "check.end" and e["phase"] == "gate"]
    assert [e["cmd"] for e in gate_events] == ["flaky"]


def test_gate_never_reuses_a_pass_from_another_tree_and_refuses_when_the_final_tree_fails(make_layer, app_repo, tmp_path):
    counter = tmp_path / "ran.log"
    layer = _layer(make_layer, tmp_path, app_repo, review={"requireChecks": ["suite-a"], "require": "affected"},
                   commands=[_cmd(counter, "suite-a", ["a"], then="test ! -e a/broken")],
                   steps=[{"name": "build", "kind": "run", "command": "build"}, _check("suite-a"),
                          {"name": "commit", "kind": "run", "command": "commit"},
                          {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}])
    _script(layer, "build", {"files": {"a/x.txt": "x\n"}})
    # after the check passed, a later step changes a file the check covers
    _script(layer, "commit", {"files": {"a/broken": "x\n"}, "commit": True})
    before = _git(app_repo, "rev-parse", "main")
    assert main("zte", layer, ["Change a", "--adw-id", "afec000a"]) == 0
    state = _state(layer, "afec000a")
    assert state["checks"]["suite-a"]["exit"] == 0 and _step(state, "suite-a")["status"] == "passed"
    (gate,) = _ledger(state, check="suite-a", phase="gate")
    assert gate["outcome"] == "failed" and gate["exit"] == 1 and gate["tree"] != state["checks"]["suite-a"]["tree"]
    assert not _ledger(state, outcome="reused") and not _ledger(state, phase="gate", outcome="not-affected")
    assert _ran(tmp_path) == ["suite-a", "suite-a"]
    assert "require-check-failed:suite-a" in state["ship"]["gate"]["reasons"] and state["ship"]["gate"]["ship"] is False
    assert state["ship"]["shipped"] is False and state["status"] == "handed-over"
    assert _git(app_repo, "rev-parse", "main") == before


def test_gate_requires_everything_by_default(make_layer, app_repo, tmp_path):
    layer = _layer(make_layer, tmp_path, app_repo, review={"require": "all"})
    _build(layer, {"README.md": "# The app\n"})
    assert main("zte", layer, ["Reorganise the README", "--adw-id", "afec000b"]) == 0
    state = _state(layer, "afec000b")
    # the check steps ran docs only; the gate runs the two code suites, which have no pass on the final tree
    assert _ran(tmp_path) == ["docs", "suite-a", "suite-b"]
    assert {e["check"]: e["outcome"] for e in _ledger(state, phase="gate")} == {
        "suite-a": "passed", "suite-b": "passed", "docs": "reused"}
    assert not _ledger(state, phase="gate", outcome="not-affected")


def test_resume_redecides_a_not_affected_step_and_reuses_passes(make_layer, app_repo, tmp_path):
    counter, ok = tmp_path / "ran.log", tmp_path / "b-is-fixed"
    layer = _layer(make_layer, tmp_path, app_repo, keep="always", commands=[
        _cmd(counter, "suite-a", ["a"]), _cmd(counter, "suite-b", ["b"], then=f"test -e {ok}"),
        _cmd(counter, "docs", ["README.md", "docs"])],
        steps=[{"name": "build", "kind": "run", "command": "build"},
               _check("suite-a"), _check("docs"), _check("suite-b"), _check("again", cmd="suite-a")])
    _script(layer, "build", {"files": {"a/x.txt": "x\n", "b/y.txt": "y\n"}})
    assert main("zte", layer, ["Change a and b", "--adw-id", "afec000c"]) == 1
    first = _state(layer, "afec000c")
    assert _ran(tmp_path) == ["suite-a", "suite-b"]
    assert [(_step(first, n)["status"]) for n in ("suite-a", "docs", "suite-b")] == ["passed", "not-affected", "failed"]
    assert "again" not in first["checks"]
    ok.write_text("fixed outside the worktree\n")
    assert main("zte", layer, ["--resume", "--adw-id", "afec000c"]) == 0
    state = _state(layer, "afec000c")
    # suite-a passed and is not run again; docs is decided again; a failed result is never reused
    assert _ran(tmp_path) == ["suite-a", "suite-b", "suite-b"]
    assert [e["outcome"] for e in _ledger(state, check="docs")] == ["not-affected", "not-affected"]
    assert [e["outcome"] for e in _ledger(state, check="suite-b")] == ["failed", "passed"]
    assert _step(state, "docs")["status"] == "not-affected" and _step(state, "docs")["attempts"] == 2
    # the ledger survived the resume: the second step on the same cmd takes the pass of the first run
    again = state["checks"]["again"]
    tree = state["checks"]["suite-a"]["tree"]
    assert (again["outcome"], again["exit"], again["tree"]) == ("reused", 0, tree)
    assert again["from"] == {"step": "suite-a", "phase": "check", "log": "agents/afec000c/checks/suite-a.1.log"}
    assert _step(state, "again")["status"] == "passed"
    assert _step(state, "again")["detail"] == (f"reused from step suite-a, same tree {tree[:8]}, "
                                               "log agents/afec000c/checks/suite-a.1.log")
    assert f"again=reused from suite-a, same tree {tree[:8]}" in next(
        line for line in _summary(layer, "afec000c") if line.startswith("checks:"))
    assert state["status"] == "passed"


# ---------------------------------------------------------------- what a person reads

def test_summary_and_pr_text_show_each_check(make_layer, app_repo, tmp_path):
    layer = _layer(make_layer, tmp_path, app_repo, review={"ship": "pr"})
    _build(layer, {"a/x.txt": "x\n"})
    assert main("zte", layer, ["Change a", "--adw-id", "afec000d"]) == 0
    state = _state(layer, "afec000d")
    tree = state["checks"]["suite-a"]["tree"]
    lines = _summary(layer, "afec000d")
    checks_line = next(line for line in lines if line.startswith("checks:"))
    gate_line = next(line for line in lines if line.startswith("gate:"))
    # (read back from state.json, whose keys are sorted)
    assert checks_line == (f"checks:    docs=not affected ({NO_COVERED}; 1 paths compared), suite-a=0, "
                           f"suite-b=not affected ({NO_COVERED}; 1 paths compared)")
    assert gate_line == (f"gate:      suite-a=reused from suite-a, same tree {tree[:8]}, "
                         f"suite-b=not affected ({NO_COVERED}; 1 paths compared), "
                         f"docs=not affected ({NO_COVERED}; 1 paths compared)")
    assert lines.index(gate_line) == lines.index(checks_line) + 1
    assert state["ship"]["target"] == "agents/afec000d/PR.md"
    assert state["ship"]["checks_report"] == {"posted": True, "where": "agents/afec000d/PR.md", "warning": ""}
    text = (layer / "agents" / "afec000d" / "PR.md").read_text()
    report = text[text.index("## What the checks proved"):]
    assert text.count("## What the checks proved") == 1 and text.endswith(report) and report.endswith("- `a/x.txt`\n")
    rows = [line for line in report.splitlines() if line.startswith("| `")]
    assert rows == [
        f"| `suite-a` | suite-a | passed | log `agents/afec000d/checks/suite-a.1.log`, tree `{tree[:8]}` |",
        f"| `suite-a` | gate | reused from suite-a, same tree {tree[:8]} | log `agents/afec000d/checks/suite-a.1.log` |",
        f"| `suite-b` | suite-b | not affected ({NO_COVERED}; 1 paths compared) | 1 paths compared, tree `{tree[:8]}` |",
        f"| `suite-b` | gate | not affected ({NO_COVERED}; 1 paths compared) | 1 paths compared, tree `{tree[:8]}` |",
        f"| `docs` | docs | not affected ({NO_COVERED}; 1 paths compared) | 1 paths compared, tree `{tree[:8]}` |",
        f"| `docs` | gate | not affected ({NO_COVERED}; 1 paths compared) | 1 paths compared, tree `{tree[:8]}` |"]
    assert str(tmp_path) not in report and "/home/" not in report and "/tmp/" not in report


def test_report_is_posted_as_one_comment_on_the_pull_request(make_layer, app_repo, tmp_path, monkeypatch):
    layer_dir = _layer(make_layer, tmp_path, app_repo, extra={"forge": {
        "id": "github", "kind": "github", "status": "stable", "remote": "origin", "repo": "acme/app"}})
    layer = ir.load(layer_dir)
    state = state_module.ADWState(adw_id="afec000e", adw="zte")
    affected.record(state, check="suite-a", step="suite-a", phase="check", outcome="passed", tree="a" * 40,
                    definition="d", exit=0, log="agents/afec000e/checks/suite-a.1.log")
    comments: list[tuple[int, str]] = []

    class Client:
        def comment(self, number: int, body: str) -> None:
            comments.append((number, body))

    from adw_modules import forge
    monkeypatch.setattr(forge, "get_client", lambda _layer: Client())
    affected.publish(layer, state)                              # nothing shipped yet: nothing to attach it to
    assert comments == [] and "checks_report" not in state.ship
    state.ship.update({"action": "pr", "pr": {"number": 7, "url": "https://example.org/pr/7"}, "target": "https://example.org/pr/7"})
    affected.publish(layer, state)
    assert [number for number, _ in comments] == [7] and comments[0][1].startswith("## What the checks proved\n")
    assert state.ship["checks_report"] == {"posted": True, "where": "PR #7", "warning": ""}
    state.dry_run = True                                        # a dry run never uses the forge
    affected.publish(layer, state)
    assert len(comments) == 1 and state.ship["checks_report"]["posted"] is False

    class Broken:
        def comment(self, number: int, body: str) -> None:
            raise RuntimeError("forge is down")

    state.dry_run = False
    monkeypatch.setattr(forge, "get_client", lambda _layer: Broken())
    affected.publish(layer, state)                              # never raises
    assert state.ship["checks_report"] == {"posted": False, "where": "", "warning": "RuntimeError: forge is down"}


# ---------------------------------------------------------------- layers that declare nothing

def test_layer_without_declarations_runs_every_check(make_layer, app_repo, tmp_path):
    """An IR written before this feature (no covers / always / reuse, no whenAffected, no require):
    every check step runs for every change; the one new behaviour is the gate's reuse."""
    counter = tmp_path / "ran.log"
    old_step = {"resolve": "", "retries": 0, "collect": [], "in": "", "maxSkipped": -1, "forbidSkipReason": ""}
    layer = _layer(make_layer, tmp_path, app_repo,
                   commands=[{"name": name, "run": f"echo {name} >> {counter}", "timeout": 20, "description": ""}
                             for name in ("suite-a", "suite-b", "docs")],
                   steps=[{"name": "build", "kind": "run", "command": "build"},
                          *[{"name": name, "kind": "check", "cmd": name, **old_step} for name in ("suite-a", "suite-b", "docs")],
                          {"name": "commit", "kind": "run", "command": "commit"},
                          {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}])
    raw = json.loads((layer / "agentic-layer.json").read_text())
    del raw["reviews"][0]["require"]
    (layer / "agentic-layer.json").write_text(json.dumps(raw, indent=2))
    _build(layer, {"README.md": "# The app\n"})
    assert main("zte", layer, ["Reorganise the README", "--adw-id", "afec000f"]) == 0
    state = _state(layer, "afec000f")
    assert _ran(tmp_path) == ["suite-a", "suite-b", "docs"]
    assert {_step(state, name)["status"] for name in ("suite-a", "suite-b", "docs")} == {"passed"}
    assert not _ledger(state, outcome="not-affected")
    assert [e["outcome"] for e in _ledger(state, phase="gate")] == ["reused"] * 3
    assert {e["why"] for e in _ledger(state, phase="check")} == {""}
    checks_line = next(line for line in _summary(layer, "afec000f") if line.startswith("checks:"))
    assert checks_line == "checks:    docs=0, suite-a=0, suite-b=0"     # read back from state.json: sorted
    assert state["ship"]["shipped"] is True
