"""The workspace: the git repositories in a folder, each classified read-only and cheaply with al scout's
own detection into layer / setup / none; cached, bounded, never leaving the folder, never running a
repository's hooks or fsmonitor; served on /api/workspace and /api/repo."""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

import dashboard as d
import workspace_fixtures as wf
from conftest import HERE

TOOLS = HERE.parents[2] / "tools"


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    wf.build(root)
    return root


def scan(root: Path, depth: int = 1) -> d.Workspace:
    workspace = d.Workspace([root], depth, d.load_detectors(d.find_al_tool(None)[0]))
    workspace.refresh()
    return workspace


def states(workspace: d.Workspace) -> dict[str, str]:
    return {e.name: e.state for e in workspace.as_list()}


def tracked(repo: Path) -> list[str]:
    return [f for f in wf.git(repo, "ls-files").splitlines() if f]


def test_scan_finds_the_four_repositories_with_their_states(ws):
    workspace = scan(ws)
    assert states(workspace) == {"pantry": "layer", "quill": "setup", "comet": "setup", "fern": "none"}
    comet = workspace.entry("comet")
    assert comet.suite and "suite" in comet.setup
    for entry in workspace.as_list():
        assert entry.branch == "main" and entry.dirty is False and entry.last_activity and not entry.error


def test_dirty_repository_is_marked(ws):
    workspace = scan(ws)
    (ws / "fern" / "notes.txt").write_text("not committed\n")
    workspace.refresh()
    assert workspace.entry("fern").dirty is True


def test_sibling_layer_counts_as_a_layer(ws):
    workspace = scan(ws)
    (ws / "fern.al").mkdir()
    (ws / "fern.al" / "agentic.rkt").write_text("#lang al\n")
    workspace.refresh()
    assert workspace.entry("fern").state == "layer"
    assert workspace.entry("fern").layer_dir == ws / "fern.al"
    assert sorted(e.name for e in workspace.as_list()) == ["comet", "fern", "pantry", "quill"]


def test_setup_summary_reuses_scout_detection(ws):
    sys.path.insert(0, str(TOOLS))
    try:
        import al_adopt
        import al_suite
    finally:
        sys.path.remove(str(TOOLS))
    workspace = scan(ws)
    quill = ws / "quill"
    assert workspace.entry("quill").summary == al_adopt.render_section(al_adopt.detect(quill, tracked(quill)))
    comet = ws / "comet"
    suite = al_suite.detect_suite(comet, tracked(comet))
    assert suite and al_suite.render_suite(suite) in workspace.entry("comet").summary
    assert "### Existing ADW suite" in workspace.entry("comet").summary


def test_depth_is_configurable(ws):
    wf.make_repo(ws / "group", "deep", {"README.md": "# Deep\n"})
    assert "deep" not in states(scan(ws))
    assert states(scan(ws, depth=2))["deep"] == "none"


def test_scan_never_follows_symlinks_out_of_the_root(ws, tmp_path):
    outside = tmp_path / "outside"
    wf.make_repo(outside, "stray", {"CLAUDE.md": "# Rules\n\n- Never.\n"})
    (outside / "secret.md").write_text("# Rules\n\n- a secret rule\n")
    os.symlink(outside / "stray", ws / "stray-link")
    os.symlink(outside / "secret.md", ws / "fern" / "CLAUDE.md")
    wf.commit(ws / "fern", "a symlinked rule file")
    workspace = scan(ws)
    assert "stray" not in states(workspace) and "stray-link" not in states(workspace)
    fern = workspace.entry("fern")
    assert fern.state == "none" and "secret" not in fern.summary and "CLAUDE.md" not in fern.summary


def test_file_count_is_bounded(ws, monkeypatch):
    monkeypatch.setattr(d, "WORKSPACE_FILE_CAP", 3)
    quill = scan(ws).entry("quill")
    assert quill.truncated is True and quill.files == 3 and quill.state == "setup"


def test_git_time_is_bounded(ws, tmp_path, monkeypatch):
    bin_dir = tmp_path / "slow-bin"
    bin_dir.mkdir()
    (bin_dir / "git").write_text("#!/bin/sh\nsleep 30\n")
    (bin_dir / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(d, "WORKSPACE_GIT_TIMEOUT", 0.3)
    assert d.safe_git(ws / "fern", "status", errors=(errors := [])) is None and errors == ["git timed out"]
    started = time.monotonic()
    workspace = scan(ws)
    assert time.monotonic() - started < 10
    assert {e.name: e.error for e in workspace.as_list()} == dict.fromkeys(["comet", "fern", "pantry", "quill"], "git timed out")


def test_scan_runs_git_without_fsmonitor_or_hooks(ws, tmp_path):
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    wf.git(ws / "fern", "config", "core.fsmonitor", str(hook))
    hooks = ws / "fern" / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("post-index-change", "reference-transaction"):
        (hooks / name).write_text(f"#!/bin/sh\ntouch {marker}\n")
        (hooks / name).chmod(0o755)
    (ws / "fern" / "untracked.txt").write_text("x\n")
    entry = scan(ws).entry("fern")
    assert entry.dirty is True
    assert not marker.exists()


def test_cache_reuses_unchanged_entries_and_refresh_picks_up_changes(ws, monkeypatch):
    calls: list[str] = []
    real = d.classify_repo

    def counting(repo, *args, **kwargs):
        calls.append(repo.name)
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(d, "classify_repo", counting)
    workspace = scan(ws)
    workspace.refresh()
    assert sorted(calls) == ["comet", "fern", "pantry", "quill"]
    (ws / "fern" / "CLAUDE.md").write_text("# Rules\n\n- Keep it small.\n")
    wf.commit(ws / "fern", "rules")
    workspace.refresh()
    assert calls.count("fern") == 2 and len(calls) == 5
    assert workspace.entry("fern").state == "setup"


def test_refresh_is_rate_limited(ws, workspace_server):
    running = workspace_server(ws)
    scans = running.server.model.workspace.scans
    assert running.json("/api/workspace?refresh=1")["refreshed"] is True
    assert running.json("/api/workspace?refresh=1")["refreshed"] is False
    assert running.server.model.workspace.scans == scans + 1


def test_default_workspace_is_the_parent_of_the_layer_s_repository(ws, tmp_path, workspace_server):
    running = workspace_server(None, "--layer", str(ws / "pantry" / "agentic"))
    assert running.server.model.workspace.roots == [Path(os.path.realpath(ws))]
    view = running.json("/api/workspace")
    assert view["roots"] == ["ws"] and sorted(r["name"] for r in view["repos"]) == ["comet", "fern", "pantry", "quill"]
    off = workspace_server(None, "--layer", str(ws / "pantry" / "agentic"), "--no-workspace")
    assert off.server.model.workspace is None and off.json("/api/snapshot")["workspace"]["enabled"] is False
    other = tmp_path / "other"
    wf.make_repo(other, "solo", {"README.md": "# Solo\n"})
    only = workspace_server(other, "--layer", str(ws / "pantry" / "agentic"))
    assert [r["name"] for r in only.json("/api/workspace")["repos"]] == ["solo"]


def test_workspace_layer_is_watched_like_any_layer(ws, workspace_server):
    running = workspace_server(ws)
    layers = running.json("/api/snapshot")["layers"]
    assert [l["name"] for l in layers] == ["pantry"]
    repo = running.json("/api/repo?repo=pantry")["repo"]
    assert repo["layer_key"] == layers[0]["key"] and repo["layer"] == "ws/pantry/agentic"


def test_al_tool_lookup_reports_where_it_looked(ws, tmp_path, monkeypatch, workspace_server):
    bin_dir = tmp_path / "only-git"
    bin_dir.mkdir()
    os.symlink(shutil.which("git"), bin_dir / "git")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(d, "KIT_AL", tmp_path / "kit" / "tools" / "al")
    running = workspace_server(ws, al="/nonexistent")
    al = running.json("/api/snapshot")["al"]
    assert al == {"found": False, "looked": ["/nonexistent", "~/kit/tools/al", "PATH"]}
    actions = running.json("/api/repo?repo=fern")["actions"]
    assert actions and all(a["available"] is False and a["needs"] == "al" for a in actions)
    assert "looked for: /nonexistent, ~/kit/tools/al, PATH" in actions[0]["why"]


def test_workspace_api_escapes_and_hides_absolute_paths(ws, tmp_path, workspace_server):
    wf.make_repo(ws, "<script>x", {"README.md": "# x\n"})
    running = workspace_server(ws)
    status, _headers, body = running.get("/api/workspace")
    assert status == 200 and b"<script>" not in body and str(tmp_path).encode() not in body
    repos = {r["name"]: r for r in json.loads(body)["repos"]}
    assert repos["<script>x"]["dir"] == "ws/<script>x" and repos["fern"]["dir"] == "ws/fern"
    assert repos["<script>x"]["key"] == "script-x"
    status, _headers, body = running.get("/api/repo?repo=script-x")
    assert status == 200 and b"<script>" not in body
