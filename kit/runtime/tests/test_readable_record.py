"""What a person reads about a run (PR.md, the pull request, the handoff, issue comments) names
paths relative to the layer, or to the repository for a layer checked in with it, never as an
absolute path of the machine that ran it; local artefacts get a readable relative reference
instead of a file:// URL; the title is the request's first sentence, capped, with the whole
request in the body; and the document a `document` step wrote travels to the reviewer."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from adw_modules import inputs, ship
from adw_modules.runner import main

ADW_ID = "2b2b0001"
LONG = ("Add unit tests for the pure helper functions of tools/explain.py, which has no test of its own, "
        "covering listing, split_blocks and describe_step. Each test must be fast.\n\n"
        "Acceptance criteria:\n- one\n- two\n")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _ir(review_kind: str = "human") -> dict:
    return {
        "agents": [{"id": "mock", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 20}],
        "commands": [{"name": "plan", "template": "plan-chore", "modelSet": "base", "output": "path"},
                     {"name": "build", "template": "build", "modelSet": "base", "output": "text"},
                     {"name": "document", "template": "document", "modelSet": "base", "output": "text"},
                     {"name": "review", "template": "review", "modelSet": "base", "output": "json"}],
        "codebase": {"root": "", "commands": [{"name": "test", "run": "mkdir -p out && echo ok > out/report.txt",
                                               "timeout": 10}]},
        "artefacts": [{"id": "report", "kind": "file", "glob": "out/*.txt", "status": "stable"},
                      {"id": "log", "kind": "log", "tailLines": 5, "status": "stable"}],
        "reviews": [{"id": "policy", "kind": review_kind, "status": "stable", "command": "review",
                     "reviewers": [] if review_kind == "human" else 1, "maxRetries": 0, "artefacts": [], "allowedClasses": ["chore"], "blockProtectedPaths": True,
                     "maxDiffLines": 100, "requireChecks": [], "ship": "merge", "bodyCmd": ""}],
        "environments": [{"id": "wt", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 27800,
                          "portsPerRun": 0, "keep": "never", "status": "stable"}],
        "forge": {"id": "none", "kind": "none", "status": "stable", "remote": ""},
        "adws": [{"name": "demo", "agent": "mock", "review": "policy", "environment": ["wt"], "classes": ["chore"],
                  "steps": [{"name": "plan", "kind": "run", "command": "plan"},
                            {"name": "build", "kind": "run", "command": "build"},
                            {"name": "test", "kind": "check", "cmd": "test", "collect": ["report", "log"]},
                            {"name": "document", "kind": "run", "command": "document"},
                            {"name": "review", "kind": "review"}, {"name": "ship", "kind": "ship"}]}],
    }


DOC = ("# Helper tests\n\n**Read when:** changing tools/explain.py\n\n"
       "Built `${worktree}/helpers_test.py` from `${layer_dir}/specs/plan.md`.\n")  # an agent copies absolute paths


def _scripts(layer: Path) -> None:
    mock = layer / "mock" / "demo"
    mock.mkdir(parents=True, exist_ok=True)
    (mock / "build.json").write_text(json.dumps({"files": {"helpers_test.py": "def test(): pass\n"}}))
    (mock / "document.json").write_text(json.dumps({
        "layer_files": {f"docs/chore-{ADW_ID}-helper-tests.md": DOC},
        "output": "${layer_dir}/docs/${class}-${adw_id}-helper-tests.md"}))


def _sibling(make_layer, app_repo) -> Path:
    ir = _ir()
    ir["codebase"]["root"] = str(app_repo)
    layer = make_layer(ir)
    _scripts(layer)
    return layer


def _in_repo(make_layer, app_repo) -> Path:
    built = make_layer(_ir())
    layer = app_repo / "agentic"
    shutil.copytree(built, layer)
    shutil.rmtree(built)
    data = json.loads((layer / "agentic-layer.json").read_text())
    data["codebase"]["root"] = ".."
    data["layer"] = {"id": "in-repo", "kind": "in-repo", "dir": "agentic", "status": "stable"}
    (layer / "agentic-layer.json").write_text(json.dumps(data, indent=2))
    (layer / ".gitignore").write_text("/trees/\n/agents/\n/artefacts/\n/specs/\n/docs/\n__pycache__/\n")
    _scripts(layer)
    _git(app_repo, "add", "agentic")
    _git(app_repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "the layer, checked in")
    return layer


def _no_machine_path(text: str, *roots: Path) -> None:
    assert "file://" not in text, text
    for root in roots:
        assert str(root) not in text and str(root.resolve()) not in text, text


# ---------------------------------------------------------------- the title

def test_the_title_is_the_first_sentence_of_the_request_capped_at_a_word_boundary() -> None:
    state = SimpleNamespace(prompt=LONG, klass="chore")
    title = ship._title(state)
    assert title == "chore: Add unit tests for the pure helper functions of…"
    assert len(title) <= ship.TITLE_MAX
    assert ship._title(SimpleNamespace(prompt="Fix the login. Then more.", klass="bug")) == "bug: Fix the login"
    assert ship._title(SimpleNamespace(prompt="  \n\nRename v1.2 to v1.3\nsecond line", klass="chore")) == \
        "chore: Rename v1.2 to v1.3"
    assert ship._title(SimpleNamespace(prompt="x" * 200, klass="chore")) == "chore: " + "x" * 64 + "…"
    assert ship._title(SimpleNamespace(prompt="", klass="chore")) == "chore: adw changes"


# ---------------------------------------------------------------- PR.md

def test_pr_md_of_a_sibling_layer_names_paths_relative_to_the_layer(make_layer, app_repo, tmp_path) -> None:
    layer = _sibling(make_layer, app_repo)
    assert main("demo", layer, [LONG, "--adw-id", ADW_ID]) == 0
    text = (layer / "agents" / ADW_ID / "PR.md").read_text()
    _no_machine_path(text, tmp_path)
    title, body = text.split("\n", 1)
    assert title == "# chore: Add unit tests for the pure helper functions of…"
    assert "Acceptance criteria:\n- one\n- two" in body                          # the whole request is in the body
    assert f"`specs/plan-{ADW_ID}-plan.md`" in body
    assert f"`agents/{ADW_ID}/checks/test.1.log`" in body
    assert f"report (file): `artefacts/{ADW_ID}/report/out/report.txt`" in body
    # the document step's document travels to the reviewer, with where it is kept
    assert f"`docs/chore-{ADW_ID}-helper-tests.md`" in body and "**Read when:** changing tools/explain.py" in body
    assert "Built `helpers_test.py` from `specs/plan.md`." in body


def test_pr_md_of_an_in_repo_layer_names_paths_relative_to_the_repository(make_layer, app_repo, tmp_path) -> None:
    layer = _in_repo(make_layer, app_repo)
    assert main("demo", layer, ["Tidy the helpers", "--adw-id", ADW_ID]) == 0
    text = (layer / "agents" / ADW_ID / "PR.md").read_text()
    _no_machine_path(text, tmp_path)
    assert text.startswith("# chore: Tidy the helpers\n")
    assert f"`agentic/specs/plan-{ADW_ID}-plan.md`" in text
    assert f"`agentic/agents/{ADW_ID}/checks/test.1.log`" in text
    assert f"`agentic/artefacts/{ADW_ID}/report/out/report.txt`" in text
    assert f"`agentic/docs/chore-{ADW_ID}-helper-tests.md`" in text
    assert "Built `helpers_test.py` from `agentic/specs/plan.md`." in text
    assert _git(app_repo, "status", "--porcelain", "--untracked-files=all") == ""


def test_the_pull_request_on_a_forge_has_the_short_title_and_no_machine_path(make_layer, app_repo, tmp_path,
                                                                            monkeypatch) -> None:
    layer = _sibling(make_layer, app_repo)
    created = []
    client = SimpleNamespace(can_attach=False, kind="forgejo",
                             create_pr=lambda branch, base, title, body, **_: created.append((title, body))
                             or {"number": 3, "url": "https://forge.invalid/pulls/3"})
    monkeypatch.setattr(ship, "_forge_client", lambda _layer: client)
    monkeypatch.setattr(ship, "_push_branch", lambda *a, **k: None)
    assert main("demo", layer, [LONG, "--adw-id", ADW_ID]) == 0
    (title, body), = created
    assert title == "chore: Add unit tests for the pure helper functions of…"
    _no_machine_path(body, tmp_path)
    assert "Acceptance criteria:" in body


def test_an_issue_comment_names_the_handoff_target_relative_to_the_repository(make_layer, app_repo) -> None:
    layer = _in_repo(make_layer, app_repo)
    from adw_modules import ir
    loaded = ir.load(layer)
    state = SimpleNamespace(adw_id=ADW_ID, status="failed", branch="b", ship={
        "action": "handoff", "stopped_at": "test", "target": f"agents/{ADW_ID}/PR.md"})
    assert f"see `agentic/agents/{ADW_ID}/PR.md`" in inputs.summarise(state, loaded)
