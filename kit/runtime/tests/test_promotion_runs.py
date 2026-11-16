"""Full promotion runs (runner.main through promotion.main, mock agent, real git, temporary
repositories, no podman unless marked). The application's checkout is left on a branch of its own,
so main and the sprint are not checked out, except where a test says otherwise."""
from __future__ import annotations

import json
import re
from pathlib import Path

from adw_modules import ir
from promotion_fixtures import (SPRINT, decide, deliver, events_of, git, open_id, promote, sprint_layer, state_of)


def _three(layer, app):
    deliver(layer, app, "a1000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart",
            criteria=["The cart holds items", "test passes"], shots=["cart.png"])
    deliver(layer, app, "a1000002", {"search.py": "SEARCH = 1\n"}, prompt="Add search",
            criteria=["Search finds items"])
    deliver(layer, app, "a1000003", {"login.py": "LOGIN = 1\n"}, prompt="Add login", criteria=None)


def test_sprint_of_three_changes_promoted_after_acceptance(make_layer, app_repo):
    layer = sprint_layer(make_layer, app_repo)
    _three(layer, app_repo)
    symbolic = git(app_repo, "symbolic-ref", "HEAD")
    code, out = promote(layer, "sprint", "main")
    assert code == 75, out
    pid = open_id(layer)
    assert re.search(rf"^promotion {pid}: {SPRINT} -> main \(merge; 1 human approval\)$", out, re.M), out
    assert "changes: 3 delivered since the cycle opened (3 to decide)" in out
    assert re.search(rf"^checks on {SPRINT} at [0-9a-f]{{7}}: test=0$", out, re.M)
    assert f"pack: artefacts/{pid}/pack/index.html" in out
    assert f"approval item: promotions/{pid}/approval.md" in out
    assert "waiting: 1 human approval missing; 3 changes undecided" in out
    state = state_of(layer, pid)
    assert state["status"] == "waiting"
    assert next(s for s in state["steps"] if s["name"] == "approval")["status"] == "waiting"
    assert (layer / "promotions" / pid / "approval.md").is_file()
    assert (layer / "artefacts" / pid / "pack" / "index.html").is_file()
    code, out = decide(layer, "sprint", "main", "approve", "--by", "Ada Example")
    assert code == 0, out
    code, out = promote(layer, "sprint", "main")
    assert code == 0, out
    assert f"continuing promotion {pid} (opened " in out
    state = state_of(layer, pid)
    assert state["status"] == "passed"
    assert all(s["status"] == "passed" for s in state["steps"]), state["steps"]
    main_head = git(app_repo, "rev-parse", "refs/heads/main")
    parents = git(app_repo, "rev-list", "--parents", "-n", "1", main_head).split()
    assert len(parents) == 3 and parents[2] == state["promotion"]["promoted"]
    assert git(app_repo, "merge-base", "--is-ancestor", state["promotion"]["pinned"], main_head, check=False) == ""
    for name in ("cart.py", "search.py", "login.py"):
        assert git(app_repo, "show", f"{main_head}:{name}")
    assert git(app_repo, "show", f"{main_head}:VERSION") == "0.2.0"
    assert git(app_repo, "rev-parse", "v0.2.0^{commit}") == main_head
    changelog = git(app_repo, "show", f"{main_head}:CHANGELOG.md")
    entry = re.search(r"## \[0\.2\.0\] - \d{4}-\d{2}-\d{2}\n\n### Added\n\n((?:- .*\n?)+)", changelog)
    assert entry and len(entry.group(1).strip().splitlines()) == 3, changelog
    assert changelog.index("## [Unreleased]") < changelog.index("## [0.2.0]") < changelog.index("## [0.1.0]")
    for name in ("acceptance.jsonl", "sign-off.md", "promotion.json"):
        assert (layer / "promotions" / pid / name).is_file(), name
    cycle = json.loads((layer / "agents" / "cycle.json").read_text())
    assert cycle["current"] is None and cycle["history"][-1]["accepted"] is True
    types = {e["type"] for e in events_of(layer, pid)}
    assert {"promotion.opened", "promotion.waiting", "promotion.decision", "promotion.approved", "promotion.merged",
            "promotion.tagged", "promotion.closed"} <= types
    assert "promotion: " in (layer / "agents" / pid / "run.log").read_text()
    assert git(app_repo, "symbolic-ref", "HEAD") == symbolic and git(app_repo, "status", "--porcelain") == ""
    assert not (layer / "agents" / "promotions" / "sprint-main.json").exists()


def _approve(layer, who="Ada Example", pair=("sprint", "main")):
    code, out = decide(layer, *pair, "approve", "--by", who)
    assert code == 0, out


def test_refused_change_is_reverted_out_and_absent_from_main(make_layer, app_repo):
    layer = sprint_layer(make_layer, app_repo)
    _three(layer, app_repo)
    assert promote(layer, "sprint", "main")[0] == 75
    pid = open_id(layer)
    code, out = decide(layer, "sprint", "main", "refuse", "a1000002", "--then", "revert", "--by", "Ada Example",
                       "--comment", "not this sprint")
    assert code == 0, out
    _approve(layer)
    code, out = promote(layer, "sprint", "main")
    assert code == 0, out
    assert f'excluded: a1000002 reverted out of {SPRINT} (refused by Ada Example: "not this sprint")' in out
    assert "keepachangelog entry with 2 changes" in out
    log = git(app_repo, "log", "--format=%s", SPRINT)
    assert f"Revert a1000002 out of {SPRINT}: refused by Ada Example [promotion {pid}]" in log
    main_head = git(app_repo, "rev-parse", "refs/heads/main")
    assert git(app_repo, "cat-file", "-e", f"{main_head}:search.py", check=False) == ""
    assert "search.py" not in git(app_repo, "ls-tree", "--name-only", main_head)
    assert "SEARCH" not in git(app_repo, "grep", "-I", "SEARCH", main_head, check=False)
    changelog = git(app_repo, "show", f"{main_head}:CHANGELOG.md")
    assert "- Add a cart" in changelog and "- Add login" in changelog and "- Add search" not in changelog
    pack = (layer / "artefacts" / pid / "pack" / "index.html").read_text()
    excluded = pack.split("<h2>Not included</h2>", 1)[1]
    assert "a1000002" in excluded and "Ada Example" in excluded and "not this sprint" in excluded
    state = state_of(layer, pid)
    refused = next(c for c in state["promotion"]["changes"] if c["id"] == "a1000002")
    assert refused["status"] == "refused" and refused["excluded"] == "reverted"


def test_refused_change_sent_back_for_a_patch_round(make_layer, app_repo, monkeypatch):
    from adw_modules import launch, promotion

    started = []

    def fake_start(layer, target, prompt, **kwargs):
        started.append({"adw": target.name, "kind": target.kind, "prompt": prompt, **kwargs})
        return {"status": "started", "run_id": "feedf00d"}

    monkeypatch.setattr(launch, "start", fake_start)
    layer = sprint_layer(make_layer, app_repo)
    deliver(layer, app_repo, "b1000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    deliver(layer, app_repo, "b1000002", {"search.py": "SEARCH = 1\n"}, prompt="Add search")
    assert promote(layer, "sprint", "main")[0] == 75
    pid = open_id(layer)
    assert decide(layer, "sprint", "main", "refuse", "b1000002", "--then", "patch", "--by", "Ada Example",
                  "--comment", "search must ignore case")[0] == 0
    _approve(layer)
    code, out = promote(layer, "sprint", "main")
    assert code == 0, out
    assert len(started) == 1
    call = started[0]
    assert call["adw"] == "demo" and call["kind"] == "workflow" and call["klass"] == "patch"
    assert call["request_key"] == f"promotion-{pid}-b1000002"
    change = next(c for c in state_of(layer, pid)["promotion"]["changes"] if c["id"] == "b1000002")
    assert "search must ignore case" in call["prompt"] and change["revert"] in call["prompt"]
    assert change["excluded"] == "patch" and change["patch_run"] == {"run_id": "feedf00d", "status": "started"}
    assert "search.py" not in git(app_repo, "ls-tree", "--name-only", "refs/heads/main")
    assert promotion.CHANGE_ID.match(change["id"])


def test_revert_that_does_not_apply_runs_an_agent_round(make_layer, app_repo):
    from promotion_fixtures import script

    layer = sprint_layer(make_layer, app_repo)
    deliver(layer, app_repo, "c1000001", {"app.py": "print('hello')\nA_FEATURE = 1\n"}, prompt="Add feature A")
    deliver(layer, app_repo, "c1000002", {"app.py": "print('hello')\nA_FEATURE = 2\nB_FEATURE = 1\n"},
            prompt="Add feature B and tune A")
    assert promote(layer, "sprint", "main")[0] == 75
    script(layer, "revert-c1000001-patch", {"output": "Remove A_FEATURE, keep B_FEATURE."})
    script(layer, "revert-c1000001-implement", {"files": {"app.py": "print('hello')\nB_FEATURE = 1\n"}})
    assert decide(layer, "sprint", "main", "refuse", "c1000001", "--by", "Ada Example", "--comment", "no A")[0] == 0
    _approve(layer)
    code, out = promote(layer, "sprint", "main")
    assert code == 0, out
    app_py = git(app_repo, "show", "refs/heads/main:app.py")
    assert "A_FEATURE" not in app_py and "B_FEATURE = 1" in app_py
    pid = open_id_or_closed(layer)
    assert "revert-c1000001-implement" in state_of(layer, pid)["outputs"]


def open_id_or_closed(layer):
    found = sorted((layer / "promotions").glob("*/promotion.json"))
    return found[-1].parent.name


GITFLOW = [
    {"name": "develop", "branch": "develop", "from": "main", "into": "", "accepts": ["chore", "feature"],
     "agentsMayMerge": True, "entry": "merge", "checks": [], "deployCmd": "", "environmentName": "",
     "origin": "declared", "frozen": [], "frozenWhilePromoting": False},
    {"name": "hotfix", "branch": "hotfix/{date}", "from": "main", "into": "main", "accepts": ["bug"],
     "agentsMayMerge": True, "entry": "merge", "checks": [], "deployCmd": "", "environmentName": "",
     "origin": "declared", "frozen": [], "frozenWhilePromoting": False},
    {"name": "main", "branch": "main", "from": "", "into": "", "accepts": [], "agentsMayMerge": False,
     "entry": "merge", "checks": [], "deployCmd": "", "environmentName": "", "origin": "declared", "frozen": [],
     "frozenWhilePromoting": False},
]


def _promo(from_, to, **extra):
    return {"from": from_, "to": to, "approval": "human", "approvers": 1, "requires": ["checks", "acceptance"],
            "method": "merge", "version": None, "tag": "", "after": [], "backMerge": [], "adw": "demo",
            "present": ["summary", "changes", "proof", "demo", "checks", "notes", "excluded"], "hotfix": False,
            "origin": "declared", **extra}


def _gitflow_layer(make_layer, app_repo, **hotfix):
    from datetime import datetime, timezone

    layer = sprint_layer(make_layer, app_repo, stages=GITFLOW, extra={"cycle": None, "delivery": None},
                         promotions=[_promo("develop", "main"),
                                     _promo("hotfix", "main", hotfix=True, backMerge=["develop"], version={
                                         "how": "bump", "level": "patch"}, tag="v{version}", **hotfix)],
                         adws=[delivering_adw(), delivering_adw("fix", deliverTo="hotfix", classes=["bug"])])
    git(app_repo, "branch", "develop", "main")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    git(app_repo, "branch", f"hotfix/{today}", "main")
    return layer, f"hotfix/{today}"


from promotion_fixtures import commit_on, delivering_adw  # noqa: E402


def test_back_merge_without_a_conflict(make_layer, app_repo):
    layer, hotfix = _gitflow_layer(make_layer, app_repo)
    commit_on(app_repo, "develop", {"develop.py": "DEV = 1\n"})
    deliver(layer, app_repo, "d1000001", {"fix.py": "FIX = 1\n"}, prompt="Fix the crash", klass="bug", adw="fix")
    assert git(app_repo, "show", f"{hotfix}:fix.py") == "FIX = 1"
    assert promote(layer, "hotfix", "main")[0] == 75
    _approve(layer, pair=("hotfix", "main"))
    code, out = promote(layer, "hotfix", "main")
    assert code == 0, out
    assert git(app_repo, "show", "refs/heads/main:fix.py") == "FIX = 1"
    assert git(app_repo, "show", "refs/heads/develop:fix.py") == "FIX = 1"
    assert git(app_repo, "show", "refs/heads/develop:develop.py") == "DEV = 1"
    parents = git(app_repo, "rev-list", "--parents", "-n", "1", "refs/heads/develop").split()
    assert len(parents) == 3
    assert git(app_repo, "rev-parse", "v0.1.1^{commit}") == git(app_repo, "rev-parse", "refs/heads/main")


def test_back_merge_with_a_conflict_runs_an_agent_round(make_layer, app_repo):
    from promotion_fixtures import script

    layer, _hotfix = _gitflow_layer(make_layer, app_repo)
    commit_on(app_repo, "develop", {"app.py": "print('develop')\n"})
    deliver(layer, app_repo, "d2000001", {"app.py": "print('fixed')\n"}, prompt="Fix the greeting", klass="bug",
            adw="fix")
    script(layer, "back-merge-develop-patch", {"output": "Keep both."})
    script(layer, "back-merge-develop-implement", {"files": {"app.py": "print('fixed in develop')\n"}})
    assert promote(layer, "hotfix", "main")[0] == 75
    pid = open_id(layer, "hotfix", "main")
    _approve(layer, pair=("hotfix", "main"))
    code, out = promote(layer, "hotfix", "main")
    assert code == 0, out
    assert git(app_repo, "show", "refs/heads/develop:app.py") == "print('fixed in develop')"
    assert len(git(app_repo, "rev-list", "--parents", "-n", "1", "refs/heads/develop").split()) == 3
    merged = [e for e in events_of(layer, pid) if e["type"] == "promotion.back-merge"]
    assert merged and merged[0]["conflict"] is True and merged[0]["resolved"] is True


def test_hotfix_needs_its_own_two_approvals(make_layer, app_repo):
    layer, _hotfix = _gitflow_layer(make_layer, app_repo, approvers=2)
    deliver(layer, app_repo, "d3000001", {"fix.py": "FIX = 1\n"}, prompt="Fix the crash", klass="bug", adw="fix")
    assert promote(layer, "hotfix", "main")[0] == 75
    _approve(layer, "Ada Example", ("hotfix", "main"))
    code, out = promote(layer, "hotfix", "main")
    assert code == 75 and "1 of 2" in out, out
    _approve(layer, "Grace Example", ("hotfix", "main"))
    code, out = promote(layer, "hotfix", "main")
    assert code == 0, out
    assert git(app_repo, "show", "refs/heads/main:fix.py") == "FIX = 1"
    assert git(app_repo, "rev-parse", "v0.1.1^{commit}")
    assert git(app_repo, "show", "refs/heads/develop:fix.py") == "FIX = 1"


def _rewrite_ir(layer, change):
    path = layer / "agentic-layer.json"
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data, indent=2))


def test_freeze_window_makes_a_run_wait_and_say_why(make_layer, app_repo):
    from datetime import datetime, timedelta, timezone

    from adw_modules.runner import main as run_main

    today = datetime.now(timezone.utc).date()
    period = f"{today - timedelta(days=1)}..{today + timedelta(days=1)}"
    layer = sprint_layer(make_layer, app_repo)
    _rewrite_ir(layer, lambda d: d["stages"][0].update(frozen=[period]))
    before = git(app_repo, "rev-parse", f"refs/heads/{SPRINT}")
    state = deliver(layer, app_repo, "e1000001", {"late.py": "LATE = 1\n"}, prompt="Add late work", expect=75)
    assert state["status"] == "waiting"
    deliver_step = next(s for s in state["steps"] if s["name"] == "deliver")
    assert deliver_step["status"] == "waiting" and period.replace("..", " to ") in deliver_step["detail"]
    assert any(e["type"] == "delivery.frozen" for e in events_of(layer, "e1000001"))
    assert git(app_repo, "rev-parse", f"refs/heads/{SPRINT}") == before
    _rewrite_ir(layer, lambda d: d["stages"][0].update(frozen=["2020-01-01..2020-01-02"]))
    assert run_main("demo", layer, ["--resume", "--adw-id", "e1000001"]) == 0
    assert git(app_repo, "show", f"{SPRINT}:late.py") == "LATE = 1"


def test_freeze_while_a_promotion_is_open(make_layer, app_repo):
    from adw_modules.runner import main as run_main

    layer = sprint_layer(make_layer, app_repo)
    _rewrite_ir(layer, lambda d: d["stages"][0].update(frozenWhilePromoting=True))
    deliver(layer, app_repo, "f1000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    assert promote(layer, "sprint", "main")[0] == 75
    pid = open_id(layer)
    state = deliver(layer, app_repo, "f1000002", {"late.py": "LATE = 1\n"}, prompt="Add late work", expect=75)
    detail = next(s for s in state["steps"] if s["name"] == "deliver")["detail"]
    assert f"promotion {pid} into main is open" in detail
    _approve(layer)
    code, out = promote(layer, "sprint", "main")
    assert code == 0, out
    assert "uv run adws/adw_demo.py --resume --adw-id f1000002" in out
    assert state_of(layer, pid)["promotion"]["waiting_runs"][0]["id"] == "f1000002"
    assert run_main("demo", layer, ["--resume", "--adw-id", "f1000002"]) == 0
    assert git(app_repo, "show", f"{SPRINT}:late.py") == "LATE = 1"


def test_dry_run_changes_nothing(make_layer, app_repo):
    from promotion_fixtures import refs

    layer = sprint_layer(make_layer, app_repo)
    _three(layer, app_repo)
    before, status = refs(app_repo), git(app_repo, "status", "--porcelain")
    code, out = promote(layer, "sprint", "main", "--dry-run")
    assert code == 0, out
    assert refs(app_repo) == before and git(app_repo, "status", "--porcelain") == status
    assert not (layer / "promotions").exists()
    assert not (layer / "agents" / "promotions" / "sprint-main.json").exists()
    assert "would merge" in out and "would set version 0.2.0 in VERSION" in out and "would tag v0.2.0" in out
    assert list((layer / "artefacts").glob("*/pack/index.html"))


def test_approval_rules_count_people(make_layer, app_repo):
    layer = sprint_layer(make_layer, app_repo, promotion={"approvers": 2})
    deliver(layer, app_repo, "9a000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    assert promote(layer, "sprint", "main")[0] == 75
    _approve(layer, "Ada Example")
    _approve(layer, "Ada Example")
    code, out = promote(layer, "sprint", "main")
    assert code == 75 and "1 human approval missing (1 of 2 given)" in out, out
    _approve(layer, "Grace Example")
    assert promote(layer, "sprint", "main")[0] == 0


DEV_QA = [
    {"name": "dev", "branch": "dev", "from": "main", "into": "qa", "accepts": ["chore", "bug", "feature"],
     "agentsMayMerge": True, "entry": "merge", "checks": [], "deployCmd": "", "environmentName": "",
     "origin": "declared", "frozen": [], "frozenWhilePromoting": False},
    {"name": "qa", "branch": "qa", "from": "main", "into": "main", "accepts": [], "agentsMayMerge": True,
     "entry": "merge", "checks": [], "deployCmd": "", "environmentName": "", "origin": "declared", "frozen": [],
     "frozenWhilePromoting": False},
    GITFLOW[2],
]


def _dev_qa_layer(make_layer, app_repo):
    layer = sprint_layer(make_layer, app_repo, stages=DEV_QA, extra={"cycle": None, "delivery": None},
                         promotions=[_promo("dev", "qa", approval="agent", approvers=2), _promo("qa", "main")])
    git(app_repo, "branch", "dev", "main")
    git(app_repo, "branch", "qa", "main")
    return layer


def test_agent_approvers_on_an_intermediate_promotion(make_layer, app_repo):
    from promotion_fixtures import script

    layer = _dev_qa_layer(make_layer, app_repo)
    deliver(layer, app_repo, "9b000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    for n in (1, 2):
        script(layer, f"review-{n}", {"output": json.dumps({"success": True, "summary": f"ok {n}", "issues": []})},
               "promote-dev-qa")
    code, out = promote(layer, "dev", "qa")
    assert code == 0, out
    assert git(app_repo, "show", "refs/heads/qa:cart.py") == "CART = 1"
    pid = open_id_or_closed(layer)
    records = [json.loads(line) for line in (layer / "promotions" / pid / "acceptance.jsonl").read_text().splitlines()]
    assert [(r["by"], r["kind"], r["decision"]) for r in records] == [
        ("agent:review-1", "agent", "approve"), ("agent:review-2", "agent", "approve")]
    deliver(layer, app_repo, "9b000002", {"search.py": "SEARCH = 1\n"}, prompt="Add search")
    qa = git(app_repo, "rev-parse", "refs/heads/qa")
    script(layer, "review-2", {"output": json.dumps({"success": False, "summary": "no", "issues": [
        {"severity": "blocker", "description": "search is untested", "resolution": "add a test"}]})},
           "promote-dev-qa")
    code, out = promote(layer, "dev", "qa")
    assert code == 1, out
    assert git(app_repo, "rev-parse", "refs/heads/qa") == qa
    pid = open_id(layer, "dev", "qa")
    state = state_of(layer, pid)
    assert state["status"] == "failed" and "agents-refused" in state["promotion"]["stopped"]["reason"]
    assert "search is untested" in state["promotion"]["stopped"]["reason"]


def test_agents_are_never_approvers_into_a_protected_stage(make_layer, app_repo):
    from promotion_fixtures import refs

    layer = sprint_layer(make_layer, app_repo, promotion={"approval": "agent"})
    deliver(layer, app_repo, "9c000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    before = refs(app_repo)
    code, out = promote(layer, "sprint", "main")
    assert code == 1 and "agents-not-allowed" in out, out
    assert refs(app_repo) == before
    assert not (layer / "agents" / "promotions" / "sprint-main.json").exists()


def _with_origin(layer, app_repo, tmp_path, monkeypatch):
    from adw_modules import forge

    from promotion_fixtures import FakeForgejoPulls

    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "-q", "--bare", str(app_repo), str(remote))
    git(app_repo, "remote", "add", "origin", str(remote))
    git(app_repo, "push", "-q", "origin", "main", SPRINT)
    fake = FakeForgejoPulls(remote, tmp_path)
    monkeypatch.setattr(forge, "http_request", fake)
    _rewrite_ir(layer, lambda d: d.update(forge={"id": "forgejo", "kind": "forgejo", "status": "stable",
                                                 "host": "https://forge.example.org", "repo": "acme/webshop",
                                                 "tokenEnv": "FORGEJO_TOKEN", "botMarker": "[ADW-BOT]"}))
    return fake, remote


def test_promotion_through_the_fake_forge(make_layer, app_repo, tmp_path, monkeypatch):
    layer = sprint_layer(make_layer, app_repo)
    _three(layer, app_repo)
    fake, remote = _with_origin(layer, app_repo, tmp_path, monkeypatch)
    code, out = promote(layer, "sprint", "main")
    assert code == 75, out
    pid = open_id(layer)
    assert list(fake.pulls) == [1]
    pull = fake.pulls[1]
    assert pull["head"] == SPRINT and pull["base"] == "main"
    assert pull["body"] == (layer / "artefacts" / pid / "pack" / "summary.md").read_text()
    assert any("Proof from run" in c["body"] for c in fake.comments[1]) and fake.assets
    fake.person_comments(1, "ada", "/refuse a1000002 revert not ready")
    fake.approve(1, "bob")
    code, out = promote(layer, "sprint", "main")
    assert code == 0, out
    assert fake.merges == [{"number": 1, "method": "merge"}]
    records = [json.loads(line) for line in (layer / "promotions" / pid / "acceptance.jsonl").read_text().splitlines()]
    assert [(r["by"], r["kind"], r["decision"], r["comment"]) for r in records] == [
        ("ada", "forge", "refuse", "not ready")]
    assert records[0]["source"].startswith("comment:")
    main = git(remote, "rev-parse", "refs/heads/main")
    assert "search.py" not in git(remote, "ls-tree", "--name-only", main)
    assert "cart.py" in git(remote, "ls-tree", "--name-only", main)
    assert state_of(layer, pid)["status"] == "passed"


import pytest  # noqa: E402


@pytest.mark.parametrize("method", ["squash", "fast-forward", "cherry-pick"])
def test_promotion_methods(make_layer, app_repo, method):
    layer = sprint_layer(make_layer, app_repo, promotion={"method": method})
    deliver(layer, app_repo, "9d000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    deliver(layer, app_repo, "9d000002", {"search.py": "SEARCH = 1\n"}, prompt="Add search")
    before = git(app_repo, "rev-parse", "refs/heads/main")
    assert promote(layer, "sprint", "main")[0] == 75
    _approve(layer)
    code, out = promote(layer, "sprint", "main")
    assert code == 0, out
    head = git(app_repo, "rev-parse", "refs/heads/main")
    pid = open_id_or_closed(layer)
    promoted = state_of(layer, pid)["promotion"]["promoted"]
    new = git(app_repo, "rev-list", f"{before}..{head}").split()
    if method == "squash":
        assert len(new) == 1 and len(git(app_repo, "rev-list", "--parents", "-n", "1", head).split()) == 2
    elif method == "fast-forward":
        assert head == promoted
    else:
        assert all(len(git(app_repo, "rev-list", "--parents", "-n", "1", c).split()) == 2 for c in new)
        assert "cherry picked from commit" in git(app_repo, "log", "-1", "--format=%B", head)
        assert head != promoted
    for name in ("cart.py", "search.py"):
        assert git(app_repo, "show", f"{head}:{name}")
    assert git(app_repo, "show", f"{head}:VERSION") == "0.2.0"


def test_fast_forward_is_refused_when_main_moved(make_layer, app_repo):
    layer = sprint_layer(make_layer, app_repo, promotion={"method": "fast-forward"})
    deliver(layer, app_repo, "9e000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    commit_on(app_repo, "main", {"other.py": "OTHER = 1\n"})
    assert promote(layer, "sprint", "main")[0] == 75
    _approve(layer)
    code, out = promote(layer, "sprint", "main")
    assert code == 1 and "not-fast-forward" in out, out


def test_checked_out_target_goes_to_a_staging_ref(make_layer, app_repo):
    layer = sprint_layer(make_layer, app_repo, leave_main=False)
    deliver(layer, app_repo, "9f000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    main_before = git(app_repo, "rev-parse", "refs/heads/main")
    assert promote(layer, "sprint", "main")[0] == 75
    _approve(layer)
    code, out = promote(layer, "sprint", "main")
    assert code == 0, out
    assert git(app_repo, "rev-parse", "refs/heads/main") == main_before
    assert git(app_repo, "show", "al/promote/main:cart.py") == "CART = 1"
    assert "git merge --ff-only al/promote/main" in out
    pid = open_id_or_closed(layer)
    assert "al/promote/main" in (layer / "agents" / pid / "run.log").read_text()


def _deploy_layer(make_layer, app_repo, script_text):
    deploy = app_repo / "deploy.sh"
    deploy.write_text(script_text, encoding="utf-8")
    deploy.chmod(0o755)
    stages = json.loads((Path(__file__).parent / "data" / "delivery-sprint.json").read_text())["stages"]
    stages[1] = {**stages[1], "deployCmd": "deploy", "environmentName": "production"}
    return sprint_layer(make_layer, app_repo, stages=stages, promotion={"after": ["deploy", "production"]})


def test_deploy_after_the_promotion(make_layer, app_repo, tmp_path, monkeypatch):
    marker = tmp_path / "deployed.txt"
    monkeypatch.setenv("DEPLOY_MARKER", str(marker))
    layer = _deploy_layer(make_layer, app_repo, '#!/bin/sh\ncp VERSION "$DEPLOY_MARKER"\n')
    deliver(layer, app_repo, "8a000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    assert promote(layer, "sprint", "main")[0] == 75
    pid = open_id(layer)
    _approve(layer)
    code, out = promote(layer, "sprint", "main")
    assert code == 0, out
    assert marker.read_text().strip() == "0.2.0"
    deployed = [e for e in events_of(layer, pid) if e["type"] == "promotion.deployed"]
    assert deployed == [{**deployed[0], "environment": "production", "exit": 0}]


def test_failing_deploy_keeps_merge_and_tag(make_layer, app_repo):
    layer = _deploy_layer(make_layer, app_repo, "#!/bin/sh\nexit 3\n")
    deliver(layer, app_repo, "8b000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    assert promote(layer, "sprint", "main")[0] == 75
    pid = open_id(layer)
    _approve(layer)
    code, out = promote(layer, "sprint", "main")
    assert code == 1 and "promotion:deploy-failed" in out, out
    assert git(app_repo, "show", "refs/heads/main:cart.py") == "CART = 1"
    assert git(app_repo, "rev-parse", "v0.2.0^{commit}")
    assert state_of(layer, pid)["promotion"]["stopped"]["step"] == "deploy"


def test_nothing_to_promote_is_said_and_changes_nothing(make_layer, app_repo):
    from promotion_fixtures import refs

    layer = sprint_layer(make_layer, app_repo)
    before = refs(app_repo)
    code, out = promote(layer, "sprint", "main")
    assert code == 1 and "promotion:nothing-to-promote" in out, out
    assert refs(app_repo) == before
    assert not (layer / "agents" / "promotions" / "sprint-main.json").exists()


def test_delivery_during_an_open_promotion_waits_for_the_next(make_layer, app_repo):
    layer = sprint_layer(make_layer, app_repo)
    deliver(layer, app_repo, "8c000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    assert promote(layer, "sprint", "main")[0] == 75
    pid = open_id(layer)
    deliver(layer, app_repo, "8c000002", {"late.py": "LATE = 1\n"}, prompt="Add late work")
    _approve(layer)
    assert promote(layer, "sprint", "main")[0] == 0
    assert [c["id"] for c in state_of(layer, pid)["promotion"]["changes"]] == ["8c000001"]
    assert "late.py" not in git(app_repo, "ls-tree", "--name-only", "refs/heads/main")
    code, out = promote(layer, "sprint", "main", "--branch", SPRINT)
    assert code == 75, out
    nxt = open_id(layer)
    assert nxt != pid
    assert [c["id"] for c in state_of(layer, nxt)["promotion"]["changes"]] == ["8c000002"]
    assert f"since promotion {pid}" in out


def test_all_refused_merges_nothing(make_layer, app_repo):
    layer = sprint_layer(make_layer, app_repo)
    deliver(layer, app_repo, "8d000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    deliver(layer, app_repo, "8d000002", {"search.py": "SEARCH = 1\n"}, prompt="Add search")
    main_before = git(app_repo, "rev-parse", "refs/heads/main")
    assert promote(layer, "sprint", "main")[0] == 75
    assert decide(layer, "sprint", "main", "refuse", "8d000001", "8d000002", "--by", "Ada Example")[0] == 0
    _approve(layer)
    code, out = promote(layer, "sprint", "main")
    assert code == 0, out
    assert "merged: nothing" in out
    assert git(app_repo, "rev-parse", "refs/heads/main") == main_before
    assert git(app_repo, "tag", "--list") == ""


def test_existing_tag_stops_before_merging(make_layer, app_repo):
    layer = sprint_layer(make_layer, app_repo)
    deliver(layer, app_repo, "8e000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    git(app_repo, "tag", "v0.2.0", "main")
    main_before = git(app_repo, "rev-parse", "refs/heads/main")
    code, out = promote(layer, "sprint", "main")
    assert code == 1 and "promotion:tag-exists" in out, out
    assert git(app_repo, "rev-parse", "refs/heads/main") == main_before


@pytest.mark.podman
def test_sandboxed_promotion(make_layer, app_repo, monkeypatch):
    import shutil
    import subprocess

    if shutil.which("podman") is None or subprocess.run(["podman", "info"], capture_output=True).returncode != 0:
        pytest.skip("podman is not installed or not usable here")
    from test_sandbox_podman import BASES, TEST_IMAGE, _podman
    from adw_modules import envs

    if _podman("image", "exists", TEST_IMAGE).returncode != 0:
        pytest.skip(f"the sandbox test image {TEST_IMAGE} is not built (see test_sandbox_podman, bases {BASES})")
    monkeypatch.setattr(envs.EnvStack, "image", property(lambda self: TEST_IMAGE))
    layer = sprint_layer(make_layer, app_repo, extra={"environments": [
        {"id": "worktree", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 27600, "portsPerRun": 0,
         "keep": "never", "status": "stable"},
        {"id": "podman", "kind": "podman", "agentInside": False, "baseImage": "debian", "cpus": "1", "memory": "1g",
         "network": "none", "packages": [], "setup": [], "status": "stable", "scope": "run", "caches": [],
         "credentials": []}]},
                         adws=[delivering_adw(environment=["worktree", "podman"])])
    (layer / "env").mkdir(exist_ok=True)
    (layer / "env" / "Containerfile").write_text(f"FROM {TEST_IMAGE}\n")   # the preflight builds it: nothing pulled
    deliver(layer, app_repo, "8f000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    assert promote(layer, "sprint", "main")[0] == 75
    _approve(layer)
    assert promote(layer, "sprint", "main")[0] == 0
    assert git(app_repo, "show", "refs/heads/main:cart.py") == "CART = 1"
