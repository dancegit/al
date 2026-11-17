"""Part B: the layer checked into the codebase (`layer in-repo`)."""
from __future__ import annotations

import json

from conftest import LAYER_TEMPLATE, al, commit, git, sh, status


def test_init_in_repo_scaffolds_the_layer_inside_the_repository(app) -> None:
    out = al("init", app, "--in-repo").stdout
    layer = app / "agentic"
    assert (layer / "LLM.txt").is_file() and (layer / "AL-DSL.llms.txt").is_file()
    assert '(codebase #:root ".." …)' in out and '(layer in-repo #:dir "agentic")' in out
    assert "/trees/" in (layer / ".gitignore").read_text()
    out = al("init", app, "--in-repo", "--dir", "tools/agentic").stdout
    assert (app / "tools" / "agentic" / "LLM.txt").is_file() and '#:root "../.."' in out
    assert al("init", app, "--in-repo", "--dir", "../outside", ok=False).returncode == 1
    assert al("init", app / "tools", "--in-repo", ok=False).stderr.count("not the top directory of a git repository") == 1


def test_generator_allows_a_layer_inside_the_app_only_for_in_repo(app) -> None:
    layer = app / "agentic"
    layer.mkdir()
    (layer / "agentic.rkt").write_text(LAYER_TEMPLATE.format(root="..", placement="(layer sibling)"))
    cp = al("gen", layer, ok=False)
    assert "must not live inside the application tree" in cp.stderr and "(layer in-repo" in cp.stderr
    elsewhere = app / "agents-layer"
    elsewhere.mkdir()
    (elsewhere / "agentic.rkt").write_text(LAYER_TEMPLATE.format(root="..", placement='(layer in-repo #:dir "agentic")'))
    cp = al("gen", elsewhere, ok=False)
    assert "puts the layer at" in cp.stderr and str(app / "agentic") in cp.stderr


def test_what_is_committed_and_what_is_ignored(in_repo, app) -> None:
    tracked = set(git(app, "ls-files", "agentic").splitlines())
    for rel in ("agentic.rkt", "LLM.txt", "agentic-layer.json", ".al-manifest.json", ".gitignore", "README.md",
                "commands/plan.md", "hops/implement.md", "adws/adw_change.py", "adws/adw_modules/runner.py",
                "experts/storage/expertise.yaml", "AL-DSL.llms.txt", "templates/README.md"):
        assert f"agentic/{rel}" in tracked, rel
    for rel in ("bin/al", ".al-manifest.local.json"):
        assert f"agentic/{rel}" not in tracked and (in_repo / rel).is_file()      # machine-specific: generated, ignored
    ignore = (in_repo / ".gitignore").read_text()
    for pattern in ("/trees/", "/agents/", "/artefacts/", "/specs/", "/.al-improve/", "/bin/al", "__pycache__/", "*.lock"):
        assert pattern in ignore.splitlines(), pattern
    readme = (in_repo / "README.md").read_text()
    assert "## What is committed" in readme and "protected-path:" in readme and "al improve" in readme
    assert str(in_repo) not in (in_repo / "README.md").read_text()                # portable: no machine path
    assert str(in_repo) not in (in_repo / "agentic-layer.json").read_text()


def test_al_check_ignores_the_layer_s_own_changes_but_nothing_else(in_repo, app) -> None:
    assert al("check", in_repo).stdout.strip().endswith("check: ok")
    (in_repo / "LLM.txt").write_text("edited, not yet committed\n")              # tracked layer content: fine
    (in_repo / "agents" / "x").mkdir(parents=True)
    (in_repo / "agents" / "x" / "run.log").write_text("ignored run state\n")       # in an ignored dir: fine
    (in_repo / "templates" / "commands").mkdir(parents=True)
    (in_repo / "templates" / "commands" / "plan-chore.md").write_text("an uncommitted override\n")  # source: fine
    assert al("check", in_repo).returncode == 0
    (in_repo / "stray-run-output.json").write_text("{}\n")                         # run state outside the ignored dirs
    (app / "app.py").write_text("NOTES = ['changed']\n")                           # the application itself
    out = al("check", in_repo, ok=False).stdout
    assert "PROBLEM: untracked file in the layer that is neither source nor generated" in out
    assert "agentic/stray-run-output.json" in out
    problems = [line for line in out.splitlines() if line.startswith("PROBLEM:")]
    assert len(problems) == 1 and not any("LLM.txt" in p or "plan-chore.md" in p or "agents/x" in p for p in problems)
    # the developer's own work outside the layer is not the layer's doing: a note, not a failure
    assert "note: uncommitted change outside the layer (not checked):  M app.py" in out


def test_al_check_accepts_a_fresh_in_repo_layer_before_its_first_commit(app) -> None:
    al("init", app, "--in-repo")
    layer = app / "agentic"
    (layer / "agentic.rkt").write_text(LAYER_TEMPLATE.format(root="..", placement='(layer in-repo #:dir "agentic")'))
    al("gen", layer)
    assert (layer / "scout.json").is_file() and "?? agentic/scout.json" in status(app)   # written by al init, not committed
    out = al("check", layer).stdout
    assert out.strip().endswith("check: ok"), out
    assert not [line for line in out.splitlines() if line.startswith("PROBLEM:")], out


def test_al_check_passes_with_unrelated_work_in_progress(in_repo, app) -> None:
    (app / "scratch.txt").write_text("my notes\n")
    (app / "app.py").write_text("NOTES = ['work in progress']\n")
    cp = al("check", in_repo)
    assert cp.stdout.strip().endswith("check: ok"), cp.stdout
    assert "note: uncommitted change outside the layer (not checked): ?? scratch.txt" in cp.stdout
    assert "note: uncommitted change outside the layer (not checked):  M app.py" in cp.stdout


def test_a_mock_run_leaves_the_application_status_clean(in_repo, app) -> None:
    cp = sh(["uv", "run", "--quiet", in_repo / "adws" / "adw_change.py", "Add a docstring", "--dry-run"], cwd=app)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert status(app) == ""
    assert al("check", in_repo).returncode == 0
    states = [json.loads(p.read_text()) for p in (in_repo / "agents").glob("*/state.json")]
    assert len(states) == 1 and states[0]["status"] == "passed"
    assert states[0]["worktree"].startswith(str(in_repo.resolve() / "trees"))


def test_an_ordinary_run_that_changes_the_layer_goes_to_a_person(in_repo, app) -> None:
    script = in_repo / "mock" / "change" / "build.json"
    script.parent.mkdir(parents=True)
    script.write_text(json.dumps({"files": {"app.py": "NOTES = [1]\n", "agentic/commands/plan.md": "rewritten by an agent\n"}}))
    # the build leaves its work uncommitted; the commit step commits it, the layer file included
    (script.parent / "commit.json").write_text(json.dumps({"commit": True}))
    commit(app, "mock script", "agentic/mock")
    cp = sh(["uv", "run", "--quiet", in_repo / "adws" / "adw_change.py", "Tidy", "--dry-run", "--adw-id", "c0ffee01"], cwd=app)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    state = json.loads((in_repo / "agents" / "c0ffee01" / "state.json").read_text())
    assert "protected-path:agentic/commands/plan.md" in state["ship"]["reasons"]
    assert "protected-path:agentic/commands/plan.md" in (in_repo / "agents" / "c0ffee01" / "PR.md").read_text()
    assert (in_repo / "commands" / "plan.md").read_text() != "rewritten by an agent\n"   # the main layer is untouched
    assert status(app) == ""


def test_existing_placements_still_generate_beside_the_app(sibling, app) -> None:
    assert (sibling / ".git").is_dir()                                             # #:git #t
    assert al("check", sibling).stdout.strip().endswith("check: ok")
    assert status(app) == ""
