"""Unit tests of promotion.py: changes, criteria, proof, decisions, approvals, versions, changelog,
tags, freezes, branch resolution, the presentation pack and forge approvals. The stages and the
promotion are the compiler's real output (data/delivery-sprint.json)."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from adw_modules import delivery, forge, ir, promotion
from adw_modules.state import ADWState
from promotion_fixtures import (KEEPACHANGELOG, NOW, SPRINT, commit_on, deliver, git, open_id, promote, sprint_layer,
                                state_of)


def _state(layer: Path, adw_id: str, **data) -> None:
    path = layer / "agents" / adw_id / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"adw_id": adw_id, "adw": "demo", "prompt": f"request {adw_id}", **data}))


def test_changes_are_the_runs_delivered_into_the_stage_and_not_yet_promoted(make_layer, app_repo):
    layer = sprint_layer(make_layer, app_repo)
    git(app_repo, "branch", "other", "main")
    base = git(app_repo, "rev-parse", f"refs/heads/{SPRINT}")
    first = commit_on(app_repo, SPRINT, {"a.py": "A = 1\n"})
    second = commit_on(app_repo, SPRINT, {"b.py": "B = 1\n"})
    elsewhere = commit_on(app_repo, "other", {"c.py": "C = 1\n"})
    in_main = git(app_repo, "rev-parse", "refs/heads/main")
    _state(layer, "00000001", ended="2026-11-16T10:00:00", delivery={"branch": SPRINT, "delivered": True,
                                                                      "base": first, "head": second})
    _state(layer, "00000002", ended="2026-11-16T09:00:00", delivery={"branch": SPRINT, "delivered": True,
                                                                      "base": base, "head": first})
    _state(layer, "00000003", delivery={"branch": "other", "delivered": True, "head": elsewhere})
    _state(layer, "00000004", delivery={"branch": SPRINT, "delivered": True, "head": in_main})
    _state(layer, "00000005", delivery={"branch": SPRINT, "delivered": False}, ship={"action": "handoff"})
    found, handed = promotion.changes(ir.load(layer), SPRINT, "main")
    assert [c.id for c in found] == ["00000002", "00000001"]
    assert found[0].files == ["a.py"] and found[1].files == ["b.py"]
    assert [c.id for c in handed] == ["00000005"]
    pinned, _ = promotion.changes(ir.load(layer), SPRINT, "main", pinned=first)
    assert [c.id for c in pinned] == ["00000002"]


def test_acceptance_criteria_come_from_the_plan(make_layer, app_repo):
    layer = sprint_layer(make_layer, app_repo)
    (layer / "specs").mkdir()
    (layer / "specs" / "feature.md").write_text(
        "# Feature: cart\n\nA cart.\n\n## Acceptance Criteria\n\n- The cart holds items\n- It survives a reload,\n"
        "  also offline\n\n## Notes\n\n- not a criterion\n")
    (layer / "specs" / "bug.md").write_text("# Bug: crash\n\n## Steps\n\n- open it\n")
    loaded = ir.load(layer)
    assert promotion.criteria(loaded, "specs/feature.md") == ["The cart holds items",
                                                                "It survives a reload, also offline"]
    assert promotion.criteria(loaded, "specs/bug.md", "Fix the crash on start\nmore") == [
        "The request: Fix the crash on start"]


def test_each_criterion_lists_the_proof_that_covers_it():
    change = promotion.Change(id="00000001", criteria=["lint is clean", "cart.png shows the cart", "it is fast"],
                              checks={"lint": 0, "test": 1},
                              artefacts=[{"type": "image", "path": "artefacts/x/cart.png"},
                                         {"type": "image", "path": "artefacts/x/other.png"}],
                              verdict={"success": True, "summary": "fine"})
    proof = promotion.proof_for(None, change)
    assert [p["label"] for p in proof[0]["proof"] if p["kind"] == "check"] == ["lint exit 0"]
    assert [p["label"] for p in proof[1]["proof"] if p["kind"] == "image"] == ["cart.png"]
    assert len([p for p in proof[2]["proof"] if p["kind"] == "image"]) == 2
    assert all(any(p["kind"] == "verdict" for p in item["proof"]) for item in proof)
    assert not any(item["uncovered"] for item in proof)
    bare = promotion.Change(id="00000002", criteria=["works"], checks={"test": 1}, verdict={"success": True})
    assert promotion.proof_for(None, bare)[0]["uncovered"] is True


def test_record_decision_writes_an_auditable_record(make_layer, app_repo):
    layer = ir.load(sprint_layer(make_layer, app_repo))
    known = ["00000001", "00000002"]
    record = promotion.record_decision(layer, "abcd0001", " Ada Example ", "refuse", ["00000001"], "not now",
                                       known=known, now=NOW)
    lines = (layer.layer_dir / "promotions" / "abcd0001" / "acceptance.jsonl").read_text().splitlines()
    assert json.loads(lines[0]) == {"at": "2026-11-16T09:30:00+00:00", "by": "Ada Example", "kind": "human",
                                    "scope": ["00000001"], "decision": "refuse", "then": "revert",
                                    "comment": "not now", "source": ""}
    assert record["then"] == "revert"
    sign_off = (layer.layer_dir / "promotions" / "abcd0001" / "sign-off.md").read_text()
    assert "Ada Example" in sign_off and "refuse 00000001" in sign_off
    for bad in [dict(scope=["deadbeef"]), dict(by=""), dict(by="x" * 81), dict(by="a\nb"),
                dict(decision="approve", scope=["00000001"]), dict(comment="x" * 2001), dict(decision="maybe")]:
        args = {"by": "Ada", "decision": "accept", "scope": ["00000002"], "comment": "", **bad}
        with pytest.raises(promotion.PromotionError) as caught:
            promotion.record_decision(layer, "abcd0001", args["by"], args["decision"], args["scope"], args["comment"],
                                      known=known)
        assert caught.value.reason == "invalid-decision"
    assert len((layer.layer_dir / "promotions" / "abcd0001" / "acceptance.jsonl").read_text().splitlines()) == 1


def test_sign_off_of_a_phase_gate_promotion_is_a_document(make_layer, app_repo):
    stages = [{"name": "build", "branch": "phase/build", "from": "main", "into": "verify", "accepts": ["feature"],
               "origin": "preset phase-gate"},
              {"name": "verify", "branch": "phase/verify", "from": "main", "into": "main", "accepts": [],
               "agentsMayMerge": False, "origin": "preset phase-gate"},
              {"name": "main", "branch": "main", "accepts": [], "agentsMayMerge": False, "origin": "preset phase-gate"}]
    layer = ir.load(sprint_layer(make_layer, app_repo, stages=stages, promotions=[], extra={"cycle": None}))
    changes = [promotion.Change(id="00000001", prompt="Add the cart"), promotion.Change(id="00000002", prompt="Add search")]
    info = {"from": "build", "to": "verify", "from_branch": "phase/build", "to_branch": "phase/verify",
            "changes": [c.__dict__ for c in changes]}
    known = [c.id for c in changes]
    promotion.record_decision(layer, "abcd0002", "Ada Example", "accept", ["00000001"], "good", known=known, now=NOW)
    promotion.record_decision(layer, "abcd0002", "Grace Example", "refuse", ["00000002"], "too slow", known=known,
                              now=NOW)
    text = promotion.render_sign_off(layer, "abcd0002", info).read_text()
    assert text.startswith("# Sign-off: phase build → verify\n")
    assert re.search(r"^\| `00000001` \| Add the cart \| accepted \| Ada Example \| 2026-11-16T09:30:00\+00:00 \| good \|$",
                     text, re.M)
    assert re.search(r"^\| `00000002` \| Add search \| refused \(revert\) \| Grace Example \| .* \| too slow \|$", text, re.M)


def test_approval_counts_distinct_people_and_forge_reviews():
    promo = {"approval": "human", "approvers": 2, "requires": ["checks", "acceptance"]}
    changes = [promotion.Change(id="00000001"), promotion.Change(id="00000002")]

    def line(by, decision, scope=("all",), kind="human", at="2026-11-16T10:00:00"):
        return {"by": by, "decision": decision, "scope": list(scope), "kind": kind, "at": at, "then": "", "comment": ""}

    twice = [line("Ada", "approve"), line("Ada", "approve")]
    status = promotion.approval_status(None, promo, twice, changes)
    assert status["have"] == 1 and not status["ready"] and status["why"] == "1 human approval missing (1 of 2 given)"
    status = promotion.approval_status(None, promo, twice, changes, [{"by": "bob", "state": "APPROVED"}])
    assert status["have"] == 2 and status["ready"] and status["undecided"] == []
    none = promotion.approval_status(None, {**promo, "approvers": 1}, [], changes)
    assert none["why"] == "1 human approval missing; 2 changes undecided"
    later = [line("Ada", "accept", ["00000001"], at="1"), line("Ada", "refuse", ["00000001"], at="2"),
             line("Ada", "approve")]
    status = promotion.approval_status(None, {**promo, "approvers": 1}, later, changes)
    assert status["refused"] == ["00000001"] and status["ready"]
    assert promotion.approval_status(None, {"approval": "auto", "requires": []}, [], changes)["ready"]


def test_runtime_refuses_agent_approval_into_a_protected_stage(make_layer, app_repo):
    layer = ir.load(sprint_layer(make_layer, app_repo, promotion={"approval": "agent"}))
    with pytest.raises(promotion.PromotionError) as caught:
        promotion.guard(layer, promotion.promotion_for(layer, "sprint", "main"))
    assert caught.value.reason == "agents-not-allowed"


def _repo(tmp_path: Path, files: dict[str, str], tags=()) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for name, content in files.items():
        (repo / name).write_text(content)
    (repo / "x").write_text("x")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "x")
    for tag in tags:
        git(repo, "tag", tag)
    return repo


@pytest.mark.parametrize("files, tags, expected", [
    ({"VERSION": "1.4.2\n"}, (), ("1.4.2", "VERSION")),
    ({"pyproject.toml": '[build-system]\nrequires = ["x"]\n\n[project]\nname = "a"\nversion = "2.0.1"\n'}, (),
     ("2.0.1", "pyproject.toml")),
    ({"pyproject.toml": '[tool.poetry]\nname = "a"\nversion = "0.3.0"\n'}, (), ("0.3.0", "pyproject.toml")),
    ({"package.json": '{"name": "a", "dependencies": {"version": "9.9.9"}, "version": "3.1.4"}'}, (),
     ("3.1.4", "package.json")),
    ({"Cargo.toml": '[dependencies]\nversion = "9.9.9"\n\n[package]\nname = "a"\nversion = "0.9.0"\n'}, (),
     ("0.9.0", "Cargo.toml")),
    ({}, ("v1.2.3", "v1.10.0", "nightly"), ("1.10.0", "tag v1.10.0")),
    ({}, (), ("0.0.0", "")),
])
def test_read_version_from_where_the_repository_keeps_it(tmp_path, files, tags, expected):
    assert promotion.read_version(_repo(tmp_path, files, tags)) == expected


def test_bump_levels():
    assert promotion.bump("1.2.3", "major") == "2.0.0"
    assert promotion.bump("1.2.3", "minor") == "1.3.0"
    assert promotion.bump("1.2.3", "patch") == "1.2.4"
    assert promotion.bump("1.2.3-rc.1", "patch") == "1.2.4"
    assert promotion.bump("v1.2.3+build.5", "minor") == "1.3.0"


def test_write_version_keeps_the_file_byte_for_byte_except_the_version(tmp_path):
    text = '{\n   "name" :"a",\n  "deps": {"version":"9.9.9"},\n\t"version"  :   "1.2.3" ,\n "x": [1,2]\n}\n'
    (tmp_path / "package.json").write_text(text)
    assert promotion.write_version(tmp_path, "package.json", "1.3.0")
    assert (tmp_path / "package.json").read_text() == text.replace('"1.2.3"', '"1.3.0"')
    toml = '[package]\nname = "a"\nversion = "0.1.0"  # the crate\n\n[dependencies]\nserde = { version = "1" }\n'
    (tmp_path / "Cargo.toml").write_text(toml)
    assert promotion.write_version(tmp_path, "Cargo.toml", "0.2.0")
    assert (tmp_path / "Cargo.toml").read_text() == toml.replace('"0.1.0"', '"0.2.0"')
    assert not promotion.write_version(tmp_path, "tag v1.0.0", "1.1.0")


CHANGES = [promotion.Change(id="00000001", prompt="Add the cart", klass="feature"),
           promotion.Change(id="00000002", prompt="Fix the crash\nmore", klass="bug"),
           promotion.Change(id="00000003", prompt="Tidy the readme", klass="chore")]


def test_changelog_keepachangelog_entry(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(KEEPACHANGELOG.replace("## [Unreleased]\n", "## [Unreleased]\n\n- wip\n"))
    assert promotion.changelog_kind(tmp_path) == ("keepachangelog", "CHANGELOG.md")
    entry = promotion.insert_changelog(tmp_path, "CHANGELOG.md", "keepachangelog", "0.2.0", "2026-11-16", CHANGES)
    assert entry == ("## [0.2.0] - 2026-11-16\n\n### Added\n\n- Add the cart\n\n### Fixed\n\n- Fix the crash\n\n"
                     "### Changed\n\n- Tidy the readme\n")
    text = (tmp_path / "CHANGELOG.md").read_text()
    assert text.index("## [Unreleased]") < text.index("- wip") < text.index("## [0.2.0]") < text.index("## [0.1.0]")


def test_changelog_follows_a_plain_heading_style(tmp_path):
    (tmp_path / "CHANGES.md").write_text("# Changes\n\n## v0.1.0 (2026-01-02)\n\n- first\n")
    assert promotion.changelog_kind(tmp_path) == ("plain", "CHANGES.md")
    promotion.insert_changelog(tmp_path, "CHANGES.md", "plain", "0.2.0", "2026-11-16", CHANGES[:1])
    assert (tmp_path / "CHANGES.md").read_text() == ("# Changes\n\n## v0.2.0 (2026-11-16)\n\n- Add the cart\n\n"
                                                     "## v0.1.0 (2026-01-02)\n\n- first\n")


def test_no_changelog_file_is_left_alone(tmp_path):
    assert promotion.changelog_kind(tmp_path) == ("", "")
    assert list(tmp_path.iterdir()) == []


def test_fill_tag():
    assert promotion.fill_tag("v{version}", "0.2.0") == "v0.2.0"
    assert promotion.fill_tag("sprint-{cycle}", cycle="2026-s47") == "sprint-2026-s47"
    assert promotion.fill_tag("release-{date}-{version}", "1.0.0", date="2026-11-16") == "release-2026-11-16-1.0.0"


def test_frozen_reason_by_date_and_while_promoting(make_layer, app_repo):
    layer = ir.load(sprint_layer(make_layer, app_repo))
    stage = delivery.Stage(name="sprint", branch=SPRINT, frozen=("2026-12-21..2027-01-03",))

    def at(day):
        return datetime.fromisoformat(f"{day}T12:00:00+00:00")

    assert delivery.frozen_reason(layer, stage, at("2026-12-20")) == ""
    assert delivery.frozen_reason(layer, stage, at("2026-12-21")).startswith(
        "stage sprint is frozen from 2026-12-21 to 2027-01-03 (UTC); the run waits")
    assert delivery.frozen_reason(layer, stage, at("2027-01-03"), resume="RESUME").endswith("after the freeze: RESUME")
    assert delivery.frozen_reason(layer, stage, at("2027-01-04")) == ""
    promoting = delivery.Stage(name="sprint", branch=SPRINT, frozen_while_promoting=True)
    assert delivery.frozen_reason(layer, promoting, NOW) == ""
    promotion._mark_open(layer, {"id": "1a2b3c4d", "from": "sprint", "to": "main"})
    assert delivery.frozen_reason(layer, promoting, NOW) == (
        "stage sprint is closed to deliveries while promotion 1a2b3c4d into main is open; it continues when the "
        "promotion closes")
    assert delivery.frozen_reason(layer, delivery.Stage(name="sprint", branch=SPRINT), NOW) == ""


def test_resolve_branches(make_layer, app_repo):
    stages = [
        {"name": "sprint", "branch": "sprint/{cycle}", "from": "main", "accepts": ["feature"]},
        {"name": "hotfix", "branch": "hotfix/{date}", "from": "main", "into": "main", "accepts": ["bug"]},
        {"name": "develop", "branch": "develop", "from": "main", "accepts": ["chore"]},
        {"name": "main", "branch": "main", "accepts": [], "agentsMayMerge": False}]
    promos = [{"from": name, "to": "main", "approval": "human", "approvers": 1} for name in ("sprint", "hotfix", "develop")]
    layer_dir = sprint_layer(make_layer, app_repo, stages=stages, promotions=promos)
    git(app_repo, "branch", "develop", "main")
    layer = ir.load(layer_dir)
    promo = {p["from"]: p for p in promos}
    assert promotion.resolve_branches(layer, promo["develop"]) == ("develop", "main")
    assert promotion.resolve_branches(layer, promo["sprint"]) == (SPRINT, "main")
    assert promotion.resolve_branches(layer, promo["sprint"], "develop") == ("develop", "main")
    with pytest.raises(promotion.PromotionError) as caught:
        promotion.resolve_branches(layer, promo["hotfix"])
    assert caught.value.reason == "stage-missing"
    git(app_repo, "branch", "hotfix/2026-11-10", "main")
    commit_on(app_repo, "hotfix/2026-11-10", {"fix.py": "FIX = 1\n"})
    git(app_repo, "branch", "hotfix/2026-11-01", "main")   # already in main: not waiting
    assert promotion.resolve_branches(layer, promo["hotfix"]) == ("hotfix/2026-11-10", "main")
    git(app_repo, "branch", "hotfix/2026-11-12", "main")
    commit_on(app_repo, "hotfix/2026-11-12", {"fix2.py": "FIX = 2\n"})
    with pytest.raises(promotion.PromotionError) as caught:
        promotion.resolve_branches(layer, promo["hotfix"])
    assert caught.value.reason == "ambiguous-branch"
    assert "hotfix/2026-11-10, hotfix/2026-11-12" in caught.value.message


def test_pack_contents_with_pictures_and_a_recording(make_layer, app_repo, tmp_path):
    layer = sprint_layer(make_layer, app_repo)
    deliver(layer, app_repo, "a2000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart",
            criteria=["The cart shows cart.png", "test passes"], shots=["cart.png", "cart-empty.png"],
            risks=[f"touches {layer}/specs/plan-a2000001.md and {app_repo}/cart.py"])
    deliver(layer, app_repo, "a2000002", {"checkout.py": "CHECKOUT = 1\n"}, prompt="Add the checkout",
            criteria=["Checkout completes"], video="checkout.webm")
    code, out = promote(layer, "sprint", "main")
    assert code == 75, out
    pid = open_id(layer)
    pack = layer / "artefacts" / pid / "pack"
    page = (pack / "index.html").read_text()
    for needle in ("Add a cart", "Add the checkout", "Adds what was asked: Add a cart.", "Risk notes",
                   "touches specs/plan-a2000001.md and cart.py", "The cart shows cart.png", "Checkout completes",
                   "test exit 0", "review pass", "Release notes", "Changelog entry", "## [0.2.0]", "Not included",
                   "<td>test</td><td class=\"ok\">0</td>"):
        assert needle in page, needle
    assert re.search(rf'<img src="media/a2000001/cart\.png"', page)
    assert re.search(rf'<img src="media/a2000001/cart-empty\.png"', page)
    assert re.search(r'<img src="media/a2000002/frame-1\.png"', page)
    assert re.search(r'<video controls src="media/a2000002/checkout\.webm"', page)
    for link in re.findall(r'(?:src|href)="([^"]+)"', page):
        assert not link.startswith(("/", "http", "file:")), link
        assert (pack / link).resolve().is_file() and (pack / link).resolve().is_relative_to(pack.resolve()), link
    for path in pack.rglob("*"):
        if path.is_file() and path.suffix in (".html", ".md"):
            assert str(tmp_path) not in path.read_text(), path
    for name in ("summary.md", "release-notes.md", "changelog-entry.md", "acceptance.md"):
        assert (pack / name).is_file()
    artefacts = state_of(layer, pid)["artefacts"]
    kinds = {(a["type"], Path(a["path"]).name) for a in artefacts}
    assert {("html", "index.html"), ("image", "cart.png"), ("image", "cart-empty.png"),
            ("video", "checkout.webm")} <= kinds
    video = next(a for a in artefacts if a["type"] == "video")
    assert [Path(f["path"]).name for f in video["keyframes"]] == ["frame-1.png", "frame-2.png"]


def test_pr_approvals_github_and_forgejo():
    def gh_run(argv, **kwargs):
        assert argv[:5] == ["gh", "pr", "view", "7", "--json"] and argv[5] == "reviews"
        out = json.dumps({"reviews": [{"author": {"login": "ada"}, "state": "APPROVED", "submittedAt": "t1"},
                                      {"author": {"login": "bob"}, "state": "CHANGES_REQUESTED", "submittedAt": "t2"}]})
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    github = forge.GitHubForge({"kind": "github", "repo": "acme/webshop"}, run=gh_run)
    assert github.pr_approvals(7) == [{"by": "ada", "state": "APPROVED", "at": "t1"}]

    def http(method, url, headers, body, timeout=30.0):
        assert method == "GET" and url.endswith("/repos/acme/webshop/pulls/7/reviews")
        return 200, [{"user": {"login": "grace"}, "state": "APPROVED", "submitted_at": "t3"},
                     {"user": {"login": "bob"}, "state": "COMMENT", "submitted_at": "t4"}]

    forgejo = forge.ForgejoForge({"kind": "forgejo", "host": "https://forge.example.org", "repo": "acme/webshop"},
                                 http=http)
    assert forgejo.pr_approvals(7) == [{"by": "grace", "state": "APPROVED", "at": "t3"}]
    assert forge.NoneForge({"kind": "none"}).pr_approvals(7) == []


def test_promotion_adw_is_a_valid_synthetic_workflow(make_layer, app_repo):
    layer = ir.load(sprint_layer(make_layer, app_repo))
    promo = promotion.promotion_for(layer, "sprint", "main")
    adw = promotion.promotion_adw(layer, promo, SPRINT, "main")
    layer.ir["adws"].append(adw)
    assert layer.validate_adw("promote-sprint-main") is adw
    assert [s["name"] for s in adw["steps"]] == ["open", "checks", "pack", "request", "approval", "version", "merge",
                                                  "tag", "close"]
    assert adw["promotion"] == {"from": "sprint", "to": "main", "fromBranch": SPRINT, "toBranch": "main",
                                "base": "demo"}
    assert adw["deliverTo"] == "sprint" and adw["environment"] == ["worktree"]
    bare = promotion.promotion_adw(layer, {**promo, "version": None, "tag": "", "requires": ["acceptance"]}, SPRINT)
    assert [s["name"] for s in bare["steps"]] == ["open", "pack", "request", "approval", "merge", "close"]
    full = promotion.promotion_adw(layer, {**promo, "after": ["deploy"], "backMerge": ["x"]}, SPRINT)
    assert "deploy" in [s["name"] for s in full["steps"]] and "back-merge" in [s["name"] for s in full["steps"]]


def test_delivery_records_base_and_head(make_layer, app_repo):
    layer_dir = sprint_layer(make_layer, app_repo)
    head = commit_on(app_repo, SPRINT, {"x.py": "X = 1\n"})
    layer = ir.load(layer_dir)
    state = ADWState(adw_id="0badf00d", adw="demo", ship={"shipped": True, "target": SPRINT},
                     delivery={"stage": "sprint", "branch": SPRINT, "base": "abc"})
    delivery.after_ship(layer, state)
    assert state.delivery["head"] == head and state.delivery["base"] == "abc" and state.delivery["delivered"] is True
    state = deliver(layer_dir, app_repo, "a3000001", {"y.py": "Y = 1\n"})
    assert state["delivery"]["base"] == head
    assert state["delivery"]["head"] == git(app_repo, "rev-parse", f"refs/heads/{SPRINT}")
    assert git(app_repo, "diff", "--name-only", state["delivery"]["base"], state["delivery"]["head"]) == "y.py"
