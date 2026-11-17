"""Affected checks and reused results (adw_modules/affected.py): what a `#:covers` entry matches,
which checks a set of changed paths needs, the tree hash a result belongs to, the ledger, and the
texts a person reads. Pure functions and real git repositories; no run, no agent."""
from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from adw_modules import affected, gitsafe
from adw_modules.ir import load
from adw_modules.state import ADWState

ADW_ID = "af000001"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.org", *args],
                          check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def _checks(**extra) -> list[dict]:
    return [{"name": "suite-a", "covers": ["a"], "always": False, "reuse": True},
            {"name": "suite-b", "covers": ["b"], "always": False, "reuse": True},
            {"name": "docs", "covers": ["README.md", "docs"], "always": False, "reuse": True},
            *([{"name": "lint", "covers": extra["lint"], "always": True, "reuse": True}] if "lint" in extra else [])]


def _layer(make_layer, *, copy_files=(), commands=None, steps=None, review=None, extra=None) -> Path:
    cmds = commands if commands is not None else [
        {"name": "suite-a", "run": "true", "covers": ["a"], "always": False, "reuse": True},
        {"name": "suite-b", "run": "true", "covers": ["b"], "always": False, "reuse": True},
        {"name": "lint", "run": "true"}]
    return make_layer({
        "codebase": {"commands": cmds, "paths": {"source": ["a", "b", "tools"], "protected": []}},
        "environments": [{"id": "worktree", "kind": "worktree", "dir": "trees", "copyFiles": list(copy_files),
                          "portBase": 27100, "portsPerRun": 0, "keep": "never", "status": "stable"}],
        "reviews": [{"id": "auto", "kind": "auto", "status": "stable", "artefacts": [], "command": "review",
                     "reviewers": 1, "maxRetries": 0, "allowedClasses": ["chore"], "blockProtectedPaths": True,
                     "maxDiffLines": 100, "requireChecks": ["suite-b"], "ship": "pr", "deployCmd": "",
                     **(review or {})}],
        "adws": [{"name": "demo", "agent": "mock-agent", "review": "auto", "environment": ["worktree"],
                  "steps": steps if steps is not None else [
                      {"name": "suite-a", "kind": "check", "cmd": "suite-a", "whenAffected": True},
                      {"name": "lint", "kind": "check", "cmd": "lint"}]}],
        **(extra or {})})


def _run(layer_dir: Path, worktree: Path):
    """A layer, a run whose worktree is `worktree`, and an environment stack without a sandbox."""
    state = ADWState(adw_id=ADW_ID, adw="demo", worktree=str(worktree))
    return load(layer_dir), state, SimpleNamespace(sandbox=None)


# ---------------------------------------------------------------- matching

@pytest.mark.parametrize("path, patterns, expected", [
    ("kit/dashboard/x.py", ["kit/dashboard"], True),
    ("kit/dashboard", ["kit/dashboard"], True),
    ("kit/dashboard2/x.py", ["kit/dashboard"], False),
    ("kit/x.py", ["kit/dashboard"], False),
    ("README.md", ["README.md"], True),
    ("docs/README.md", ["README.md"], False),
    ("docs/a.md", ["*.md"], True),
    ("a.md", ["*.md"], True),
    ("docs/a.txt", ["*.md"], False),
    ("docs/guide/a.txt", ["docs/**"], True),
    ("docs", ["docs/**"], True),
    ("documents/a.txt", ["docs/**"], False),
    ("kit/runtime/steps.py", ["./kit/"], True),
    ("anything/at/all.py", ["."], True),
    ("anything/at/all.py", [""], True),
    ("src/Main.py", ["src/main.py"], False),          # case-sensitive
    ("tests/test_x.py", ["test_*.py"], True),         # a glob without a slash also matches the file name
    ("a/b/c.py", ["other", "a/b"], True),
    ("a/b/c.py", [], False),
])
def test_matches_directories_files_and_globs(path, patterns, expected):
    assert affected.matches(path, patterns) is expected


# ---------------------------------------------------------------- selection

def test_select_runs_covering_and_always_checks_only():
    result = affected.select(_checks(lint=["a", "b", "README.md", "docs"]), ["a/x.txt", "a/y.txt"])
    assert result["everything"] == ""
    assert result["run"] == {"suite-a": "covers a/x.txt and 1 more", "lint": "runs for every change"}
    assert result["skip"] == {"suite-b": "no covered path changed", "docs": "no covered path changed"}


def test_select_uncovered_path_runs_everything_and_names_it():
    result = affected.select(_checks(), ["a/x.txt", "other/x.txt"])
    assert result["everything"] == "other/x.txt is covered by no check"
    assert result["skip"] == {} and set(result["run"]) == {"suite-a", "suite-b", "docs"}
    assert set(result["run"].values()) == {"other/x.txt is covered by no check"}


def test_select_file_declared_by_several_selective_checks_runs_everything():
    checks = [{"name": "suite-a", "covers": ["a", "shared"], "always": False, "reuse": True},
              {"name": "suite-b", "covers": ["b", "shared/**"], "always": False, "reuse": True}]
    result = affected.select(checks, ["shared/util.py"])
    assert result["everything"] == "shared/util.py is declared by several checks (suite-a, suite-b)"
    assert result["skip"] == {} and set(result["run"]) == {"suite-a", "suite-b"}
    assert affected.select(checks, ["a/x.py"])["skip"] == {"suite-b": "no covered path changed"}


def test_select_always_checks_do_not_count_as_overlap():
    checks = [{"name": "suite-a", "covers": ["a"], "always": False, "reuse": True},
              {"name": "lint", "covers": ["a", "b"], "always": True, "reuse": True},
              {"name": "suite-b", "covers": ["b"], "always": False, "reuse": True}]
    result = affected.select(checks, ["a/x.py"])
    assert result["everything"] == ""
    assert result["run"] == {"suite-a": "covers a/x.py", "lint": "runs for every change"}
    assert result["skip"] == {"suite-b": "no covered path changed"}


def test_select_layer_change_runs_everything():
    in_layer = affected.select(_checks(), ["a/x.txt", "agentic/commands/plan.md"], layer_dir="agentic")
    assert in_layer["everything"] == "the change touches the agentic layer (agentic/commands/plan.md)"
    assert in_layer["skip"] == {} and len(in_layer["run"]) == 3
    assert affected.select(_checks(), ["agentic2/x.txt", "a/x"], layer_dir="agentic")["everything"] == (
        "agentic2/x.txt is covered by no check")          # a sibling directory is not the layer
    workflow = affected.select(_checks(), ["a/x.txt"], changes_layer=True)
    assert workflow["everything"].startswith("the change touches the agentic layer") and workflow["skip"] == {}


def test_select_unknown_changes_run_everything():
    result = affected.select(_checks(), None)
    assert result["everything"] == "the changed paths could not be computed"
    assert result["skip"] == {} and set(result["run"]) == {"suite-a", "suite-b", "docs"}


def test_select_empty_change_runs_only_always_checks():
    result = affected.select(_checks(lint=[]), [])
    assert result["everything"] == ""
    assert result["run"] == {"lint": "runs for every change"}
    assert set(result["skip"]) == {"suite-a", "suite-b", "docs"}


def test_workflow_checks_lists_step_cmds_then_required_checks_with_defaults(make_layer, app_repo):
    layer = load(_layer(make_layer))
    assert affected.workflow_checks(layer, layer.adw("demo")) == [
        {"name": "suite-a", "covers": ["a"], "always": False, "reuse": True},
        {"name": "lint", "covers": [], "always": True, "reuse": True},      # an IR entry without the three keys
        {"name": "suite-b", "covers": ["b"], "always": False, "reuse": True}]


# ---------------------------------------------------------------- git: changed paths and the tree

def test_changed_counts_both_sides_of_a_rename_and_a_deleted_file(make_layer, app_repo):
    layer, state, stack = _run(_layer(make_layer), app_repo)
    base = _git(app_repo, "rev-parse", "HEAD")
    _git(app_repo, "mv", "app.py", "main.py")
    (app_repo / "test.sh").unlink()
    (app_repo / "new").mkdir()
    (app_repo / "new" / "ünï.txt").write_text("x\n", encoding="utf-8")
    tree = affected.tree(layer, state, stack)
    assert affected.changed(layer, state, stack, base, tree) == ["app.py", "main.py", "new/ünï.txt", "test.sh"]
    assert affected.changed(layer, state, stack, tree, tree) == []
    assert affected.changed(layer, state, stack, "", tree) is None
    assert affected.changed(layer, state, stack, base, "") is None
    assert affected.changed(layer, state, stack, "0" * 40, tree) is None      # git fails: unknown, not "nothing"


def test_tree_includes_untracked_files_and_leaves_the_index_alone(make_layer, app_repo):
    layer, state, stack = _run(_layer(make_layer), app_repo)
    clean = affected.tree(layer, state, stack)
    assert clean == _git(app_repo, "rev-parse", "HEAD^{tree}")
    (app_repo / "untracked.txt").write_text("new\n", encoding="utf-8")
    (app_repo / "app.py").write_text("print('changed')\n", encoding="utf-8")
    index, status = (app_repo / ".git" / "index").read_bytes(), _git(app_repo, "status", "--porcelain")
    dirty = affected.tree(layer, state, stack)
    assert len(dirty) == 40 and dirty != clean and affected.tree(layer, state, stack) == dirty
    assert (app_repo / ".git" / "index").read_bytes() == index
    assert _git(app_repo, "status", "--porcelain") == status and "?? untracked.txt" in status
    listing = _git(app_repo, "ls-tree", "-r", "--name-only", dirty).splitlines()
    assert "untracked.txt" in listing and "app.py" in listing


def test_tree_equals_head_tree_once_committed(make_layer, app_repo):
    layer, state, stack = _run(_layer(make_layer), app_repo)
    (app_repo / "feature.py").write_text("FEATURE = True\n", encoding="utf-8")
    (app_repo / "test.sh").unlink()
    before = affected.tree(layer, state, stack)
    assert before != affected.head_tree(layer, state, stack)
    _git(app_repo, "add", "-A")
    _git(app_repo, "commit", "-m", "feature")
    assert affected.tree(layer, state, stack) == before == affected.head_tree(layer, state, stack)
    assert before == _git(app_repo, "rev-parse", "HEAD^{tree}")


def test_tree_ignores_copy_files(make_layer, app_repo):
    layer, state, stack = _run(_layer(make_layer, copy_files=[".env", "config/local.json"]), app_repo)
    clean = affected.tree(layer, state, stack)
    (app_repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (app_repo / "config").mkdir()
    (app_repo / "config" / "local.json").write_text("{}\n", encoding="utf-8")
    assert affected.tree(layer, state, stack) == clean
    (app_repo / "config" / "other.json").write_text("{}\n", encoding="utf-8")
    assert affected.tree(layer, state, stack) != clean


def test_tree_is_empty_when_git_fails(make_layer, app_repo, tmp_path, capsys):
    layer, state, stack = _run(_layer(make_layer), tmp_path / "not-a-repository")
    (tmp_path / "not-a-repository").mkdir()
    assert affected.tree(layer, state, stack) == ""
    assert affected.tree(layer, state, stack) == ""
    assert affected.head_tree(layer, state, stack) == "" and affected.base(layer, state, stack) == ""
    out = capsys.readouterr().out
    assert out.count("the worktree's tree hash could not be computed") == 1      # said once per run
    assert "so every check runs and no result is reused" in out
    missing = ADWState(adw_id="af000002", adw="demo", worktree="")
    assert affected.tree(layer, missing, stack) == ""
    # and then nothing is selected away or reused
    decision = affected.decide(layer, state, stack, layer.adw("demo")["steps"][0])
    assert decision.action == "run" and decision.why == "the changed paths could not be computed"


def test_base_is_the_merge_base_with_the_default_branch(make_layer, app_repo):
    layer, state, stack = _run(_layer(make_layer), app_repo)
    start = _git(app_repo, "rev-parse", "HEAD")
    _git(app_repo, "checkout", "-q", "-b", "work")
    (app_repo / "a").mkdir()
    (app_repo / "a" / "x.txt").write_text("x\n", encoding="utf-8")
    _git(app_repo, "add", "-A")
    _git(app_repo, "commit", "-m", "work")
    assert affected.base(layer, state, stack) == start


# ---------------------------------------------------------------- definition and ledger

def test_definition_changes_with_the_command_the_environment_and_the_skip_rules(make_layer, app_repo):
    layer, state, _stack = _run(_layer(make_layer), app_repo)
    cmd = {"name": "suite-a", "run": "sh a.sh"}
    step = {"name": "suite-a", "kind": "check", "cmd": "suite-a", "in": "", "maxSkipped": -1, "forbidSkipReason": ""}
    first = affected.definition(layer, state, cmd, step)
    assert len(first) == 16 and int(first, 16) >= 0 and affected.definition(layer, state, dict(cmd), dict(step)) == first
    assert affected.definition(layer, state, cmd, {"name": "suite-a", "cmd": "suite-a"}) == first   # the defaults
    assert affected.definition(layer, state, cmd, None) == first                # the gate, for a cmd without a step
    assert affected.definition(layer, state, cmd, {**step, "name": "other-step"}) == first
    different = [
        affected.definition(layer, state, {**cmd, "run": "sh a.sh --all"}, step),
        affected.definition(layer, state, cmd, {**step, "in": "engine"}),
        affected.definition(layer, state, cmd, {**step, "maxSkipped": 0}),
        affected.definition(layer, state, cmd, {**step, "forbidSkipReason": "no podman"}),
    ]
    state.image = "al-test:0123456789ab"
    different.append(affected.definition(layer, state, cmd, step))
    assert first not in different and len(set(different)) == len(different)


def test_passing_needs_the_same_tree_and_definition_and_a_pass():
    state = ADWState(adw_id=ADW_ID, adw="demo")
    tree, other = "a" * 40, "b" * 40
    assert affected.passing(state, "suite-a", "d1", tree) is None
    affected.record(state, check="suite-a", step="suite-a", phase="check", outcome="failed", tree=tree,
                    definition="d1", exit=1, log="agents/x/checks/suite-a.1.log")
    assert affected.passing(state, "suite-a", "d1", tree) is None                  # a failed result is never reused
    passed = affected.record(state, check="suite-a", step="suite-a", phase="check", outcome="passed", tree=tree,
                             definition="d1", exit=0, log="agents/x/checks/suite-a.2.log")
    assert affected.passing(state, "suite-a", "d1", tree) is passed
    assert affected.passing(state, "suite-a", "d1", other) is None                 # another tree
    assert affected.passing(state, "suite-a", "d2", tree) is None                  # another definition
    assert affected.passing(state, "suite-b", "d1", tree) is None                  # another check
    assert affected.passing(state, "suite-a", "d1", "") is None
    # a decision is not a pass: neither `not-affected` nor `reused` is ever a source
    affected.record(state, check="suite-b", step="suite-b", phase="check", outcome="not-affected", tree=tree,
                    definition="d1", why="no covered path changed", paths=["a/x.txt"])
    affected.record(state, check="suite-a", step="gate", phase="gate", outcome="reused", tree=tree, definition="d1",
                    exit=0, **{"from": passed})
    assert affected.passing(state, "suite-b", "d1", tree) is None
    assert affected.passing(state, "suite-a", "d1", tree) is passed
    # a check that changed the worktree has a result without a tree: found by nothing
    affected.record(state, check="writes", step="writes", phase="check", outcome="passed", tree="",
                    definition="d1", exit=0)
    assert affected.passing(state, "writes", "d1", "") is None and affected.last_pass(state, "writes", "d1") is None
    assert affected.last_pass(state, "suite-a", "d1") is passed and affected.last_pass(state, "suite-a", "d2") is None
    entry = state.check_results[2]
    assert set(entry) == {"check", "step", "phase", "outcome", "tree", "definition", "exit", "log", "why", "paths",
                          "paths_total", "from", "at"}
    assert entry["paths"] == ["a/x.txt"] and entry["paths_total"] == 1 and entry["from"] is None
    assert state.check_results[3]["from"] == {"step": "suite-a", "phase": "check",
                                              "log": "agents/x/checks/suite-a.2.log"}


def test_ledger_keeps_fifty_paths_and_the_real_count_and_survives_a_save(tmp_path):
    state = ADWState(adw_id=ADW_ID, adw="demo")
    state.save(tmp_path)
    assert "check_results" not in json.loads((tmp_path / "agents" / ADW_ID / "state.json").read_text())
    paths = [f"a/{index:03d}.txt" for index in range(120)]
    affected.record(state, check="suite-b", step="suite-b", phase="check", outcome="not-affected", tree="c" * 40,
                    definition="d", why="no covered path changed", paths=paths, paths_total=len(paths))
    assert state.check_results[0]["paths"] == paths[:50] and state.check_results[0]["paths_total"] == 120
    state.save(tmp_path)
    from adw_modules import state as state_module
    assert state_module.load(tmp_path, ADW_ID).check_results == state.check_results


# ---------------------------------------------------------------- decisions on a real repository

def _work(app_repo: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        (app_repo / relative).parent.mkdir(parents=True, exist_ok=True)
        (app_repo / relative).write_text(content, encoding="utf-8")


def test_decide_runs_skips_and_reuses(make_layer, app_repo):
    layer, state, stack = _run(_layer(make_layer, steps=[
        {"name": "suite-a", "kind": "check", "cmd": "suite-a", "whenAffected": True},
        {"name": "suite-b", "kind": "check", "cmd": "suite-b", "whenAffected": True},
        {"name": "plain-b", "kind": "check", "cmd": "suite-b"}]), app_repo)
    _git(app_repo, "checkout", "-q", "-b", "work")
    _work(app_repo, {"a/x.txt": "x\n"})
    a, b, plain = layer.adw("demo")["steps"]
    run = affected.decide(layer, state, stack, a)
    assert (run.action, run.why, run.paths, run.paths_total) == ("run", "covers a/x.txt", ["a/x.txt"], 1)
    skipped = affected.decide(layer, state, stack, b)
    assert (skipped.action, skipped.why, skipped.paths) == ("not-affected", "no covered path changed", ["a/x.txt"])
    assert skipped.reference == _git(app_repo, "rev-parse", "main") and skipped.tree == run.tree
    # a step that does not ask for affected checks always runs
    assert affected.decide(layer, state, stack, plain).action == "run"
    affected.record(state, check="suite-a", step="suite-a", phase="check", outcome="passed", tree=run.tree,
                    definition=run.definition, exit=0, log="agents/x/checks/suite-a.1.log")
    again = affected.decide(layer, state, stack, a)
    assert again.action == "reuse" and again.source is state.check_results[0] and again.tree == run.tree
    # the tree moves on: no reuse; compared with the tree of the last pass, only the new path counts
    _work(app_repo, {"b/y.txt": "y\n"})
    later = affected.decide(layer, state, stack, a)
    assert (later.action, later.paths, later.reference) == ("not-affected", ["b/y.txt"], run.tree)
    assert affected.decide(layer, state, stack, b).action == "run"


def test_decide_never_reuses_for_a_cmd_with_reuse_off(make_layer, app_repo):
    layer, state, stack = _run(_layer(make_layer, commands=[
        {"name": "flaky", "run": "true", "covers": ["a"], "always": True, "reuse": False}],
        steps=[{"name": "flaky", "kind": "check", "cmd": "flaky"}], review={"requireChecks": ["flaky"]}), app_repo)
    step = layer.adw("demo")["steps"][0]
    first = affected.decide(layer, state, stack, step)
    affected.record(state, check="flaky", step="flaky", phase="check", outcome="passed", tree=first.tree,
                    definition=first.definition, exit=0)
    assert affected.decide(layer, state, stack, step).action == "run"
    gate = affected.decide_gate(layer, state, stack, layer.cmd("flaky"), step, layer.option("reviews", "auto"))
    assert gate.action == "run" and gate.tree == first.tree


def test_gate_decision_needs_the_committed_tree(make_layer, app_repo):
    """Uncommitted changes at the gate: what would ship is not what is in the worktree, so a pass
    is not reused and nothing is judged not affected, whatever the review option says."""
    layer, state, stack = _run(_layer(make_layer, review={"requireChecks": ["suite-a", "suite-b"],
                                                          "require": "affected"}), app_repo)
    option = layer.option("reviews", "auto")
    _git(app_repo, "checkout", "-q", "-b", "work")
    _work(app_repo, {"a/x.txt": "x\n"})
    _git(app_repo, "add", "-A")
    _git(app_repo, "commit", "-m", "work")
    step = layer.adw("demo")["steps"][0]
    committed = affected.tree(layer, state, stack)
    affected.record(state, check="suite-a", step="suite-a", phase="check", outcome="passed", tree=committed,
                    definition=affected.definition(layer, state, layer.cmd("suite-a"), step), exit=0, log="l")
    reused = affected.decide_gate(layer, state, stack, layer.cmd("suite-a"), step, option)
    assert reused.action == "reuse" and reused.tree == committed
    skipped = affected.decide_gate(layer, state, stack, layer.cmd("suite-b"), None, option)
    assert (skipped.action, skipped.paths, skipped.why) == ("not-affected", ["a/x.txt"], "no covered path changed")
    # by default every required check runs
    assert affected.decide_gate(layer, state, stack, layer.cmd("suite-b"), None,
                                {**option, "require": "all"}).action == "run"
    assert affected.decide_gate(layer, state, stack, layer.cmd("suite-b"), None,
                                {key: value for key, value in option.items() if key != "require"}).action == "run"
    _work(app_repo, {"notes.txt": "not committed\n"})
    for name, check_step in (("suite-a", step), ("suite-b", None)):
        decision = affected.decide_gate(layer, state, stack, layer.cmd(name), check_step, option)
        assert (decision.action, decision.tree, decision.source) == ("run", "", None), name


def test_sandboxed_tree_goes_through_gitsafe(make_layer, app_repo, tmp_path, monkeypatch):
    """A sandboxed run: git runs inside gitsafe.session (container frozen, metadata verified), with
    the pinned locations and the hardening options, and the index it writes is a copy outside the
    worktree and outside the container-writable admin directory."""
    worktree = tmp_path / "trees" / ADW_ID
    _git(app_repo, "worktree", "add", "--detach", str(worktree))
    admin = app_repo / ".git" / "worktrees" / ADW_ID
    quarantine = tmp_path / "objects"
    quarantine.mkdir()
    frozen = {"now": False, "times": 0}

    @contextlib.contextmanager
    def freeze():
        frozen["now"], frozen["times"] = True, frozen["times"] + 1
        try:
            yield
        finally:
            frozen["now"] = False

    sandbox = gitsafe.Sandbox(app_repo / ".git", worktree, admin, quarantine, freeze)
    layer, state, _ = _run(_layer(make_layer, copy_files=[".env"]), worktree)
    stack = SimpleNamespace(sandbox=sandbox)
    (worktree / "new.txt").write_text("new\n", encoding="utf-8")
    (worktree / ".env").write_text("SECRET=1\n", encoding="utf-8")
    index_before = (admin / "index").read_bytes()
    calls: list[dict] = []
    real_run = subprocess.run

    def spy(argv, **kwargs):
        calls.append({"argv": list(argv), "env": dict(kwargs.get("env") or {}), "cwd": kwargs.get("cwd"),
                      "frozen": frozen["now"], "depth": sandbox._depth})
        return real_run(argv, **kwargs)

    monkeypatch.setattr(affected.subprocess, "run", spy)
    tree = affected.tree(layer, state, stack)
    monkeypatch.undo()
    assert len(tree) == 40 and frozen["times"] == 1 and not frozen["now"]     # one session for the whole hash
    assert [call["argv"][len(gitsafe.HARDENING) + 1] for call in calls] == ["add", "reset", "write-tree"]
    for call in calls:
        assert call["argv"][:len(gitsafe.HARDENING) + 1] == ["git", *gitsafe.HARDENING]
        assert call["frozen"] and call["depth"] >= 1 and call["cwd"] == worktree
        env = call["env"]
        assert env["GIT_DIR"] == str(admin) and env["GIT_WORK_TREE"] == str(worktree)
        assert env["GIT_COMMON_DIR"] == str(app_repo / ".git") and env["GIT_OBJECT_DIRECTORY"] == str(quarantine)
        index = Path(env["GIT_INDEX_FILE"])
        assert not index.is_relative_to(worktree) and not index.is_relative_to(app_repo) and not index.exists()
    assert (admin / "index").read_bytes() == index_before
    listing = real_run(["git", "ls-tree", "-r", "--name-only", tree], cwd=worktree, capture_output=True, text=True,
                       env={**gitsafe.worktree_env(sandbox)}, check=True).stdout.splitlines()
    assert "new.txt" in listing and ".env" not in listing
    assert affected.head_tree(layer, state, stack) == _git(app_repo, "rev-parse", "HEAD^{tree}")
    # a sandbox whose git metadata was tampered with is refused by gitsafe: no hash, so nothing is reused
    (admin / "config.worktree").write_text("[core]\n\thooksPath = /tmp/x\n", encoding="utf-8")
    assert affected.tree(layer, state, stack) == ""


# ---------------------------------------------------------------- what a person reads

def test_coverage_report_lists_undeclared_checks_and_uncovered_source_paths(make_layer, app_repo):
    layer = load(_layer(make_layer, extra={"stages": [{"name": "main", "checks": ["stage-check"]}]}, commands=[
        {"name": "suite-a", "run": "true", "covers": ["a"], "always": False, "reuse": True},
        {"name": "suite-b", "run": "true", "covers": ["b/**"], "always": False, "reuse": True},
        {"name": "lint", "run": "true"},
        {"name": "stage-check", "run": "true", "covers": [], "always": True, "reuse": True},
        {"name": "install", "run": "true"}]))
    report = affected.coverage_report(layer)
    # install is no check (no check step, review or stage names it); lint and stage-check declare nothing
    assert report == {"declared": True, "undeclared": ["lint", "stage-check"], "uncovered": ["tools"]}
    assert affected.coverage_report(layer, "demo") == report
    assert affected.coverage_report(layer, "no-such-workflow")["undeclared"] == ["stage-check"]
    plain = load(_layer(make_layer, commands=[{"name": "suite-a", "run": "true"}, {"name": "suite-b", "run": "true"},
                                               {"name": "lint", "run": "true"}]))
    assert affected.coverage_report(plain) == {"declared": False, "undeclared": ["suite-a", "lint", "suite-b"],
                                               "uncovered": ["a", "b", "tools"]}


def test_coverage_lines_wording():
    assert affected.coverage_lines({"declared": False, "undeclared": ["test"], "uncovered": ["src"]}) == [
        "coverage: no check declares #:covers, so every check runs for every change"]
    assert affected.coverage_lines({"declared": True, "undeclared": ["lint", "e2e"], "uncovered": ["tools", "kit/x"]}) == [
        "coverage: 2 checks declare no #:covers and run for every change: lint, e2e",
        "coverage: source path tools is covered by no check: a change there runs every check",
        "coverage: source path kit/x is covered by no check: a change there runs every check"]
    assert affected.coverage_lines({"declared": True, "undeclared": ["lint"], "uncovered": []}) == [
        "coverage: 1 check declares no #:covers and runs for every change: lint"]
    assert affected.coverage_lines({"declared": True, "undeclared": [], "uncovered": []}) == [
        "coverage: every check declares #:covers and every source path is covered"]


def _ledger_state() -> ADWState:
    state = ADWState(adw_id=ADW_ID, adw="demo")
    tree = "0123456789abcdef" * 2 + "01234567"
    passed = affected.record(state, check="suite-a", step="suite-a", phase="check", outcome="passed", tree=tree,
                             definition="d", exit=0, log=f"agents/{ADW_ID}/checks/suite-a.1.log", why="covers a/x.txt")
    affected.record(state, check="suite-b", step="suite-b", phase="check", outcome="not-affected", tree=tree,
                    definition="d", why="no covered path changed", paths=["a/x.txt"],
                    log=f"agents/{ADW_ID}/checks/suite-b.not-affected.log")
    affected.record(state, check="lint", step="lint", phase="check", outcome="failed", tree=tree, definition="d",
                    exit=2, log=f"agents/{ADW_ID}/checks/lint.1.log")
    affected.record(state, check="lint", step="lint", phase="recheck", outcome="passed", tree=tree, definition="d",
                    exit=0, log=f"agents/{ADW_ID}/checks/lint.2.log")
    affected.record(state, check="suite-a", step="gate", phase="gate", outcome="reused", tree=tree, definition="d",
                    exit=0, log=f"agents/{ADW_ID}/checks/gate-suite-a.log", **{"from": passed})
    affected.record(state, check="suite-b", step="gate", phase="gate", outcome="not-affected", tree=tree,
                    definition="d", why="no covered path changed", paths=["a/x.txt"],
                    log=f"agents/{ADW_ID}/checks/gate-suite-b.log")
    return state


def test_summary_text():
    state = _ledger_state()
    assert [affected.summary_text(entry) for entry in state.check_results] == [
        "passed", "not affected (no covered path changed; 1 paths compared)", "failed (exit 2)", "passed",
        "reused from suite-a, same tree 01234567", "not affected (no covered path changed; 1 paths compared)"]
    # a state.checks record reads the same way
    assert affected.summary_text({"cmd": "x", "exit": 0, "outcome": "not-affected", "why": "no covered path changed",
                                  "paths_total": 12}) == "not affected (no covered path changed; 12 paths compared)"


def test_rows_are_per_check_the_latest_step_result_then_the_gate():
    table = affected.rows(None, _ledger_state())
    assert [(row["check"], row["where"], row["outcome"]) for row in table] == [
        ("suite-a", "suite-a", "passed"), ("suite-a", "gate", "reused"),
        ("suite-b", "suite-b", "not-affected"), ("suite-b", "gate", "not-affected"),
        ("lint", "lint (review round)", "passed")]
    assert set(table[0]) == {"check", "where", "outcome", "why", "tree", "exit", "log", "paths", "paths_total", "from"}
    assert affected.rows(None, ADWState(adw_id=ADW_ID, adw="demo")) == []


def test_report_markdown_has_a_row_per_check_and_no_machine_paths(make_layer, app_repo, tmp_path):
    layer = load(_layer(make_layer))
    state = _ledger_state()
    report = affected.report_markdown(layer, state)
    lines = report.splitlines()
    assert lines[0] == "## What the checks proved" and lines[2] == "| Check | Where | Result | Evidence |"
    assert lines[4:9] == [
        f"| `suite-a` | suite-a | passed | log `agents/{ADW_ID}/checks/suite-a.1.log`, tree `01234567` |",
        f"| `suite-a` | gate | reused from suite-a, same tree 01234567 | log `agents/{ADW_ID}/checks/suite-a.1.log` |",
        "| `suite-b` | suite-b | not affected (no covered path changed; 1 paths compared) | 1 paths compared, tree `01234567` |",
        "| `suite-b` | gate | not affected (no covered path changed; 1 paths compared) | 1 paths compared, tree `01234567` |",
        f"| `lint` | lint (review round) | passed | log `agents/{ADW_ID}/checks/lint.2.log`, tree `01234567` |"]
    assert lines[-3:] == ["Paths compared:", "", "- `a/x.txt`"]
    assert str(tmp_path) not in report and "/home/" not in report
    assert affected.report_markdown(layer, ADWState(adw_id=ADW_ID, adw="demo")) == ""
    many = ADWState(adw_id=ADW_ID, adw="demo")
    paths = [f"a/{index:03d}.txt" for index in range(60)]
    affected.record(many, check="suite-b", step="suite-b", phase="check", outcome="not-affected", tree="c" * 40,
                    definition="d", why="no covered path changed", paths=paths, paths_total=60)
    tail = affected.report_markdown(layer, many).splitlines()
    assert tail[-1] == "- … and 40 more" and tail[-2] == "- `a/019.txt`" and len([l for l in tail if l.startswith("- `")]) == 20
