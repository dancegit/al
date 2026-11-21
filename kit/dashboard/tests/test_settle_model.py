"""Settlement records, attention() and its buckets, failure groups, replacement and supersession, and
the needs-you count, on the scenario of settle_fixtures (an invented orchard with real git history)."""
from __future__ import annotations

import collections
import json
import os
import shutil
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import dashboard as d
import settle_fixtures as sf


@pytest.fixture(scope="module")
def scenario_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("settle") / "s"
    sf.build_scenario(root)
    return root


@pytest.fixture()
def scenario(tmp_path: Path, scenario_root: Path) -> dict[str, Path]:
    """A private copy of the scenario (tests write settlements into it)."""
    root = tmp_path / "s"
    shutil.copytree(scenario_root, root, symlinks=True)
    return {"root": root, "repo": root / "orchard", "layer": root / "orchard.al"}


def model_for(layer: Path, ticks: int = 3) -> d.Model:
    model = d.Model([layer], [])
    model.discover()
    for _ in range(ticks):
        model.tick()
    return model


def buckets(model: d.Model) -> dict[str, list[str]]:
    out: dict[str, list[str]] = collections.defaultdict(list)
    for key in sorted(model.summaries):
        out[model.attention[key]["bucket"]].append(key.split("/", 1)[1])
    return dict(out)


def item(model: d.Model, run_id: str) -> dict:
    return model.attention[f"orchard/{run_id}"]


# ------------------------------------------------------------------------------ the record

def test_write_settlement_is_atomic_and_keeps_previous(scenario):
    layer = scenario["layer"]
    run = sf.OPEN[0]
    first = d.write_settlement(layer, run, as_="abandoned", note="the spec\nchanged", who="Ada", via="cli",
                               status="handed-over")
    path = layer / "agents" / run / d.SETTLEMENT_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir() if p.name.startswith(".settlement.")] == []   # no temporary left
    on_disk = json.loads(path.read_text())
    assert on_disk == first and first["note"] == "the spec changed" and first["via"] == "cli"
    assert set(first) == {"as", "by", "note", "who", "at", "via"}
    second = d.write_settlement(layer, run, as_="superseded", by=sf.INTEGRATION, who="Ada", via="dashboard",
                                status="handed-over")
    assert second["previous"]["as"] == "abandoned" and "previous" not in second["previous"]
    third = d.write_settlement(layer, run, as_="landed", via="dashboard", status="handed-over")
    assert third["previous"]["as"] == "superseded" and "previous" not in third["previous"]   # one level
    assert d.read_settlement(layer, run) == third
    assert sorted(p.name for p in path.parent.iterdir()) == ["request.md", "settlement.json", "state.json"]


def test_release_worktree_removes_only_that_runs_worktree(scenario):
    layer, repo = scenario["layer"], scenario["repo"]
    first, second = sf.OPEN[0], sf.OPEN[1]
    for run in (first, second):
        sf.Git(repo)("worktree", "add", "-q", str(layer / "trees" / run), f"al/{run}")
    d.write_settlement(layer, first, as_="abandoned", who="Ada", via="cli", status="handed-over")
    ir = json.loads((layer / "agentic-layer.json").read_text())
    assert d.release_worktree(layer, ir, first, repo) == (f"trees/{first}", "")
    assert not (layer / "trees" / first).exists() and (layer / "trees" / second).is_dir()
    listed = sf.Git(repo)("worktree", "list", "--porcelain")
    assert f"/{first}\n" not in listed + "\n" and second in listed
    assert (layer / "agents" / first / "state.json").is_file()
    assert sf.Git(repo)("rev-parse", "--verify", "-q", f"refs/heads/al/{first}")
    assert d.release_worktree(layer, ir, first, repo) == ("", "")              # nothing left to remove
    assert d.release_worktree(layer, ir, "../x", repo) == ("", "not a run id")


def test_settlement_refused_for_running_and_bad_by(scenario):
    layer, repo = scenario["layer"], scenario["repo"]
    with pytest.raises(d.SettleError) as running:
        d.write_settlement(layer, sf.RESTARTS[0], as_="abandoned", via="cli", status="running")
    assert running.value.kind == "running"
    with pytest.raises(d.SettleError) as unknown:
        d.write_settlement(layer, sf.OPEN[0], as_="abandoned", via="cli", status="unknown")
    assert unknown.value.kind == "running"
    with pytest.raises(d.SettleError) as by:
        d.write_settlement(layer, sf.OPEN[0], as_="landed", by="deadbeefdeadbeef", via="cli",
                           status="handed-over", repo=repo)
    assert by.value.kind == "by"
    with pytest.raises(d.SettleError) as bad:
        d.write_settlement(layer, sf.OPEN[0], as_="landed", by="not a sha", via="cli", status="handed-over", repo=repo)
    assert bad.value.kind == "invalid"
    with pytest.raises(d.SettleError):
        d.write_settlement(layer, sf.OPEN[0], as_="merged", via="cli", status="handed-over")
    with pytest.raises(d.SettleError):
        d.write_settlement(layer, sf.OPEN[0], as_="landed", note="x" * 501, via="cli", status="handed-over")
    with pytest.raises(d.SettleError):
        d.write_settlement(layer, sf.OPEN[0], as_="landed", who="a\nb", via="cli", status="handed-over")
    with pytest.raises(d.SettleError) as missing:
        d.write_settlement(layer, "0badc0de", as_="landed", via="cli", status="failed")
    assert missing.value.kind == "missing"
    assert not (layer / "agents" / sf.OPEN[0] / d.SETTLEMENT_FILE).exists()
    tip = sf.Git(repo)("rev-parse", "main")
    by_commit = d.write_settlement(layer, sf.OPEN[0], as_="landed", by=tip[:10], via="cli", status="handed-over",
                                   repo=repo)
    by_run = d.write_settlement(layer, sf.OPEN[1], as_="superseded", by=sf.INTEGRATION, via="cli",
                                status="handed-over", repo=repo)
    assert by_commit["by"] == tip[:10] and by_run["by"] == sf.INTEGRATION


# ------------------------------------------------------------------------------ buckets

def test_attention_buckets_on_the_scenario(scenario):
    model = model_for(scenario["layer"])
    got = buckets(model)
    assert sorted(got["needs-you"]) == sorted(sf.OPEN + [sf.WAITING, sf.OLD_FAILURE])
    assert sorted(got["landed"]) == sorted(sf.MERGED + sf.SQUASHED)
    assert sorted(got["settled"]) == sorted(sf.INTEGRATED + sf.FAILED)
    assert sorted(got["running"]) == sorted(sf.RESTARTS)
    assert got["done"] == [sf.INTEGRATION]
    assert {item(model, r)["landing"]["how"] for r in sf.MERGED} == {"ancestor"}
    assert {item(model, r)["landing"]["how"] for r in sf.SQUASHED} == {"patch-id"}
    for run in sf.INTEGRATED:
        assert item(model, run)["superseded_by"] == sf.INTEGRATION
        assert item(model, run)["reason"] == f"superseded by {sf.INTEGRATION}"
    assert item(model, sf.PR_RUN)["next"] == "review PR #7, or Give to an agent"
    assert item(model, sf.OPEN[0])["next"] == f"merge al/{sf.OPEN[0]} into main, or `al settle`, or Give to an agent"
    assert item(model, sf.WAITING)["reason"] == "waiting at approve"
    assert "--resume --adw-id a0000001" in item(model, sf.WAITING)["next"]
    assert item(model, sf.OLD_FAILURE)["reason"].startswith("build: ")
    assert item(model, sf.OLD_FAILURE)["next"] == "Start again, or Give to an agent"
    assert item(model, sf.MERGED[0])["reason"].startswith("landed in ") and " on main, " in item(model, sf.MERGED[0])["reason"]
    decorated = model._decorated(model.summaries[f"orchard/{sf.OPEN[0]}"])
    assert decorated["bucket"] == "needs-you" and decorated["reason"]


def test_replaced_failure_reason_names_the_new_run(scenario):
    model = model_for(scenario["layer"])
    for old, new in zip(sf.FAILED, sf.RESTARTS):
        assert item(model, old)["replaced_by"] == new
        assert item(model, old)["reason"] == f"replaced by {new} (running)"
        assert item(model, old)["bucket"] == "settled" and item(model, old)["group"] is None


def _row(run_id: str, status: str, started: float, **extra) -> dict:
    return {"key": f"l/{run_id}", "layer": "l", "id": run_id, "status": status, "started": started, "ended": started + 60,
            "outcome": {}, **extra}


def test_superseded_by_prompt_or_ancestry():
    ir = {"codebase": {"defaultBranch": "main"}}
    open_ = {"state": "open", "target": "main"}
    older = _row("11111111", "handed-over", 100, branch="al/11111111")
    by_prompt = _row("22222222", "passed", 200, prompt="Integrate al/11111111 with the rest",
                     outcome={"shipped": True})
    got = d.attention([older, by_prompt], {"l/11111111": open_}, ir)
    assert got["l/11111111"]["superseded_by"] == "22222222" and got["l/11111111"]["bucket"] == "settled"
    by_id = _row("22222222", "passed", 200, prompt="Finish what 11111111 began", outcome={"shipped": True})
    assert d.attention([older, by_id], {"l/11111111": open_}, ir)["l/11111111"]["superseded_by"] == "22222222"
    # a later run that did not land supersedes nothing
    unshipped = _row("22222222", "passed", 200, prompt="Integrate al/11111111", outcome={"shipped": False})
    assert d.attention([older, unshipped], {"l/11111111": open_}, ir)["l/11111111"]["bucket"] == "needs-you"
    # ancestry: the later landed run's tip holds the older one's, without naming it
    quiet = _row("33333333", "handed-over", 300, prompt="Tidy up", branch="al/33333333")
    landings = {"l/11111111": open_, "l/33333333": {"state": "landed", "how": "ancestor", "commit": "abc1234",
                                                    "target": "main", "at": 0}}
    contains = lambda older_, newer: older_["id"] == "11111111" and newer["id"] == "33333333"   # noqa: E731
    got = d.attention([older, quiet], landings, ir, contains)
    assert got["l/11111111"]["superseded_by"] == "33333333"
    assert d.attention([older, quiet], landings, ir)["l/11111111"]["bucket"] == "needs-you"
    # an earlier run does not supersede a later one
    earlier = _row("44444444", "passed", 50, prompt="Integrate al/11111111", outcome={"shipped": True})
    assert d.attention([older, earlier], {"l/11111111": open_}, ir)["l/11111111"]["superseded_by"] == ""


# ------------------------------------------------------------------------------ failure groups

def _failure(run_id: str, ended: float, step: str = "preflight", error: str | None = None) -> dict:
    return {"key": f"l/{run_id}", "id": run_id, "layer": "l", "status": "failed", "failed_step": step,
            "error": error if error is not None else sf.preflight_error(run_id, int(run_id[-1])),
            "started": ended - 40, "ended": ended}


def test_failure_groups_same_step_reason_window():
    base = 1_800_000_000
    five = [_failure(f"d000000{i}", base + 120 * i) for i in range(1, 6)]
    groups = d.failure_groups(five, "orchard")
    assert set(groups) == {f"l/d000000{i}" for i in range(1, 6)}
    group = groups["l/d0000001"]
    assert group["count"] == 5 and group["step"] == "preflight" and group["id"] == "orchard/preflight/d0000001"
    assert group["runs"] == [f"d000000{i}" for i in range(1, 6)]
    other_reason = [_failure("d0000001", base), _failure("d0000002", base + 60, error="preflight failed: no disk")]
    assert d.failure_groups(other_reason) == {}
    other_step = [_failure("d0000001", base), _failure("d0000002", base + 60, step="build")]
    assert d.failure_groups(other_step) == {}
    far = [_failure("d0000001", base), _failure("d0000002", base + d.FAILURE_GROUP_SECONDS + 1)]
    assert d.failure_groups(far) == {}
    assert d.failure_groups([_failure("d0000001", base)]) == {}


def test_failure_group_in_attention_on_the_group_scenario(tmp_path):
    sc = sf.build_group_scenario(tmp_path / "g")
    model = model_for(sc["layer"], ticks=1)
    groups = {json.dumps(item(model, r)["group"], sort_keys=True) for r in sf.FAILED}
    assert len(groups) == 1
    group = item(model, sf.FAILED[0])["group"]
    assert group["count"] == 5 and group["runs"] == sf.FAILED and group["step"] == "preflight"
    assert model.layer_list()[0]["needs_you"] == 5


def test_normalise_reason_drops_ids_numbers_paths():
    one = d.normalise_reason(sf.preflight_error("d0000001", 1) + "\nsecond line")
    two = d.normalise_reason(sf.preflight_error("d0000004", 4))
    assert one == two
    assert "d0000001" not in one and "/srv" not in one and "2026" not in one and "second" not in one
    assert one.startswith("preflight failed: the sandbox image al-orchard:")
    assert d.normalise_reason("exit 137 after 12.5 s in ./build/out at 10:04:01") == "exit after s in at"
    assert d.normalise_reason("") == ""


# ------------------------------------------------------------------------------ counts and bounds

def test_layer_list_needs_you_count(scenario):
    model = model_for(scenario["layer"])
    [layer] = model.layer_list()
    assert layer["needs_you"] == 6 == model.needs_you("orchard")
    assert model.layer_detail("orchard")["stats"]["needs_you"] == 6
    d.write_settlement(scenario["layer"], sf.OPEN[0], as_="abandoned", via="cli", status="handed-over")
    model.checked.clear()   # a finished run is read again after a few seconds; do not wait for them
    model.tick()
    assert model.layer_list()[0]["needs_you"] == 5
    assert item(model, sf.OPEN[0])["bucket"] == "settled"
    assert item(model, sf.OPEN[0])["reason"] == "abandoned"


def test_landing_checks_are_bounded_per_tick(tmp_path):
    repo = tmp_path / "orchard"   # where the layer's codebase.root points
    tips = sf.build_repo(repo, merge=False)
    layer = sf._layer(tmp_path, repo)
    git = sf.Git(repo)
    base = datetime.now(timezone.utc) - timedelta(days=1)
    runs = []
    for index in range(20):
        run_id = f"f{index:07x}"
        tip = tips.get(f"b{index + 1:07x}") or sf._branch(git, repo, run_id, {f"src/{run_id}.py": "X\n"})
        branch = f"al/b{index + 1:07x}" if f"b{index + 1:07x}" in tips else f"al/{run_id}"
        start = base + timedelta(minutes=index)
        sf.write_run(layer, run_id, prompt=f"Run {index}", status="handed-over", started=start.isoformat(),
                     ended=(start + timedelta(minutes=1)).isoformat(), branch=branch,
                     ship={"action": "pr", "shipped": False, "commit": tip, "target": ""})
        runs.append(run_id)

    def checking(model: d.Model) -> int:
        return sum(1 for r in runs if (item_of(model, r)["landing"] or {}).get("state") == "checking")

    def item_of(model: d.Model, run_id: str) -> dict:
        return model.attention[f"{model_key}/{run_id}"]

    model = d.Model([layer], [])
    model.discover()
    model_key = next(iter(model.layers))
    model.tick()
    assert checking(model) == 20 - d.LANDING_CHECKS_PER_TICK
    assert all(item_of(model, r)["bucket"] == "needs-you" for r in runs)
    model.tick()
    assert checking(model) == 20 - 2 * d.LANDING_CHECKS_PER_TICK
    model.tick()
    assert checking(model) == 0
    assert {(item_of(model, r)["landing"] or {}).get("state") for r in runs} == {"open"}
