"""The actions on a repository that already has a layer: snapshot, regen, reauthor, improve, and restore
(which has no button of its own: it starts from the Snapshots card or a failed job's outcome). Each is a
fixed argv list built from the scanned repository; the typed values (a snapshot id, an improve request)
are checked and passed as single argv elements, and a confirmation is bound to them. The al tool is
`fake_al`, which records its argv; the workspace is workspace_fixtures (pantry: an in-repo layer)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import dashboard as d
import workspace_fixtures as wf
from test_actions import job_file, post, real, wait_job

LAYER_ACTIONS = ("snapshot", "regen", "reauthor", "improve")
SNAP = "20261119T101500Z"


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    wf.build(root)
    return root


@pytest.fixture()
def al(tmp_path: Path) -> Path:
    return wf.fake_al(tmp_path / "bin" / "al", out=json.dumps({"id": SNAP, "kind": "archive"}))


def views(running, repo: str) -> dict[str, dict]:
    return {a["name"]: a for a in running.json(f"/api/repo?repo={repo}")["actions"]}


def record(layer: Path, snap_id: str, note: str = "by hand", kind: str = "archive") -> Path:
    path = layer / ".work" / "snapshots" / f"{snap_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": snap_id, "kind": kind, "created": "2026-11-19T10:15:00Z", "note": note,
                                "archive": f".work/snapshots/{snap_id}.tar.gz", "files": 3}))
    return path


def preview_and_confirm(running, body: dict, confirm_extra: dict | None = None) -> tuple[dict, str]:
    status, preview = post(running, body)
    assert status == 200 and preview["nonce"], preview
    if preview.get("job"):
        assert wait_job(running, preview["job"])["status"] == "passed"
    status, started = post(running, {**body, **(confirm_extra or {}), "confirm": preview["nonce"]})
    assert status == 202, started
    return preview, started["job"]


def step_argvs(tmp_path: Path, job_id: str) -> dict[str, list[str]]:
    return {s["name"]: s["argv"] for s in job_file(tmp_path / "jobs", job_id)["steps"]}


def doc(running, job_id: str, name: str) -> str:
    return running.json(f"/api/job/doc?id={job_id}&name={name}")["text"]


# ------------------------------------------------------------------------------------------ offered

def test_layer_actions_are_offered_for_a_layer_only(ws, al, workspace_server):
    running = workspace_server(ws, al=al)
    pantry = views(running, "pantry")
    for name in LAYER_ACTIONS:
        assert pantry[name]["available"] is True and pantry[name]["writes"] is True, pantry[name]
    assert "restore" not in pantry
    assert pantry["improve"]["param"] == "request" and pantry["snapshot"]["param"] == ""
    for repo, state in (("fern", "none"), ("quill", "setup")):
        listed = views(running, repo)
        for name in LAYER_ACTIONS:
            assert listed[name]["available"] is False
            assert listed[name]["why"].startswith(f"not for this state ({state})"), listed[name]
    for name in LAYER_ACTIONS:
        status, body = post(running, {"repo": "fern", "action": name,
                                      **({"request": "a change"} if name == "improve" else {})})
        assert status == 409 and "its state is none" in body["error"], body
    assert wf.calls(al) == []


def test_layer_action_command_lines_are_fixed(ws, al, tmp_path, workspace_server):
    running = workspace_server(ws, al=al)
    pantry = views(running, "pantry")
    assert pantry["snapshot"]["commands"] == ["al snapshot ws/pantry/agentic --note 'from the dashboard' --json"]
    assert pantry["regen"]["commands"] == ["al snapshot ws/pantry/agentic --note 'before regen' --json",
                                           "al gen ws/pantry/agentic --discard-edits"]
    assert pantry["regen"]["preview"] == ["al gen ws/pantry/agentic --list-edits"]
    assert pantry["reauthor"]["commands"] == [
        "al snapshot ws/pantry/agentic --note 'before reauthor' --json",
        "al author ws/pantry/agentic --set-aside",
        "al scout ws/pantry --into ws/pantry/agentic --raw",
        "al author ws/pantry/agentic --raw",
        "al gen ws/pantry/agentic",
        "al check ws/pantry/agentic",
        "al author ws/pantry/agentic --diff-set-aside"]
    assert pantry["reauthor"]["preview"] == ["al create ws/pantry --choices --json"]
    assert pantry["improve"]["commands"] == ["al improve ws/pantry/agentic -- REQUEST"]
    detail = running.json("/api/repo?repo=pantry")
    shown = json.dumps([detail["actions"], detail["help"], detail["snapshots"]])
    assert str(tmp_path) not in shown and real(tmp_path) not in shown
    _preview, job_id = preview_and_confirm(running, {"repo": "pantry", "action": "snapshot"})
    assert wait_job(running, job_id)["status"] == "passed"
    assert step_argvs(tmp_path, job_id)["snapshot"] == [real(al), "snapshot", real(ws / "pantry" / "agentic"),
                                                        "--note", "from the dashboard", "--json"]


# ------------------------------------------------------------------------------------------ regen

def test_regen_preview_lists_hand_edited_files(ws, tmp_path, workspace_server):
    layer = real(ws / "pantry" / "agentic")
    al = wf.fake_al(tmp_path / "bin" / "al", out="  commands/plan.md   al eject …\n1 hand-edited generated file(s) would be "
                                                  "overwritten by al gen ws/pantry/agentic --discard-edits")
    running = workspace_server(ws, al=al)
    status, preview = post(running, {"repo": "pantry", "action": "regen"})
    assert status == 200 and preview["job"]
    assert wait_job(running, preview["job"])["status"] == "passed"
    assert wf.calls(al) == [["gen", layer, "--list-edits"]]
    assert "commands/plan.md" in doc(running, preview["job"], "hand-edits.txt")
    wf.fake_al(al, out="no hand-edited generated files: the plain al gen ws/pantry/agentic is enough")
    status, again = post(running, {"repo": "pantry", "action": "regen"})
    assert status == 200
    wait_job(running, again["job"])
    assert doc(running, again["job"], "hand-edits.txt").startswith("no hand-edited generated files")
    status, started = post(running, {"repo": "pantry", "action": "regen", "confirm": again["nonce"]})
    assert status == 202
    assert wait_job(running, started["job"])["status"] == "passed"
    assert [c[:2] for c in wf.calls(al)[2:]] == [["snapshot", layer], ["gen", layer]]
    assert wf.calls(al)[-1] == ["gen", layer, "--discard-edits"]


# ------------------------------------------------------------------------------------------ reauthor

def test_reauthor_starts_with_a_snapshot_and_keeps_run_history(ws, al, tmp_path, workspace_server):
    layer = ws / "pantry" / "agentic"
    state = layer / "agents" / "abc12345" / "state.json"
    rules = layer / "adopted" / "rules" / "CLAUDE.md"
    for path, text in ((state, "{}\n"), (rules, "# rules\n")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    running = workspace_server(ws, al=al)
    _preview, job_id = preview_and_confirm(running, {"repo": "pantry", "action": "reauthor"},
                                           {"choices": {"forge": "github", "repo": "acme/pantry"}})
    view = wait_job(running, job_id)
    assert view["status"] == "passed", view
    assert [s["name"] for s in view["steps"]] == ["snapshot", "set-aside", "scout", "author", "gen", "check", "diff"]
    argvs = step_argvs(tmp_path, job_id)
    for argv in argvs.values():
        assert "rm" not in argv and "--discard-edits" not in argv
        assert not [a for a in argv if "/agents/" in a or "/adopted/" in a or a.endswith(("/agents", "/adopted"))]
    assert argvs["author"][-4:] == ["--choice", "forge=github", "--choice", "repo=acme/pantry"]
    assert argvs["set-aside"][1:] == ["author", real(layer), "--set-aside"]
    assert state.is_file() and rules.read_text() == "# rules\n"
    assert view["outcome"]["diff"] == "definition.diff.txt"
    assert view["outcome"]["next"]


def test_a_failing_author_step_offers_the_restore(ws, tmp_path, workspace_server):
    layer = ws / "pantry" / "agentic"
    al = wf.fake_al(tmp_path / "bin" / "al", out=json.dumps({"id": SNAP, "kind": "archive"}),
                    fail={"author --raw": (1, "al: author: the agent exited 1")})   # not the set-aside
    record(layer, SNAP)
    running = workspace_server(ws, al=al)
    _preview, job_id = preview_and_confirm(running, {"repo": "pantry", "action": "reauthor"})
    view = wait_job(running, job_id)
    assert view["status"] == "failed"
    statuses = {s["name"]: s["status"] for s in view["steps"]}
    assert statuses["author"] == "failed" and statuses["set-aside"] == statuses["scout"] == "passed"
    assert statuses["gen"] == statuses["check"] == statuses["diff"] == "skipped"
    assert view["outcome"]["restore"] == {"snapshot": SNAP,
                                          "command": f"al snapshot ws/pantry/agentic --restore {SNAP}"}
    assert view["outcome"]["step"] == "author" and view["outcome"]["definition_aside"] is True

    wf.fake_al(al, out=json.dumps({"id": SNAP, "kind": "archive"}))
    before = len(wf.calls(al))
    status, preview = post(running, {"repo": "pantry", "action": "restore", "snapshot": SNAP})
    assert status == 200 and preview["job"] and preview["commands"] == [
        f"al snapshot ws/pantry/agentic --restore {SNAP}"]
    assert wait_job(running, preview["job"])["status"] == "passed"
    assert wf.calls(al)[before:] == [["snapshot", real(layer), "--restore", SNAP, "--dry-run"]]
    status, started = post(running, {"repo": "pantry", "action": "restore", "snapshot": SNAP,
                                     "confirm": preview["nonce"]})
    assert status == 202, started
    assert wait_job(running, started["job"])["status"] == "passed"
    assert wf.calls(al)[-1] == ["snapshot", real(layer), "--restore", SNAP]

    (layer / "agentic.rkt").unlink()             # set aside and committed: the layer is no longer scanned as one
    wf.commit(ws / "pantry", "pantry: the definition is set aside")
    fresh = workspace_server(ws, al=al, jobs=tmp_path / "jobs2")
    assert fresh.json("/api/repo?repo=pantry")["repo"]["state"] != "layer"
    status, preview = post(fresh, {"repo": "pantry", "action": "restore", "snapshot": SNAP})
    assert status == 200, preview
    wait_job(fresh, preview["job"])
    status, started = post(fresh, {"repo": "pantry", "action": "restore", "snapshot": SNAP, "confirm": preview["nonce"]})
    assert status == 202, started
    assert wait_job(fresh, started["job"])["status"] == "passed"
    assert wf.calls(al)[-1] == ["snapshot", real(layer), "--restore", SNAP]


def test_a_reauthor_failing_after_the_set_aside_says_the_old_definition_is_kept(ws, tmp_path, workspace_server):
    al = wf.fake_al(tmp_path / "bin" / "al", out=json.dumps({"id": SNAP, "kind": "archive"}),
                    fail={"scout": (1, "al: scout: the agent exited 1")})
    running = workspace_server(ws, al=al)
    _preview, job_id = preview_and_confirm(running, {"repo": "pantry", "action": "reauthor"})
    view = wait_job(running, job_id)
    assert view["status"] == "failed" and view["outcome"]["step"] == "scout"
    assert view["outcome"]["definition_aside"] is True and view["outcome"]["restore"]["snapshot"] == SNAP


def test_restore_refuses_unknown_or_malformed_ids(ws, al, tmp_path, workspace_server):
    record(ws / "pantry" / "agentic", SNAP)
    running = workspace_server(ws, al=al)
    status, body = post(running, {"repo": "pantry", "action": "restore", "snapshot": "../x"})
    assert status == 400 and "not a snapshot id" in body["error"]
    status, body = post(running, {"repo": "pantry", "action": "restore", "snapshot": "20000101T000000Z"})
    assert status == 409 and "no snapshot" in body["error"]
    status, body = post(running, {"repo": "pantry", "action": "restore"})
    assert status == 400 and "restore needs a snapshot id" in body["error"]
    status, body = post(running, {"repo": "pantry", "action": "gen", "snapshot": SNAP})
    assert status == 400
    status, preview = post(running, {"repo": "pantry", "action": "restore", "snapshot": SNAP})
    assert status == 200
    wait_job(running, preview["job"])
    assert all("--dry-run" in c for c in wf.calls(al))         # without a confirm only the dry run ran
    other = "20261119T101500Z-2"
    record(ws / "pantry" / "agentic", other)
    status, body = post(running, {"repo": "pantry", "action": "restore", "snapshot": other, "confirm": preview["nonce"]})
    assert status == 409 and "changed since the preview" in body["error"]
    assert all("--dry-run" in c for c in wf.calls(al))


# ------------------------------------------------------------------------------------------ improve

def test_improve_refuses_an_empty_request(ws, al, tmp_path, workspace_server):
    running = workspace_server(ws, al=al)
    base = {"repo": "pantry", "action": "improve"}
    for request in (None, "", "   "):
        status, body = post(running, {**base, **({"request": request} if request is not None else {})})
        assert status == 400 and "improve needs a request" in body["error"], (request, body)
    for request in ("a" * 2001, "a\nb", "a\x00"):
        assert post(running, {**base, "request": request})[0] == 400, request
    assert post(running, {"repo": "pantry", "action": "gen", "request": "a change"})[0] == 400
    assert wf.calls(al) == []
    text = "Every plan ends with a Rollback section"
    status, preview = post(running, {**base, "request": text})
    assert status == 200 and preview["commands"] == [f"al improve ws/pantry/agentic -- '{text}'"]
    status, body = post(running, {**base, "request": "Something else", "confirm": preview["nonce"]})
    assert status == 409 and "changed since the preview" in body["error"]
    status, preview = post(running, {**base, "request": f"  {text} "})
    status, started = post(running, {**base, "request": text, "confirm": preview["nonce"]})
    assert status == 202, started
    assert wait_job(running, started["job"])["status"] == "passed"
    assert wf.calls(al) == [["improve", real(ws / "pantry" / "agentic"), "--", text]]
    _preview, job_id = preview_and_confirm(running, {**base, "request": "--dry-run please"})
    wait_job(running, job_id)
    assert wf.calls(al)[-1] == ["improve", real(ws / "pantry" / "agentic"), "--", "--dry-run please"]


# ------------------------------------------------------------------------------------------ the Repos screen

def test_repo_page_lists_snapshots_with_a_restore_command(ws, al, tmp_path, workspace_server):
    layer = ws / "pantry" / "agentic"
    record(layer, "20261119T101500Z", "one")
    record(layer, "20261119T101500Z-2", "two", kind="git")
    (layer / ".work" / "snapshots" / "not-an-id.json").write_text(json.dumps({"id": "not-an-id"}))
    (layer / ".work" / "snapshots" / "20261120T000000Z.json").write_text("not json")
    running = workspace_server(ws, al=al)
    detail = running.json("/api/repo?repo=pantry")
    assert [s["id"] for s in detail["snapshots"]] == ["20261119T101500Z-2", "20261119T101500Z"]
    for row in detail["snapshots"]:
        assert row["restore"] == f"al snapshot ws/pantry/agentic --restore {row['id']}"
    assert detail["snapshots"][0]["note"] == "two" and detail["snapshots"][0]["kind"] == "git"
    shown = json.dumps(detail["snapshots"])
    assert str(tmp_path) not in shown and real(tmp_path) not in shown
    assert running.json("/api/repo?repo=fern")["snapshots"] == []


def test_repo_page_help_says_when_to_use_each(ws, al, workspace_server):
    running = workspace_server(ws, al=al)
    help_lines = running.json("/api/repo?repo=pantry")["help"]
    assert [line.split(":")[0] for line in help_lines] == ["gen", "regen", "reauthor", "improve"]
    assert all("\n" not in line for line in help_lines)
    assert running.json("/api/repo?repo=fern")["help"] == []


def test_snapshot_records_are_read_like_al_reads_them(tmp_path):
    layer = tmp_path / "notes.al"
    record(layer, "20261119T101500Z")
    record(layer, "20261119T101500Z-10")
    record(layer, "20261119T101500Z-2")
    assert [r["id"] for r in d.snapshot_list(layer)] == ["20261119T101500Z-10", "20261119T101500Z-2", "20261119T101500Z"]
    assert d.snapshot_list(tmp_path / "missing") == []
    assert os.path.isdir(layer / ".work")
