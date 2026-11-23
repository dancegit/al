"""Approve & ship in the model (no server): a Needs-you run's `approval` of kind `ship` with its `ship`
facts, read from the agents/<id>/approvable.json its layer's runtime wrote (read_approvable); and the
settlement kind `shipped`, which takes a run out of Needs you until git shows its work on the target."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import dashboard as d
import settle_fixtures as sf
import ship_fixtures as shf

RUN = "f1000001"
URL = shf.PR_URL


@pytest.fixture(scope="module")
def scenario_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("ship-model") / "s"
    sf.build_scenario(root)
    return root


@pytest.fixture()
def scenario(tmp_path: Path, scenario_root: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    monkeypatch.delenv("AL_APP_ROOT", raising=False)
    root = tmp_path / "s"
    shutil.copytree(scenario_root, root, symlinks=True)
    return {"root": root, "repo": root / "orchard", "layer": root / "orchard.al"}


def model_for(layer: Path, ticks: int = 3) -> d.Model:
    model = d.Model([layer], [])
    model.discover()
    for _ in range(ticks):
        model.tick()
    return model


def _row(model: d.Model, run: str) -> dict:
    return next(r for r in model.snapshot()["runs"] if r["id"] == run)


def test_ship_record_gives_the_row_its_ship_facts(scenario):
    layer, repo = scenario["layer"], scenario["repo"]
    state = shf.add_ship_run(layer, repo, RUN)
    record = shf.record(layer, RUN)
    row = _row(model_for(layer), RUN)
    assert row["bucket"] == "needs-you"
    approval = row["approval"]
    assert (approval["kind"], approval["token"], approval["from_step"]) == ("ship", record["token"], "")
    assert approval["after"] == "the run's branch is merged into integration (fast-forward)"
    [item] = approval["items"]
    assert item["id"] == "ship" and item["text"].startswith(f"{shf.DIFF}: 3148 lines in 1 file(s) → integration")
    assert approval["ship"] == {
        "way": "merge", "label": "Approve & merge", "target": "integration", "commit": state["ship"]["commit"],
        "diff_lines": 3148, "files_total": 1, "files": [f"shelf_{RUN}.py"], "reasons": [shf.DIFF],
        "checks": [{"name": "test", "phase": "check", "state": "passed"}], "pr_url": ""}
    assert model_for(layer).waiting_approval("orchard") == 1

    # a hostile text stays text, a URL that is not http(s) is not passed on, and counts are numbers
    hostile = "<img src=x onerror=alert(1)>"
    shf.record(layer, RUN, ship={"target": hostile, "label": hostile, "reasons": [hostile, 7], "files": [hostile] * 30,
                                 "files_total": "many", "diff_lines": -4,
                                 "pr": {"number": 7, "url": "javascript:alert(1)"},
                                 "checks": [{"name": hostile, "phase": "check", "state": "passed"}, "x"]})
    on_disk = json.loads((layer / "agents" / RUN / "state.json").read_text())
    ship = d.read_approvable(layer, RUN, on_disk)["ship"]
    assert ship["target"] == hostile and ship["label"] == hostile[:40] and ship["reasons"] == [hostile]
    assert ship["files"] == [hostile] * 20 and ship["files_total"] == 0 and ship["diff_lines"] == 0
    assert ship["pr_url"] == "" and ship["checks"] == [{"name": hostile, "phase": "check", "state": "passed"}]
    shf.record(layer, RUN, ship={"pr": {"number": 7, "url": URL}, "way": "pr", "label": "Approve & ship"})
    ship = d.read_approvable(layer, RUN, on_disk)["ship"]
    assert (ship["way"], ship["label"], ship["pr_url"]) == ("pr", "Approve & ship", URL)

    # a kind `ship` without a valid `ship` object is not shown
    shf.record(layer, RUN, with_ship=False)
    assert d.read_approvable(layer, RUN, on_disk) is None
    shf.record(layer, RUN, ship={"way": "force-push"})
    assert d.read_approvable(layer, RUN, on_disk) is None
    # and a record about an earlier state of the run is ignored, as for every kind
    shf.record(layer, RUN)
    assert d.read_approvable(layer, RUN, {**on_disk, "approvals": [{"what": f"ship:{shf.DIFF}"}]}) is None


def test_shipped_settlement_leaves_needs_you_until_git_shows_it_landed(scenario):
    layer, repo = scenario["layer"], scenario["repo"]
    run = sf.OPEN[0]
    assert model_for(layer).attention[f"orchard/{run}"]["bucket"] == "needs-you"
    record = d.write_settlement(layer, run, as_="shipped", by=URL, note="agreed", who="Ada", via="cli",
                                status="handed-over", repo=repo)
    assert (record["as"], record["by"]) == ("shipped", URL) and d.read_settlement(layer, run)["as"] == "shipped"
    model = model_for(layer)
    item = model.attention[f"orchard/{run}"]
    assert item["bucket"] == "settled" and item["reason"] == f"shipped ({URL}) by Ada: agreed"
    assert _row(model, run)["settled"]["by"] == URL
    assert next(entry for entry in model.layer_list() if entry["key"] == "orchard")["needs_you"] == 5

    sf.Git(repo)("merge", "-q", "--no-ff", "-m", f"Merge al/{run}", f"al/{run}")
    item = model_for(layer).attention[f"orchard/{run}"]
    assert item["bucket"] == "landed" and item["reason"].startswith("landed in ") and " on main" in item["reason"]
    assert item["settled"]["as"] == "shipped", "the record stays"
    # another settlement of a merged run is not looked at again: it stays what the person said
    other = sf.OPEN[1]
    d.write_settlement(layer, other, as_="abandoned", who="Ada", via="cli", status="handed-over", repo=repo)
    sf.Git(repo)("merge", "-q", "--no-ff", "-m", f"Merge al/{other}", f"al/{other}")
    assert model_for(layer).attention[f"orchard/{other}"]["bucket"] == "settled"


def test_check_settlement_takes_a_url_only_for_shipped(scenario):
    commit = "c0ffee1"
    assert d.check_settlement("shipped", f"  {URL} ", "", "Ada") == ("shipped", URL, "", "Ada")
    assert d.check_settlement("shipped", commit, "", "Ada")[1] == commit
    for by in ("", None, "not a url", "ftp://forge.example/pull/7", "https://forge.example/a b", "javascript:alert(1)",
               "https://forge.example/" + "x" * 300):
        with pytest.raises(d.SettleError, match="shipped needs by: the pull request's URL or the commit"):
            d.check_settlement("shipped", by, "", "Ada")
    for as_ in ("landed", "superseded", "abandoned"):
        with pytest.raises(d.SettleError, match="by names a run"):
            d.check_settlement(as_, URL, "", "Ada")
        assert d.check_settlement(as_, commit, "", "Ada")[1] == commit
    layer, repo = scenario["layer"], scenario["repo"]
    with pytest.raises(d.SettleError):                    # a commit is still looked up, also for shipped
        d.write_settlement(layer, sf.OPEN[2], as_="shipped", by="deadbeefdeadbeef", via="cli", status="handed-over",
                           repo=repo)
    assert d.read_settlement(layer, sf.OPEN[2]) is None
