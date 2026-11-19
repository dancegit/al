"""`al snapshot` (take, list, restore a backup of a layer folder), `al gen --list-edits` and
`al author --set-aside / --diff-set-aside`, and that the dashboard reads the same snapshot records as al.
Real git, the real compiler; invented names only.

    uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools/test_snapshot.py -q
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tarfile
from pathlib import Path

import pytest

from conftest import KIT, ROOT, al, git, status

sys.path.insert(0, str(ROOT / "tools"))
import al_snapshot  # noqa: E402


def load_dashboard():
    spec = importlib.util.spec_from_file_location("al_dashboard_for_snapshots", KIT / "dashboard" / "dashboard.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def take(layer: Path, note: str = "by hand") -> dict:
    return json.loads(al("snapshot", layer, "--note", note, "--json").stdout)


def listed(layer: Path) -> list[str]:
    return [r["id"] for r in json.loads(al("snapshot", layer, "--list", "--json").stdout)]


def tree_hashes(layer: Path) -> dict[str, str]:
    return {p.relative_to(layer).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(layer.rglob("*")) if p.is_file() and ".git" not in p.relative_to(layer).parts}


def run_state(layer: Path, run: str = "abc12345") -> Path:
    path = layer / "agents" / run / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run": run}) + "\n")
    return path


# ------------------------------------------------------------------------------------------ take

def test_snapshot_of_a_git_layer_is_a_commit(sibling):
    run_state(sibling)
    record = json.loads(al("snapshot", sibling, "--note", "before cleanup", "--json").stdout)
    assert record["kind"] == "git" and al_snapshot.ID_RX.match(record["id"])
    assert git(sibling, "log", "-1", "--format=%an <%ae>|%s") == "al <al@localhost>|al snapshot: before cleanup"
    assert git(sibling, "rev-parse", "HEAD") == record["commit"]
    assert "agentic.rkt" in git(sibling, "ls-tree", "--name-only", "HEAD").splitlines()
    with tarfile.open(sibling / record["agents_archive"]) as tar:
        assert "agents/abc12345/state.json" in tar.getnames()
    assert ".work" not in status(sibling)
    assert (sibling / ".work" / "snapshots" / f"{record['id']}.json").is_file()
    al("doctor", sibling, "--for", "snapshot")
    out = al("snapshot", sibling).stdout
    assert out.startswith("snapshot ") and "(commit " in out and str(sibling.name) in out


def test_snapshot_of_a_plain_layer_is_an_archive(in_repo):
    app = in_repo.parent
    commits = git(app, "rev-list", "--count", "HEAD")
    record = take(in_repo)
    assert record["kind"] == "archive" and "commit" not in record
    assert (in_repo / ".work" / "snapshots" / f"{record['id']}.tar.gz").is_file()
    assert record["archive"] == f".work/snapshots/{record['id']}.tar.gz"
    assert git(app, "rev-list", "--count", "HEAD") == commits
    assert status(app) == ""


def test_list_shows_snapshots_newest_first(sibling, in_repo):
    first, second = take(sibling, "one"), take(sibling, "two")
    assert first["id"] != second["id"]
    if first["id"][:16] == second["id"][:16]:          # within one second
        assert second["id"] == first["id"][:16] + "-2"
    out = al("snapshot", sibling, "--list").stdout
    rows = [line.split() for line in out.splitlines()[1:]]
    assert [(r[0], r[-1]) for r in rows] == [(second["id"], "two"), (first["id"], "one")], out
    assert listed(sibling) == [second["id"], first["id"]]
    assert "no snapshots of " in al("snapshot", in_repo, "--list").stdout
    assert listed(in_repo) == []


# ------------------------------------------------------------------------------------------ restore

@pytest.mark.parametrize("fixture", ["sibling", "in_repo"])
def test_restore_takes_a_snapshot_first_and_brings_back_an_edited_file(fixture, request):
    layer = request.getfixturevalue(fixture)
    original = (layer / "agentic.rkt").read_text()
    s = take(layer)["id"]
    (layer / "agentic.rkt").write_text(original + ";; an edit\n")
    (layer / "notes.txt").write_text("scratch\n")
    out = al("snapshot", layer, "--restore", s).stdout
    assert (layer / "agentic.rkt").read_text() == original and not (layer / "notes.txt").exists()
    assert "undoes this restore" in out and f"restored {s}:" in out
    records = json.loads(al("snapshot", layer, "--list", "--json").stdout)
    before = [r for r in records if r["note"] == f"before restore of {s}"]
    assert len(before) == 1
    al("snapshot", layer, "--restore", before[0]["id"])
    assert (layer / "agentic.rkt").read_text() == original + ";; an edit\n"
    assert (layer / "notes.txt").read_text() == "scratch\n"


def test_restore_dry_run_changes_nothing(sibling):
    s = take(sibling)["id"]
    (sibling / "agentic.rkt").write_text((sibling / "agentic.rkt").read_text() + ";; an edit\n")
    before, ids = tree_hashes(sibling), listed(sibling)
    out = al("snapshot", sibling, "--restore", s, "--dry-run").stdout
    assert "restore agentic.rkt" in out and "would take a snapshot" in out
    assert tree_hashes(sibling) == before and listed(sibling) == ids
    assert not (sibling / ".work" / f"restore-{s}").exists()


def test_agents_survive_and_work_scratch_is_not_archived(in_repo):
    run_state(in_repo)
    (in_repo / "agents" / "abc12345" / "objects" / "pack").mkdir(parents=True)
    (in_repo / "agents" / "abc12345" / "objects" / "pack" / "x").write_text("object\n")
    (in_repo / ".work").mkdir(exist_ok=True)
    (in_repo / ".work" / "scratch.txt").write_text("scratch\n")
    (in_repo / "trees" / "abc12345").mkdir(parents=True)
    (in_repo / "trees" / "abc12345" / "f").write_text("checkout\n")
    record = take(in_repo)
    with tarfile.open(in_repo / record["archive"]) as tar:
        names = tar.getnames()
    assert "agents/abc12345/state.json" in names
    assert not [n for n in names if n.startswith(("agents/abc12345/objects", ".work", "trees"))], names
    for path in sorted((in_repo / "agents" / "abc12345").rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    (in_repo / "agents" / "abc12345").rmdir()
    other = run_state(in_repo, "def67890")
    al("snapshot", in_repo, "--restore", record["id"])
    assert json.loads((in_repo / "agents" / "abc12345" / "state.json").read_text()) == {"run": "abc12345"}
    assert json.loads(other.read_text()) == {"run": "def67890"}
    assert (in_repo / "trees" / "abc12345" / "f").is_file()


def test_restore_refuses_an_unknown_id(sibling):
    take(sibling)
    before, ids = tree_hashes(sibling), listed(sibling)
    cp = al("snapshot", sibling, "--restore", "20000101T000000Z", ok=False)
    assert cp.returncode != 0 and "al: snapshot: no snapshot 20000101T000000Z" in cp.stderr
    assert tree_hashes(sibling) == before and listed(sibling) == ids
    cp = al("snapshot", sibling, "--list", "--restore", "X", ok=False)
    assert cp.returncode != 0 and "exclude each other" in cp.stderr


def test_restore_refuses_an_archive_member_outside_the_folder(in_repo, tmp_path):
    record = take(in_repo)
    evil = tmp_path / "evil.txt"
    evil.write_text("outside\n")
    with tarfile.open(in_repo / record["archive"], "w:gz") as tar:
        tar.add(evil, arcname="../evil.txt")
    cp = al("snapshot", in_repo, "--restore", record["id"], ok=False)
    assert cp.returncode != 0 and "unsafe archive member" in cp.stderr


# ------------------------------------------------------------------------------------------ gen --list-edits

def test_gen_list_edits_names_hand_edited_files(sibling):
    plan = sibling / "commands" / "plan.md"
    plan.write_text(plan.read_text() + "\nA hand edit.\n")
    manifest = (sibling / ".al-manifest.json").read_bytes()
    edited = plan.read_bytes()
    out = al("gen", sibling, "--list-edits").stdout
    assert "commands/plan.md" in out and "would be overwritten by al gen" in out
    assert (sibling / ".al-manifest.json").read_bytes() == manifest and plan.read_bytes() == edited
    al("gen", sibling, "--discard-edits")
    assert "no hand-edited generated files" in al("gen", sibling, "--list-edits").stdout
    cp = al("gen", sibling, "--list-edits", "--discard-edits", ok=False)
    assert cp.returncode != 0 and "exclude each other" in cp.stderr


# ------------------------------------------------------------------------------------------ author --set-aside

def test_author_set_aside_keeps_run_history_and_adopted(sibling):
    state = run_state(sibling)
    rules = sibling / "adopted" / "rules" / "CLAUDE.md"
    rules.parent.mkdir(parents=True)
    rules.write_text("# rules\n")
    original = (sibling / "agentic.rkt").read_text()
    out = al("author", sibling, "--set-aside").stdout
    aside = list(sibling.glob("agentic.rkt.before-*"))
    assert len(aside) == 1 and aside[0].read_text() == original and not (sibling / "agentic.rkt").exists()
    assert f"set aside: agentic.rkt -> {aside[0].name}" in out
    assert state.is_file() and rules.read_text() == "# rules\n"
    cp = al("author", sibling, "--set-aside", ok=False)
    assert cp.returncode != 0 and "no agentic.rkt to set aside" in cp.stderr
    (sibling / "agentic.rkt").write_text(original.replace("A tiny app", "A small app"))
    diff = al("author", sibling, "--diff-set-aside").stdout
    assert f"--- {aside[0].name}" in diff and "+++ agentic.rkt" in diff
    assert "-  (codebase" in diff and "A small app" in diff
    (sibling / "agentic.rkt").write_text(original)
    assert "the definition is unchanged" in al("author", sibling, "--diff-set-aside").stdout
    cp = al("author", sibling, "--set-aside", "--raw", ok=False)
    assert cp.returncode != 0 and "--set-aside goes alone" in cp.stderr


# ------------------------------------------------------------------------------------------ dashboard parity

def test_dashboard_reads_the_same_snapshot_records(sibling):
    dashboard = load_dashboard()
    assert dashboard.SNAPSHOT_ID.pattern == al_snapshot.ID_RX.pattern
    assert dashboard.SNAPSHOT_DIR == al_snapshot.SNAPSHOT_DIR
    take(sibling, "one")
    take(sibling, "two")
    ids = listed(sibling)
    assert len(ids) == 2
    assert [r["id"] for r in dashboard.snapshot_list(sibling)] == ids
