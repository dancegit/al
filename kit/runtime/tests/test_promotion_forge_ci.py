"""Promotion and delivery with a forge used for CI and pushing only (#:pull-requests #f): full runs
(runner.main through promotion.main, mock agent, real git) against a bare `origin` and FakeForgeCI,
the Forgejo double that answers the combined status of a commit. The forge's CI is the last gate of
a promotion with #:requires forge-ci: TO moves only after it passed on the exact commit."""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from adw_modules import delivery, forge
from promotion_fixtures import (SPRINT, FakeForgeCI, decide, deliver, events_of, forgejo_forge, git, open_id, promote,
                                sprint_layer, state_of, with_origin)

URL = "https://ci.example.org/runs/7"


def _clock(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(forge, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(forge, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    return clock


def _setup(make_layer, app_repo, tmp_path, monkeypatch, fake, *, method="fast-forward", forge_ci=True,
           pull_requests=False):
    requires = ["checks", "acceptance", "forge-ci"] if forge_ci else ["checks", "acceptance"]
    layer = sprint_layer(make_layer, app_repo, extra={"forge": forgejo_forge(pull_requests)},
                         promotion={"requires": requires, "forgeCiTimeout": 1, "method": method})
    remote = with_origin(app_repo, tmp_path)
    fake.remote = remote
    monkeypatch.setattr(forge, "http_request", fake)
    _clock(monkeypatch)
    deliver(layer, app_repo, "fc000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    deliver(layer, app_repo, "fc000002", {"search.py": "SEARCH = 1\n"}, prompt="Add search")
    code, out = promote(layer, "sprint", "main")
    assert code == 75, out
    pid = open_id(layer)
    code, out = decide(layer, "sprint", "main", "approve", "--by", "Ada Example")
    assert code == 0, out
    return layer, remote, pid


def _success(sha):
    return [[("ci/build", "success", URL)]]


def test_forge_ci_success_on_the_pinned_commit_merges(make_layer, app_repo, tmp_path, monkeypatch):
    fake = FakeForgeCI(rule=_success)
    layer, remote, pid = _setup(make_layer, app_repo, tmp_path, monkeypatch, fake)
    code, out = promote(layer, "sprint", "main")
    assert code == 0, out
    state = state_of(layer, pid)
    promoted = state["promotion"]["promoted"]
    assert git(app_repo, "rev-parse", "refs/heads/main") == promoted
    assert git(remote, "rev-parse", "refs/heads/main") == promoted
    assert set(fake.queried) == {promoted}
    assert git(remote, "rev-parse", f"refs/heads/{SPRINT}") == promoted
    assert state["promotion"]["forge_ci"]["outcome"] == "passed"
    assert f"forge CI on {promoted[:7]}: passed (1 run(s))" in out
    assert any(e["type"] == "promotion.forge-ci" and e["outcome"] == "passed" for e in events_of(layer, pid))
    assert ("POST", "/pulls") not in fake.calls


def test_pack_lists_the_forge_ci_runs(make_layer, app_repo, tmp_path, monkeypatch):
    fake = FakeForgeCI(rule=_success)
    layer, _remote, pid = _setup(make_layer, app_repo, tmp_path, monkeypatch, fake)
    code, out = promote(layer, "sprint", "main")
    assert code == 0, out
    pack = layer / "artefacts" / pid / "pack"
    index = (pack / "index.html").read_text(encoding="utf-8")
    assert "Forge CI on" in index and "ci/build" in index and "success" in index and URL in index
    summary = (pack / "summary.md").read_text(encoding="utf-8")
    assert "Forge CI: passed" in summary and "ci/build=success" in summary


@pytest.mark.parametrize("answer, reason", [
    ([[("ci/build", "failure", URL)]], "forge-ci-failed"),
    ([[("ci/build", "error", URL)]], "forge-ci-failed"),
    ([], "forge-ci-missing"),
    ([[("ci/build", "pending", URL)]], "forge-ci-timeout"),
])
def test_forge_ci_stops_and_merges_nothing(make_layer, app_repo, tmp_path, monkeypatch, answer, reason):
    fake = FakeForgeCI(rule=lambda sha: answer)
    layer, remote, pid = _setup(make_layer, app_repo, tmp_path, monkeypatch, fake)
    local_main = git(app_repo, "rev-parse", "refs/heads/main")
    remote_main = git(remote, "rev-parse", "refs/heads/main")
    code, out = promote(layer, "sprint", "main")
    assert code == 1, out
    state = state_of(layer, pid)
    assert f"promotion:{reason}" in out
    assert f"promotion:{reason}" in state["promotion"]["stopped"]["reason"]
    assert "main is unchanged" in state["promotion"]["stopped"]["reason"]
    if reason == "forge-ci-failed":
        assert "ci/build" in out and URL in out
        assert "ci/build" in state["promotion"]["stopped"]["reason"]
    assert git(app_repo, "rev-parse", "refs/heads/main") == local_main
    assert git(remote, "rev-parse", "refs/heads/main") == remote_main
    index = (layer / "artefacts" / pid / "pack" / "index.html").read_text(encoding="utf-8")
    assert "Forge CI on" in index
    if answer:
        assert "ci/build" in index and URL in index


@pytest.mark.parametrize("passes", [True, False])
def test_forge_ci_with_merge_waits_on_the_merge_commit(make_layer, app_repo, tmp_path, monkeypatch, passes):
    holder = SimpleNamespace(remote=None)

    def rule(sha):
        parents = git(holder.remote, "rev-list", "--parents", "-n", "1", sha).split()
        if len(parents) == 3:  # the merge commit, not the pinned stage head
            return [[("ci/build", "success" if passes else "failure", URL)]]
        return []

    fake = FakeForgeCI(rule=rule)
    holder.remote = tmp_path / "remote.git"
    layer, remote, pid = _setup(make_layer, app_repo, tmp_path, monkeypatch, fake, method="merge")
    old_main = git(app_repo, "rev-parse", "refs/heads/main")
    remote_main = git(remote, "rev-parse", "refs/heads/main")
    code, out = promote(layer, "sprint", "main")
    state = state_of(layer, pid)
    promoted = state["promotion"]["promoted"]
    merge_commit = fake.queried[0]
    assert merge_commit != promoted
    assert git(app_repo, "rev-list", "--parents", "-n", "1", merge_commit).split()[1:] == [old_main, promoted]
    assert fake.ci_refs[0] == merge_commit
    if passes:
        assert code == 0, out
        assert git(app_repo, "rev-parse", "refs/heads/main") == merge_commit
        assert git(remote, "rev-parse", "refs/heads/main") == merge_commit
        assert git(remote, "rev-parse", "--verify", "--quiet", "refs/heads/al/ci/main", check=False) == ""
    else:
        assert code == 1, out
        assert "promotion:forge-ci-failed" in out
        assert git(app_repo, "rev-parse", "refs/heads/main") == old_main
        assert git(remote, "rev-parse", "refs/heads/main") == remote_main


def test_promotion_pushes_to_after_the_merge(make_layer, app_repo, tmp_path, monkeypatch):
    fake = FakeForgeCI()
    layer, remote, pid = _setup(make_layer, app_repo, tmp_path, monkeypatch, fake, method="merge", forge_ci=False)
    code, out = promote(layer, "sprint", "main")
    assert code == 0, out
    result = state_of(layer, pid)["promotion"]["result"]
    assert git(app_repo, "rev-parse", "refs/heads/main") == result
    assert git(remote, "rev-parse", "refs/heads/main") == result
    assert state_of(layer, pid)["promotion"]["pushes"]["main"]["ok"] is True
    assert fake.queried == []
    assert ("POST", "/pulls") not in fake.calls


def test_delivery_pushes_the_stage_branch(make_layer, app_repo, tmp_path, monkeypatch):
    fake = FakeForgeCI()
    layer = sprint_layer(make_layer, app_repo, extra={"forge": forgejo_forge()})
    remote = with_origin(app_repo, tmp_path)
    monkeypatch.setattr(forge, "http_request", fake)
    state = deliver(layer, app_repo, "fd000001", {"cart.py": "CART = 1\n"}, prompt="Add a cart")
    head = git(app_repo, "rev-parse", f"refs/heads/{SPRINT}")
    assert git(remote, "rev-parse", f"refs/heads/{SPRINT}") == head
    assert state["delivery"]["pushed"] == {"remote": "origin", "branch": SPRINT, "commit": head, "ok": True,
                                           "error": ""}
    assert any(e["type"] == "delivery.pushed" and e["ok"] for e in events_of(layer, "fd000001"))
    assert not (state["ship"].get("pr") or {}).get("number")
    assert ("POST", "/pulls") not in fake.calls


@dataclasses.dataclass
class _Layer:
    ir: dict


def test_pull_requests_true_keeps_today_s_forge_path():
    with_prs = _Layer(ir={"forge": forgejo_forge(pull_requests=True)})
    assert delivery.ship_layer(with_prs) is with_prs
    none = _Layer(ir={"forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""}})
    assert delivery.ship_layer(none) is none
    original = forgejo_forge(pull_requests=False)
    without = _Layer(ir={"name": "x", "forge": original})
    seen = delivery.ship_layer(without)
    assert seen is not without
    assert seen.ir["forge"]["kind"] == "none" and seen.ir["name"] == "x"
    assert without.ir["forge"] is original and original["kind"] == "forgejo"
    assert delivery.pull_requests(with_prs) and not delivery.pull_requests(without)
    assert delivery.push_remote(without) == "origin" and delivery.push_remote(with_prs) == ""
