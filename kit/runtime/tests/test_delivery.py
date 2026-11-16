"""delivery.py: routing, branch placeholders, the run-scoped layer, entry methods, forge protection
and the cycle state, on temporary repositories. The stages come from the compiler's real output
(data/delivery-sprint.json, compiled from racket/al/tests/fixtures/delivery-sprint.rkt)."""
from __future__ import annotations

import copy
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from adw_modules import delivery, forge, ir, ship
from adw_modules.errors import StepFailed
from adw_modules.state import ADWState

DATA = Path(__file__).resolve().parent / "data" / "delivery-sprint.json"
NOW = datetime(2026, 11, 16, 9, 30, tzinfo=timezone.utc)   # ISO week 47


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
                            capture_output=True, text=True, timeout=30)
    if check:
        assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def sprint_ir() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


def _adw(**extra) -> dict:
    return {"name": "demo", "agent": "mock", "review": "auto", "environment": ["worktree"], "classes": [],
            "steps": [{"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}], **extra}


def _sprint_layer(make_layer, app_repo, *, stages=None, forge_option=None, adws=None, **review) -> Path:
    data = sprint_ir()
    overrides = {
        "stages": stages if stages is not None else data["stages"], "cycle": data["cycle"], "delivery": data["delivery"],
        "codebase": {"commands": [{"name": "test", "run": "./test.sh", "timeout": 5},
                                  {"name": "lint", "run": "true", "timeout": 5},
                                  {"name": "deploy", "run": "true", "timeout": 5}]},
        "reviews": [{"id": "auto", "kind": "auto", "status": "stable", "artefacts": [], "command": "review",
                     "reviewers": 1, "maxRetries": 0, "allowedClasses": ["chore", "bug", "feature"],
                     "blockProtectedPaths": True, "maxDiffLines": 100, "requireChecks": ["test"], "ship": "pr",
                     "deployCmd": "", **review}],
        "forge": forge_option or {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "adws": adws or [_adw()],
    }
    return make_layer(overrides)


def _open_cycle(layer_dir: Path, app_repo: Path, name: str = "2026-s47") -> dict:
    del app_repo
    return delivery.cycle_start(ir.load(layer_dir), name=name, now=NOW)


def _state(**extra) -> ADWState:
    return ADWState(adw_id="1234abcd", adw="demo", **extra)


def test_no_stages_means_no_target(make_layer, app_repo):
    layer = ir.load(make_layer({"adws": [_adw()]}))
    assert delivery.for_run(layer, layer.adw("demo"), _state()) is None
    assert layer.default_branch == "main" and layer.delivery == {}
    assert layer.codebase_default_branch == "main"


def test_route_prefers_deliver_to_then_first_accepting_then_final(make_layer, app_repo):
    stages = [
        {"name": "hot", "branch": "hot", "from": "main", "into": "", "accepts": ["bug"], "agentsMayMerge": True},
        {"name": "dev", "branch": "dev", "from": "main", "into": "", "accepts": ["bug", "feature"], "agentsMayMerge": True},
        {"name": "main", "branch": "main", "from": "", "into": "", "accepts": [], "agentsMayMerge": False},
    ]
    layer = ir.load(_sprint_layer(make_layer, app_repo, stages=stages))
    assert delivery.route(layer, {"deliverTo": "dev"}, "bug").name == "dev"
    assert delivery.route(layer, {}, "bug").name == "hot"
    assert delivery.route(layer, {}, "feature").name == "dev"
    assert delivery.route(layer, {}, "chore").name == "main"


def test_expand_branch_fills_cycle_version_and_date():
    stage = delivery.Stage(name="sprint", branch="sprint/{cycle}-{date}")
    state = {"stage": "sprint", "current": {"name": "2026-s47", "version": "", "opened_at": "2026-11-02T08:00:00+00:00"}}
    assert delivery.expand_branch(stage, state, NOW) == "sprint/2026-s47-2026-11-02"
    release = delivery.Stage(name="release", branch="release/{version}")
    assert delivery.expand_branch(release, {"stage": "release", "current": {"name": "1.2", "version": "1.2"}}, NOW) == "release/1.2"
    hotfix = delivery.Stage(name="hotfix", branch="hotfix/{date}")
    assert delivery.expand_branch(hotfix, state, NOW) == "hotfix/2026-11-16"   # not the cycle's stage: today


def test_no_open_cycle_is_refused_with_its_reason(make_layer, app_repo):
    layer = ir.load(_sprint_layer(make_layer, app_repo))
    state = _state(klass="feature")
    with pytest.raises(delivery.DeliveryError) as caught:
        delivery.for_run(layer, layer.adw("demo"), state, env={})
    assert caught.value.reason == "no-open-cycle"
    assert "al cycle" in caught.value.message
    assert state.delivery["refused"] == "no-open-cycle" and state.status == "failed"


def test_stage_branch_missing_is_refused(make_layer, app_repo):
    layer_dir = _sprint_layer(make_layer, app_repo)
    _open_cycle(layer_dir, app_repo)
    _git(app_repo, "branch", "-D", "sprint/2026-s47")
    layer = ir.load(layer_dir)
    with pytest.raises(delivery.DeliveryError) as caught:
        delivery.for_run(layer, layer.adw("demo"), _state(klass="feature"), env={})
    assert caught.value.reason == "stage-missing"


def test_scope_sets_default_branch_checks_and_deploy_cmd(make_layer, app_repo):
    stages = copy.deepcopy(sprint_ir()["stages"])
    stages[0].update(checks=["lint", "test"], deployCmd="deploy", environmentName="staging")
    layer_dir = _sprint_layer(make_layer, app_repo, stages=stages)
    _open_cycle(layer_dir, app_repo)
    layer = ir.load(layer_dir)
    original = copy.deepcopy(layer.ir)
    target = delivery.for_run(layer, layer.adw("demo"), _state(klass="feature"), env={})
    scoped = delivery.scope(layer, target)
    assert scoped.default_branch == "sprint/2026-s47"
    assert scoped.codebase_default_branch == "main"
    review = scoped.option("reviews", "auto")
    assert review["requireChecks"] == ["test", "lint"]
    assert review["deployCmd"] == "deploy"
    assert scoped.delivery["environment_name"] == "staging" and scoped.delivery["source"] == {"stage": "main", "branch": "main"}
    assert layer.ir == original and layer.default_branch == "main"
    own = delivery.scope(ir.load(_sprint_layer(make_layer, app_repo, stages=stages, deployCmd="lint")), target)
    assert own.option("reviews", "auto")["deployCmd"] == "lint"   # the review's own command wins


def _layer_with_forge(kind: str, entry: str, delivery_record: bool = True):
    option = {"none": {"kind": "none", "remote": ""}, "github": {"kind": "github", "repo": "acme/webshop"},
              "forgejo": {"kind": "forgejo", "host": "https://forge.example.org", "repo": "acme/webshop"}}[kind]
    return SimpleNamespace(ir={"forge": option}, layer_dir=Path("/nonexistent"), app_root=None,
                           delivery={"entry": entry} if delivery_record else {})


def test_entry_support_matrix(monkeypatch):
    for method in ("merge", "squash", "rebase"):
        assert delivery.entry_supported(_layer_with_forge("github", method), {"entry": method})
    assert not delivery.entry_supported(_layer_with_forge("forgejo", "fast-forward"), {"entry": "fast-forward"})
    assert delivery.entry_supported(_layer_with_forge("none", "merge"), {"entry": "merge"})
    for method in ("squash", "rebase", "fast-forward"):
        assert not delivery.entry_supported(_layer_with_forge("none", method), {"entry": method})
    monkeypatch.setattr(ship, "ENTRY_METHODS", ("merge", "squash"), raising=False)
    assert delivery.entry_supported(_layer_with_forge("none", "squash"), {"entry": "squash"})


def test_forge_merge_method_comes_from_the_stage():
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    scoped = forge.get_client(_layer_with_forge("github", "rebase"))
    assert scoped.merge_method == "rebase"
    assert forge.get_client(_layer_with_forge("github", "", delivery_record=False)).merge_method == "squash"
    scoped._run = run
    scoped.merge_pr(7)
    assert calls[-1][:5] == ["gh", "pr", "merge", "7", "--rebase"]


def test_branch_protection_linear_history_forbids_merge_entry(make_layer, app_repo, monkeypatch):
    layer_dir = _sprint_layer(make_layer, app_repo, forge_option={"id": "forgejo", "kind": "forgejo", "status": "stable",
                                                                   "host": "https://forge.example.org", "repo": "acme/webshop"})
    _open_cycle(layer_dir, app_repo)
    layer = ir.load(layer_dir)
    target = delivery.for_run(layer, layer.adw("demo"), _state(klass="feature"), env={})
    scoped = delivery.scope(layer, target)

    def http(method, url, headers, body):
        if url.endswith("/branch_protections/sprint%2F2026-s47"):
            return 200, {"branch_name": "sprint/2026-s47", "enable_push": False}
        if url.endswith("/repos/acme/webshop"):
            return 200, {"allow_merge_commits": False, "allow_squash_merge": True}
        raise AssertionError(url)

    monkeypatch.setattr(forge, "http_request", http)
    assert forge.get_client(scoped).branch_protection("sprint/2026-s47") == {
        "protected": True, "linear_history": True, "push_allowed": False}
    state = _state(klass="feature", worktree=str(app_repo))
    state.delivery = dict(scoped.delivery)
    with pytest.raises(StepFailed, match="delivery:entry-forbidden"):
        delivery.before_ship(scoped, state, scoped.adw("demo"), {"name": "ship"}, SimpleNamespace(sandbox=None))

    def missing(method, url, headers, body):
        raise forge.ForgeError(f"{method} {url} -> HTTP 404: not found")

    monkeypatch.setattr(forge, "http_request", missing)
    assert forge.get_client(scoped).branch_protection("x") == {"protected": False}


def test_class_moved_after_classify_is_handed_over(make_layer, app_repo):
    stages = [
        {"name": "dev", "branch": "dev", "from": "main", "into": "", "accepts": ["feature"], "agentsMayMerge": True},
        {"name": "main", "branch": "main", "from": "", "into": "", "accepts": ["chore", "bug"], "agentsMayMerge": True},
    ]
    layer_dir = _sprint_layer(make_layer, app_repo, stages=stages)
    _git(app_repo, "branch", "dev")
    layer = ir.load(layer_dir)
    target = delivery.for_run(layer, layer.adw("demo"), _state(klass="feature"), env={})
    scoped = delivery.scope(layer, target)
    state = _state(klass="bug", worktree=str(app_repo))   # classify said bug: it belongs on main
    with pytest.raises(StepFailed, match="delivery:class-moved"):
        delivery.before_ship(scoped, state, scoped.adw("demo"), {"name": "ship"}, SimpleNamespace(sandbox=None))


def test_cycle_start_creates_branch_from_source_head_without_checkout(make_layer, app_repo):
    layer_dir = _sprint_layer(make_layer, app_repo)
    head, symbolic = _git(app_repo, "rev-parse", "HEAD"), _git(app_repo, "symbolic-ref", "HEAD")
    result = delivery.cycle_start(ir.load(layer_dir), now=NOW)
    assert result["current"]["name"] == "2026-s47" and result["current"]["branch"] == "sprint/2026-s47"
    assert _git(app_repo, "rev-parse", "refs/heads/sprint/2026-s47") == head
    assert _git(app_repo, "symbolic-ref", "HEAD") == symbolic and _git(app_repo, "status", "--porcelain") == ""
    saved = json.loads((layer_dir / delivery.CYCLE_FILE).read_text())
    assert saved["current"]["from_commit"] == head and saved["kind"] == "sprint" and saved["version"] == 1


def test_cycle_start_closes_the_open_cycle_and_numbers_the_next(make_layer, app_repo):
    layer_dir = _sprint_layer(make_layer, app_repo)
    first = delivery.cycle_start(ir.load(layer_dir), name="one", now=NOW)
    second = delivery.cycle_start(ir.load(layer_dir), name="two", now=NOW)
    assert first["current"]["n"] == 1 and second["current"]["n"] == 2
    assert second["closed"]["name"] == "one" and second["closed"]["accepted"] is True   # nothing new: already in main
    saved = delivery.load_cycle(ir.load(layer_dir))
    assert [item["name"] for item in saved["history"]] == ["one"]


def test_cycle_start_refuses_existing_branch_and_missing_source(make_layer, app_repo):
    layer_dir = _sprint_layer(make_layer, app_repo)
    _git(app_repo, "branch", "sprint/taken")
    with pytest.raises(delivery.DeliveryError) as caught:
        delivery.cycle_start(ir.load(layer_dir), name="taken", now=NOW)
    assert caught.value.reason == "branch-exists"
    _git(app_repo, "branch", "-m", "main", "trunk")
    with pytest.raises(delivery.DeliveryError) as caught:
        delivery.cycle_start(ir.load(layer_dir), name="fresh", now=NOW)
    assert caught.value.reason == "source-missing"
    assert delivery.load_cycle(ir.load(layer_dir)) is None


def test_cycle_close_reports_accepted_and_never_merges(make_layer, app_repo):
    layer_dir = _sprint_layer(make_layer, app_repo)
    _open_cycle(layer_dir, app_repo)
    worktree = app_repo.parent / "sprint-wt"
    _git(app_repo, "worktree", "add", str(worktree), "sprint/2026-s47")
    (worktree / "s.txt").write_text("sprint work\n")
    _git(worktree, "add", "s.txt")
    _git(worktree, "commit", "-m", "sprint work")
    main = _git(app_repo, "rev-parse", "refs/heads/main")
    closed = delivery.cycle_close(ir.load(layer_dir), now=NOW)
    assert closed["closed"]["accepted"] is False and "merges sprint/2026-s47 into main" in closed["next"]
    assert _git(app_repo, "rev-parse", "refs/heads/main") == main
    with pytest.raises(delivery.DeliveryError) as caught:
        delivery.cycle_close(ir.load(layer_dir), now=NOW)
    assert caught.value.reason == "cycle-not-open"
    _open_cycle(layer_dir, app_repo, name="2026-s48")
    _git(app_repo, "merge", "--no-ff", "-m", "accept", "sprint/2026-s48")   # a person accepts it
    assert delivery.cycle_close(ir.load(layer_dir), now=NOW)["closed"]["accepted"] is True


def test_cycle_status_reports_ahead_behind_and_days_left(make_layer, app_repo):
    layer_dir = _sprint_layer(make_layer, app_repo)
    _open_cycle(layer_dir, app_repo)
    (app_repo / "late.txt").write_text("on main after the cut\n")
    _git(app_repo, "add", "late.txt")
    _git(app_repo, "commit", "-m", "late")
    status = delivery.cycle_status(ir.load(layer_dir), now=datetime(2026, 11, 20, tzinfo=timezone.utc))
    assert status["open"] and status["days_left"] == 10
    assert status["ahead"] == 0 and status["behind"] == 1
    lines = delivery.status_lines(status)
    assert lines[0] == "cycle: sprint 2026-s47 open since 2026-11-16 (14 days, 10 left)"
    assert lines[1].startswith("branch: sprint/2026-s47 (from main at ")
    assert lines[2] == "ahead of main: 0 commits · behind main: 1 commits"


def test_resume_keeps_the_recorded_target(make_layer, app_repo):
    layer_dir = _sprint_layer(make_layer, app_repo)
    _open_cycle(layer_dir, app_repo)
    layer = ir.load(layer_dir)
    state = _state(klass="feature")
    target = delivery.for_run(layer, layer.adw("demo"), state, env={})
    state.delivery = delivery.record(target)
    _open_cycle(layer_dir, app_repo, name="2026-s48")
    again = delivery.for_run(ir.load(layer_dir), layer.adw("demo"), state, env={})
    assert again.branch == "sprint/2026-s47"


def test_conflict_detection_uses_the_fetched_remote_head(make_layer, app_repo, tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    layer_dir = _sprint_layer(make_layer, app_repo, forge_option={"id": "none", "kind": "none", "status": "stable",
                                                                   "remote": str(remote)})
    _open_cycle(layer_dir, app_repo)
    _git(app_repo, "push", str(remote), "refs/heads/sprint/2026-s47:refs/heads/sprint/2026-s47")
    layer = ir.load(layer_dir)
    scoped = delivery.scope(layer, delivery.for_run(layer, layer.adw("demo"), _state(klass="feature"), env={}))
    worktree = tmp_path / "run"
    _git(app_repo, "worktree", "add", "-b", "feature/run", str(worktree), "sprint/2026-s47")
    (worktree / "app.py").write_text("print('run')\n")
    _git(worktree, "commit", "-am", "run change")
    # the remote's sprint branch moved (the local ref did not): the same line changed
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", "-b", "sprint/2026-s47", str(remote), str(other)], check=True, capture_output=True)
    (other / "app.py").write_text("print('remote')\n")
    _git(other, "commit", "-am", "remote change")
    _git(other, "push", "-q", "origin", "HEAD:refs/heads/sprint/2026-s47")
    state = _state(klass="feature", worktree=str(worktree))
    env_stack = SimpleNamespace(sandbox=None)
    head = delivery._stage_head(scoped, state, env_stack)
    assert head == _git(other, "rev-parse", "HEAD") != _git(app_repo, "rev-parse", "refs/heads/sprint/2026-s47")
    assert delivery._conflicts(state, env_stack, head) == ["app.py"]
    # an older git without `merge-tree --write-tree`: the trial merge finds the same and leaves no trace
    monkeypatch.setattr(delivery, "_merge_tree", lambda *a: SimpleNamespace(returncode=129, stdout="", stderr="unknown option"))
    assert delivery._conflicts(state, env_stack, head) == ["app.py"]
    assert _git(worktree, "status", "--porcelain") == ""


def test_delivery_refers_to_no_private_name_of_review():
    import ast

    tree = ast.parse(Path(delivery.__file__).read_text(encoding="utf-8"))
    attributes = sorted({node.attr for node in ast.walk(tree)
                         if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                         and node.value.id == "review" and node.attr.startswith("_")})
    imported = sorted({alias.name for node in ast.walk(tree)
                       if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == "review"
                       for alias in node.names if alias.name.startswith("_")})
    assert not attributes and not imported, (
        f"delivery.py refers to private names of review: {attributes + imported}; "
        "review.py is protected; use delivery's own helpers")
