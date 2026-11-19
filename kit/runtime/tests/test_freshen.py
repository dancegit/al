"""The target branch moved while the run worked (freshen.py): nothing happens when it did not move,
nothing is merged when its commits touch none of the run's files, a clean merge is made and the checks
run again, a conflict before the review is resolved with resolve-conflicts or stops the run, and a
conflict before ship always stops it."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import convergence_fixtures as cf  # noqa: E402

from adw_modules import freshen, ir  # noqa: E402
from adw_modules.errors import StepFailed  # noqa: E402
from adw_modules.state import ADWState  # noqa: E402

RUN = "f7e50001"


def _commit_on_main(app: Path, path: str, content: str) -> None:
    tree = app.parent / f"main-{abs(hash((path, content)))}"
    cf.git(app, "worktree", "add", "-q", str(tree), "main")
    (tree / path).write_text(content)
    cf.git(tree, "add", path)
    cf.git(tree, "commit", "-qm", f"main: {path}")
    cf.git(app, "worktree", "remove", "--force", str(tree))


def _setup(make_layer, app_repo, run_content: str = "FEATURE = True\n"):
    cf.git(app_repo, "checkout", "-q", "-b", "side")
    layer = ir.load(cf.converge_layer(make_layer))
    worktree = app_repo.parent / "run-tree"
    cf.git(app_repo, "worktree", "add", "-q", "-b", "feature/run", str(worktree), "main")
    (worktree / "feature.py").write_text(run_content)
    cf.git(worktree, "add", "feature.py")
    cf.git(worktree, "commit", "-qm", "run: feature")
    state = ADWState(adw_id=RUN, adw=cf.ADW, prompt="Add the feature", worktree=str(worktree),
                     branch="feature/run", dry_run=True)
    state.steps = [{"name": "unit", "kind": "check", "status": "passed"}]
    stack = SimpleNamespace(podman_option=None, cwd=worktree, sandbox=None, read_only=False, env={},
                            wrap=lambda cmd: ["bash", "-c", cmd], process_env=lambda: {"PATH": "/usr/bin:/bin"})
    return layer, state, stack, worktree


def _adw(layer):
    return layer.adw(cf.ADW)


def test_no_op_when_target_did_not_move(make_layer, app_repo):
    layer, state, stack, worktree = _setup(make_layer, app_repo)
    head = cf.git(worktree, "rev-parse", "HEAD")
    assert freshen.behind(layer, state, stack) == 0
    record = freshen.run(layer, state, _adw(layer), stack, "review")
    assert (record["behind"], record["merged"], record["touched"]) == (0, False, [])
    assert cf.git(worktree, "rev-parse", "HEAD") == head


def test_moved_but_disjoint_files_records_and_merges_nothing(make_layer, app_repo):
    layer, state, stack, worktree = _setup(make_layer, app_repo)
    _commit_on_main(app_repo, "other.txt", "elsewhere\n")
    head = cf.git(worktree, "rev-parse", "HEAD")
    assert freshen.behind(layer, state, stack) == 1
    record = freshen.run(layer, state, _adw(layer), stack, "review")
    assert (record["behind"], record["touched"], record["merged"]) == (1, [], False)
    assert cf.git(worktree, "rev-parse", "HEAD") == head
    assert state.proof_plan["freshen"]["review"] is record


def test_moved_touching_clean_merges_and_reruns_checks(make_layer, app_repo):
    layer, state, stack, worktree = _setup(make_layer, app_repo)
    _commit_on_main(app_repo, "feature.py", "FEATURE = True\n")   # the same content: merges cleanly
    record = freshen.run(layer, state, _adw(layer), stack, "review")
    assert (record["touched"], record["conflicts"], record["merged"]) == (["feature.py"], [], True)
    assert record["checks_rerun"] == ["unit"] and state.checks["unit"]["exit"] == 0
    assert cf.git(worktree, "log", "-1", "--format=%s") == f"merge main into feature/run [adw {RUN}]"
    assert freshen.behind(layer, state, stack) == 0


def test_conflict_resolved_by_resolve_conflicts_mock(make_layer, app_repo):
    layer, state, stack, worktree = _setup(make_layer, app_repo)
    _commit_on_main(app_repo, "feature.py", "FEATURE = 'main'\n")
    report = {"resolved": [{"file": "feature.py", "how": "kept both"}], "regenerated": [], "undecidable": []}
    cf.script(Path(layer.layer_dir), "freshen-resolve", {
        "files": {"feature.py": "FEATURE = True\nMAIN = 'main'\n"},
        "output": "resolved\n\n```json\n" + json.dumps(report) + "\n```\n"})
    record = freshen.run(layer, state, _adw(layer), stack, "review")
    assert (record["conflicts"], record["merged"]) == (["feature.py"], True)
    assert (worktree / "feature.py").read_text() == "FEATURE = True\nMAIN = 'main'\n"
    assert cf.git(worktree, "status", "--porcelain") == ""
    assert freshen.behind(layer, state, stack) == 0
    brief = (Path(layer.layer_dir) / "agents" / RUN / "freshen-brief.md").read_text()
    assert "`<<<<<<<` is the run's side" in brief and "main: feature.py" in brief


def test_conflict_left_stops_target_moved_party_person(make_layer, app_repo):
    layer, state, stack, worktree = _setup(make_layer, app_repo)
    _commit_on_main(app_repo, "feature.py", "FEATURE = 'main'\n")
    head = cf.git(worktree, "rev-parse", "HEAD")
    with pytest.raises(StepFailed) as stop:
        freshen.run(layer, state, _adw(layer), stack, "review")   # the mock resolves nothing
    assert stop.value.reason == f"target-moved: feature.py conflict with main; party: person: al integrate LAYER {RUN}"
    assert (state.proof_plan["stopped"]["at"], state.proof_plan["stopped"]["party"]) == ("freshen-review", "person")
    assert cf.git(worktree, "rev-parse", "HEAD") == head and cf.git(worktree, "status", "--porcelain") == ""


def test_ship_conflict_stops_without_resolving(make_layer, app_repo):
    layer, state, stack, worktree = _setup(make_layer, app_repo)
    _commit_on_main(app_repo, "feature.py", "FEATURE = 'main'\n")
    with pytest.raises(StepFailed, match="target-moved: feature.py conflict with main"):
        freshen.run(layer, state, _adw(layer), stack, "ship")
    assert "freshen-resolve" not in [call["step"] for call in state.agent_calls]
    assert state.proof_plan["stopped"]["at"] == "freshen-ship"
    assert cf.git(worktree, "status", "--porcelain") == ""
