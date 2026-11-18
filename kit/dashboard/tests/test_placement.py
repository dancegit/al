"""Where `create` and `adopt` put the layer: beside the repository (`<name>.al`, the default) or inside
it (`<repository>/agentic`). One function, `placement_view`, computes the path and whether it is usable;
it feeds the page (GET /api/repo) and the guard of POST /api/action, whose `"placement"` field reaches
every step's argv and is bound to the preview's nonce. The al tool is `fake_al`, which records its argv."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import dashboard as d
import workspace_fixtures as wf
from test_actions import confirmed, job_file, post, real, wait_job


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    wf.build(root)
    return root


@pytest.fixture()
def al(tmp_path: Path) -> Path:
    return wf.fake_al(tmp_path / "bin" / "al", out="# LLM.txt draft")


def shown_in(ws: Path):
    """Paths as the page shows them: relative to the workspace folder, prefixed with its name."""
    root = os.path.realpath(ws)

    def shown(path: Path) -> str:
        real_path = os.path.realpath(path)
        return "ws" if real_path == root else f"ws/{os.path.relpath(real_path, root)}"
    return shown


def view(ws: Path, repo: str, placement: str) -> dict:
    return d.placement_view(Path(real(ws / repo)), placement, shown_in(ws))


def layer_dir(ws: Path, repo: str, placement: str) -> Path:
    return ws / f"{repo}.al" if placement == "beside" else ws / repo / "agentic"


def confirmed_in(running, repo: str, action: str, placement: str) -> str:
    status, preview = post(running, {"repo": repo, "action": action, "placement": placement})
    assert status == 200 and preview["nonce"], preview
    if preview.get("job"):
        wait_job(running, preview["job"])
    status, body = post(running, {"repo": repo, "action": action, "placement": placement, "confirm": preview["nonce"]})
    assert status == 202, body
    return body["job"]


# ------------------------------------------------------------------------------------------ the model

def test_placement_layer_is_the_sibling_or_the_in_repo_folder(ws):
    app = Path(real(ws / "fern"))
    assert d.placement_layer(app, "beside") == app.parent / "fern.al"
    assert d.placement_layer(app, "inside") == app / "agentic"
    assert d.PLACEMENTS == ("beside", "inside") and d.IN_REPO_DIR == "agentic"


def test_view_of_a_fresh_repository_is_usable_for_both(ws):
    for placement, path in (("beside", "ws/fern.al"), ("inside", "ws/fern/agentic")):
        found = view(ws, "fern", placement)
        assert found["name"] == placement and found["path"] == path
        assert (found["exists"], found["writable"], found["layer"], found["ok"], found["problem"]) == (
            False, True, False, True, ""), found
        assert found["label"] == f"{placement} the repository" and found["explain"]
    assert set(view(ws, "fern", "beside")) == {"name", "label", "explain", "path", "exists", "writable", "layer", "ok",
                                               "problem"}


def test_an_existing_folder_is_reported_and_kept(ws):
    (ws / "fern.al").mkdir()
    (ws / "fern.al" / "LLM.txt").write_text("a draft from an earlier attempt\n")
    found = view(ws, "fern", "beside")
    assert found["exists"] is True and found["ok"] is True and found["layer"] is False and found["problem"] == ""
    assert (ws / "fern.al" / "LLM.txt").read_text() == "a draft from an earlier attempt\n"


@pytest.mark.parametrize("placement", ["beside", "inside"])
@pytest.mark.parametrize("name, sentence", [("agentic.rkt", "never overwritten"), ("agentic-layer.json", "is already a layer")])
def test_a_path_that_is_already_a_layer_is_refused(ws, placement, name, sentence):
    layer = layer_dir(ws, "fern", placement)
    layer.mkdir()
    (layer / name).write_text("#lang al\n" if name == "agentic.rkt" else "{}\n")
    found = view(ws, "fern", placement)
    assert found["layer"] is True and found["ok"] is False and found["exists"] is True
    assert found["path"] in found["problem"] and sentence in found["problem"], found


def test_a_file_or_symlink_at_the_path_is_refused(ws, tmp_path):
    (ws / "fern.al").write_text("a file, not a folder\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, ws / "fern" / "agentic")
    for placement in d.PLACEMENTS:
        found = view(ws, "fern", placement)
        assert found["ok"] is False and found["exists"] is True and "is not a directory" in found["problem"], found
        assert found["path"] in found["problem"]
    assert list(elsewhere.iterdir()) == []


def test_an_unwritable_folder_is_refused(ws):
    if os.geteuid() == 0:
        pytest.skip("root may write anywhere")
    for placement, folder, named in (("beside", ws, "ws"), ("inside", ws / "fern", "ws/fern")):
        mode = folder.stat().st_mode & 0o7777
        folder.chmod(0o555)
        try:
            found = view(ws, "fern", placement)
        finally:
            folder.chmod(mode)
        assert found["writable"] is False and found["ok"] is False, found
        assert found["problem"] == f"{named} is not writable"
        assert view(ws, "fern", placement)["ok"] is True


def test_inside_is_refused_when_the_repository_tracks_files_there(ws):
    (ws / "fern" / "agentic").mkdir()
    (ws / "fern" / "agentic" / "notes.txt").write_text("the team's own notes\n")
    wf.commit(ws / "fern", "fern: notes in agentic/")
    inside = view(ws, "fern", "inside")
    assert inside["ok"] is False and inside["layer"] is False and "already has tracked files" in inside["problem"]
    assert "ws/fern/agentic" in inside["problem"] and "choose beside" in inside["problem"]
    assert view(ws, "fern", "beside")["ok"] is True


def test_action_views_list_both_placements_for_create_and_adopt(ws, al, tmp_path, workspace_server):
    running = workspace_server(ws, al=al)
    detail = running.json("/api/repo?repo=fern")
    actions = {a["name"]: a for a in detail["actions"]}
    create = actions["create"]
    assert [p["name"] for p in create["placements"]] == ["beside", "inside"]
    beside, inside = create["placements"]
    assert inside["commands"][0] == "al init ws/fern --in-repo" and inside["paths"] == ["ws/fern/agentic"]
    assert inside["path"] == "ws/fern/agentic" and inside["ok"] is True and inside["exists"] is False
    assert beside["commands"][0] == "al init ws/fern" and beside["paths"] == ["ws/fern.al"]
    assert create["commands"] == beside["commands"] and create["paths"] == beside["paths"]
    assert create["preview"] == beside["preview"] == inside["preview"] == ["al create ws/fern --choices --json"]
    assert "beside or inside" in create["description"]
    for name in ("scout", "gen", "check", "verify", "explain"):
        assert "placements" not in actions[name], name
    assert "placements" not in actions["adopt"]            # fern has no setup to adopt
    comet = {a["name"]: a for a in running.json("/api/repo?repo=comet")["actions"]}
    assert [p["name"] for p in comet["adopt"]["placements"]] == ["beside", "inside"]
    assert comet["adopt"]["placements"][1]["preview"][0] == "al adopt ws/comet/agentic --app ws/comet --dry-run --diff"
    assert comet["adopt"]["placements"][1]["paths"][0] == "ws/comet/agentic"
    assert comet["create"]["placements"][1]["path"] == "ws/comet/agentic"
    pantry = {a["name"]: a for a in running.json("/api/repo?repo=pantry")["actions"]}
    assert "placements" not in pantry["create"]            # it has a layer: create is not for it
    text = json.dumps([detail["actions"], detail["repo"], comet, pantry])
    assert str(tmp_path) not in text and real(tmp_path) not in text and str(Path.home()) not in text
    assert wf.calls(al) == []


# ------------------------------------------------------------------------------------------ HTTP

def test_default_placement_is_beside(ws, al, tmp_path, workspace_server):
    running = workspace_server(ws, al=al)
    job_id = confirmed(running, "fern", "create")
    assert wait_job(running, job_id)["status"] == "passed"
    record = job_file(tmp_path / "jobs", job_id)
    assert record["placement"] == "beside" and record["steps"][0]["argv"][1:] == ["init", real(ws / "fern")]
    assert running.json(f"/api/job?id={job_id}")["placement"] == "beside"
    scout = wait_job(running, post(running, {"repo": "fern", "action": "scout"})[1]["job"])
    assert scout["placement"] == ""                         # an action that creates no layer has none


def test_preview_names_the_path_of_the_chosen_placement(ws, al, workspace_server):
    running = workspace_server(ws, al=al)
    status, preview = post(running, {"repo": "fern", "action": "create", "placement": "inside"})
    assert status == 200, preview
    assert preview["placement"] == {"name": "inside", "path": "ws/fern/agentic"}
    assert preview["writes"] == ["ws/fern/agentic"] and preview["commands"][0] == "al init ws/fern --in-repo"
    wait_job(running, preview["job"])
    assert wf.calls(al) == [["create", real(ws / "fern"), "--choices", "--json"]]
    assert not (ws / "fern" / "agentic").exists()
    status, preview = post(running, {"repo": "fern", "action": "create", "placement": "beside"})
    assert status == 200 and preview["placement"] == {"name": "beside", "path": "ws/fern.al"}
    wait_job(running, preview["job"])
    status, preview = post(running, {"repo": "pantry", "action": "gen"})
    assert status == 200 and "placement" not in preview


def test_inside_reaches_every_step_of_the_job(ws, al, tmp_path, workspace_server):
    running = workspace_server(ws, al=al)
    app = real(ws / "fern")
    layer = app + "/agentic"
    job_id = confirmed_in(running, "fern", "create", "inside")
    assert wait_job(running, job_id)["status"] == "passed"
    preview_call, *steps = wf.calls(al)
    assert preview_call == ["create", app, "--choices", "--json"]
    assert steps == [["init", app, "--in-repo"], ["scout", app, "--into", layer, "--raw"], ["adopt", layer, "--app", app],
                     ["author", layer, "--raw"], ["gen", layer], ["check", layer], ["result", layer, "--json"]]
    record = job_file(tmp_path / "jobs", job_id)
    assert record["placement"] == "inside" and running.json(f"/api/job?id={job_id}")["placement"] == "inside"
    assert record["layer_display"] == "ws/fern/agentic"
    assert not (ws / "fern.al").exists()

    comet = real(ws / "comet")
    before = len(wf.calls(al))
    job_id = confirmed_in(running, "comet", "adopt", "inside")
    assert wait_job(running, job_id)["status"] == "passed"
    made = wf.calls(al)[before:]
    assert made[:2] == [["adopt", comet + "/agentic", "--app", comet, "--dry-run", "--diff"],
                        ["create", comet, "--choices", "--json"]]
    assert made[2:] == [["init", comet, "--in-repo"], ["scout", comet, "--into", comet + "/agentic", "--raw"],
                        ["adopt", comet + "/agentic", "--app", comet], ["author", comet + "/agentic", "--raw"],
                        ["gen", comet + "/agentic"], ["check", comet + "/agentic"],
                        ["result", comet + "/agentic", "--json"]]
    assert job_file(tmp_path / "jobs", job_id)["placement"] == "inside"
    assert running.json(f"/api/job?id={job_id}")["placement"] == "inside"


@pytest.mark.parametrize("repo, action, placement", [
    ("fern", "create", "elsewhere"),
    ("fern", "create", 1),
    ("fern", "create", ["inside"]),
    ("fern", "create", "inside --dir x"),
    ("fern", "create", ""),
    ("pantry", "gen", "inside"),
    ("fern", "scout", "inside"),
    ("fern", "scout", "beside"),
])
def test_placement_is_validated(ws, al, tmp_path, workspace_server, repo, action, placement):
    running = workspace_server(ws, al=al)
    status, answer = post(running, {"repo": repo, "action": action, "placement": placement})
    assert status == 400, answer
    assert wf.calls(al) == [] and not list((tmp_path / "jobs").glob("*/job.json"))


def test_confirmation_is_bound_to_the_previewed_placement(ws, al, tmp_path, workspace_server):
    running = workspace_server(ws, al=al)
    status, preview = post(running, {"repo": "fern", "action": "create", "placement": "beside"})
    assert status == 200
    wait_job(running, preview["job"])
    status, body = post(running, {"repo": "fern", "action": "create", "placement": "inside", "confirm": preview["nonce"]})
    assert status == 409 and "placement changed since the preview" in body["error"], body
    status, body = post(running, {"repo": "fern", "action": "create", "placement": "beside", "confirm": preview["nonce"]})
    assert status == 409 and "unknown, used or expired" in body["error"], body
    assert wf.calls(al) == [["create", real(ws / "fern"), "--choices", "--json"]]
    assert len(list((tmp_path / "jobs").glob("*/job.json"))) == 1
    assert not (ws / "fern.al").exists() and not (ws / "fern" / "agentic").exists()
    # a preview without the key is a preview of beside: confirming it as inside is refused too
    status, preview = post(running, {"repo": "fern", "action": "create"})
    wait_job(running, preview["job"])
    status, body = post(running, {"repo": "fern", "action": "create", "placement": "inside", "confirm": preview["nonce"]})
    assert status == 409 and "placement changed since the preview" in body["error"], body


def test_unusable_placement_is_refused_before_any_job(ws, al, tmp_path, workspace_server):
    running = workspace_server(ws, al=al)
    (ws / "fern.al").mkdir()
    (ws / "fern.al" / "agentic-layer.json").write_text("{}\n")
    status, body = post(running, {"repo": "fern", "action": "create", "placement": "beside"})
    assert status == 409 and "ws/fern.al" in body["error"] and "is already a layer" in body["error"], body
    assert wf.calls(al) == [] and not list((tmp_path / "jobs").glob("*/job.json"))
    status, preview = post(running, {"repo": "fern", "action": "create", "placement": "inside"})
    assert status == 200, preview
    wait_job(running, preview["job"])
    made = len(wf.calls(al))
    (ws / "fern" / "agentic").mkdir()
    (ws / "fern" / "agentic" / "notes.txt").write_text("the team's own notes\n")
    wf.commit(ws / "fern", "fern: notes in agentic/")
    for confirm in ({}, {"confirm": preview["nonce"]}):
        status, body = post(running, {"repo": "fern", "action": "create", "placement": "inside", **confirm})
        assert status == 409 and "already has tracked files" in body["error"], body
    assert len(wf.calls(al)) == made and len(list((tmp_path / "jobs").glob("*/job.json"))) == 1


def test_placement_passes_the_same_write_guard(ws, al, tmp_path, workspace_server):
    body = {"repo": "fern", "action": "create", "placement": "inside"}
    running = workspace_server(ws, al=al)
    assert post(running, body, X_AL_Dashboard=None)[0] == 403
    assert post(running, body, Origin="http://evil.example")[0] == 403
    assert post(running, body, "/api/generate") == (405, {"error": "read-only"})
    read_only = workspace_server(ws, "--read-only", al=al, jobs=tmp_path / "jobs-read-only")
    status, answer = post(read_only, body)
    assert status == 403 and "--read-only" in answer["error"]
    no_actions = workspace_server(ws, "--no-actions", al=al, jobs=tmp_path / "jobs-no-actions")
    status, answer = post(no_actions, body)
    assert status == 403 and "actions are off" in answer["error"]
    assert wf.calls(al) == [] and not list(tmp_path.glob("jobs*/*/job.json"))
    assert not (ws / "fern" / "agentic").exists()


def test_a_layer_created_inside_is_listed_as_in_repo(ws, tmp_path, workspace_server):
    al = wf.fake_al(tmp_path / "bin" / "al", writes={"author": {"{arg2}/agentic.rkt": "#lang al\n"}})
    running = workspace_server(ws, al=al)
    job_id = confirmed_in(running, "fern", "create", "inside")
    job = wait_job(running, job_id)
    assert job["status"] == "passed"
    assert {"path": "fern/agentic/agentic.rkt", "change": "added"} in job["changed"]
    assert job["repo_unchanged"] is False
    deadline = time.monotonic() + 30
    while True:
        fern = next(r for r in running.json("/api/workspace?refresh=1")["repos"] if r["name"] == "fern")
        if fern["state"] == "layer" or time.monotonic() > deadline:
            break
        time.sleep(0.25)
    assert (fern["state"], fern["layer_kind"], fern["layer"]) == ("layer", "in-repo", "ws/fern/agentic")
    assert not (ws / "fern.al").exists()
