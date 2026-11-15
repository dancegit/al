"""Part C: al's own workflow for changing a layer (`al improve`), with a scripted mock agent."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from conftest import KIT, al, commit, git, mock_scripts, sh, status

ADW = "abcd0001"


def plan(change: str, what_changes: str) -> dict:
    """The plan step: a plan file in the workflow's own specs/, as plan-layer-change asks for."""
    text = (f"# Layer change: {change}\n\n## Request\n{change}\n\n## Summary\n{change} (scripted plan).\n\n"
            f"## What changes for you\n{what_changes}\n\n## How\nThe smallest change.\n\n## Step by Step Tasks\n"
            "### Change the layer\n- as scripted\n\n## Validation Commands\n- definition\n\n## Notes\nnone\n")
    return {"layer_files": {"specs/layer-change.md": text}, "output": "specs/layer-change.md"}


def queue_work(source: str) -> str:
    """Request (i): a task-file prompt input and a poller trigger, accepted by the `change` workflow."""
    source = source.replace('(layer in-repo #:dir "agentic")',
                            '(layer in-repo #:dir "agentic")\n  (prompt-input cli)\n  (prompt-input tasklist)\n'
                            '  (trigger manual)\n  (trigger cron #:interval-seconds 5)')
    return source.replace('(adw change #:description "Plan, build, test; a person reviews"',
                          '(adw change #:description "Plan, build, test; a person reviews" #:input (cli tasklist) '
                          '#:trigger (manual cron)')


def improve(layer: Path, request: str, scripts: Path | None, *extra: str, ok: bool = True, adw: str = ADW):
    args = ["improve", layer, request, "--dry-run", "--adw-id", adw, *extra]
    if scripts is not None:
        args += ["--mock-scripts", scripts]
    return al(*args, ok=ok, timeout=1800)


def run_dir(layer: Path, adw: str = ADW) -> Path:
    return layer / ".al-improve" / "agents" / adw


def branch_of(layer: Path, adw: str = ADW) -> str:
    return json.loads((run_dir(layer, adw) / "state.json").read_text())["branch"]


def test_a_dsl_change_ends_in_a_reviewable_pull_request(in_repo, app, tmp_path) -> None:
    main_before = git(app, "rev-parse", "main")
    scripts = mock_scripts(tmp_path, {
        "plan": plan("queue work in a text file", "- Write a task as a line in `agentic/tasks.md`; the poller runs it."),
        "implement": {"files": {"agentic/agentic.rkt": queue_work((in_repo / "agentic.rkt").read_text())}}})
    out = improve(in_repo, "I want to queue work in a text file", scripts).stdout
    assert "the pull request includes the regenerated files: merging it is all that is needed" in out
    pr = (run_dir(in_repo) / "PR.md").read_text()
    assert pr.startswith("# agentic layer: I want to queue work in a text file")
    head = pr.split("\n---\n")[0]
    for part in ("## What changes for you", "Write a task as a line in `agentic/tasks.md`",
                 "Where work comes from: new option `tasklist` (tasklist).", "The workflow `change` changed: input, trigger.",
                 "merging it is all that is needed", "## The layer explained: before → after",
                 "### Where work comes from", "**Before:** Prompt input: the command line. (not written in the file: the default)",
                 "**After:** Prompt input: the command line or a plain-text task file.",
                 "### Workflow: change", "Work from cli or tasklist; started by manual or cron",
                 "## Files", "- `agentic/agentic.rkt` +", "| `agentic/agentic-layer.json` |",
                 "| `agentic/adws/triggers/trigger_cron.py` |", "## Checks",
                 "| every workflow completes a --dry-run | pass | 1 |"):
        assert part in head, part
    assert head.count("| pass |") == 6
    branch = branch_of(in_repo)
    changed = git(app, "diff", "--name-only", f"main...{branch}").splitlines()
    assert "agentic/agentic.rkt" in changed and "agentic/adws/triggers/trigger_cron.py" in changed   # regenerated files included
    assert all(path.startswith("agentic/") for path in changed)
    assert git(app, "rev-parse", "main") == main_before and status(app) == ""   # nothing merged, checkout untouched
    # a person merges; `al gen` is then a no-op and the new prompt input works (mock agent, poller)
    git(app, "merge", "-q", "--ff-only", branch)
    assert al("gen", in_repo).stdout.strip().endswith("(0 changed)") and status(app) == ""
    with (in_repo / "tasks.md").open("a") as tasks:
        tasks.write("\n## notes\n- [ ] Add a docstring to app.py\n")
    cp = sh(["uv", "run", "--quiet", in_repo / "adws" / "triggers" / "trigger_cron.py", "--once", "--dry-run"], cwd=app)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    runs = [json.loads(p.read_text()) for p in (in_repo / "agents").glob("*/state.json")]
    assert [(r["adw"], r["source"].split(":")[0], r["status"]) for r in runs] == [("change", "tasklist", "passed")]
    assert status(app) == ""


def kit_plan_chore_with_rollback() -> str:
    kit = (KIT / "templates/commands/plan-chore.md").read_text()
    return kit.replace("## Notes\n<tests added", "## Rollback\n<exactly how to undo the change: the commands or edits, in order>\n\n## Notes\n<tests added")


def test_an_ejected_and_edited_template_ends_in_a_pull_request(in_repo, app, tmp_path) -> None:
    kit_sha = hashlib.sha256((KIT / "templates/commands/plan-chore.md").read_bytes()).hexdigest()
    scripts = mock_scripts(tmp_path, {
        "plan": plan("plans end with Rollback", "- Every plan an agent writes ends with a Rollback section."),
        "implement": {"raw_files": {"agentic/templates/commands/plan-chore.md": kit_plan_chore_with_rollback(),
                                    "agentic/templates/.al-eject.json": json.dumps({"commands/plan-chore.md": kit_sha})}}})
    improve(in_repo, "Every plan must end with a 'Rollback' section", scripts)
    pr = (run_dir(in_repo) / "PR.md").read_text().split("\n---\n")[0]
    assert "A layer-local template changed: `templates/commands/plan-chore.md`" in pr
    assert "No block's description changed" in pr
    assert "## What the agents will be told differently" in pr and "+## Rollback" in pr
    assert "`commands/plan.md`:" in pr and "`commands/tidy.md`:" in pr                    # both users of plan-chore
    assert "- `agentic/templates/commands/plan-chore.md` +" in pr
    git(app, "merge", "-q", "--ff-only", branch_of(in_repo))
    assert al("gen", in_repo).stdout.strip().endswith("(0 changed)")
    assert "## Rollback" in (in_repo / "commands" / "plan.md").read_text()
    assert "templates/commands/plan-chore.md (used by plan, tidy" in al("check", in_repo).stdout


BROKEN = '(check test #:cmd tests)'


def test_a_change_that_breaks_compilation_is_caught_and_resolved(in_repo, app, tmp_path) -> None:
    good = queue_work((in_repo / "agentic.rkt").read_text())
    scripts = mock_scripts(tmp_path, {
        "plan": plan("queue work", "- tasks"),
        "implement": {"files": {"agentic/agentic.rkt": good.replace("(check test #:cmd test)", BROKEN)}},
        "definition-resolve-1": {"files": {"agentic/agentic.rkt": good}, "output": "fixed the cmd name"}})
    improve(in_repo, "queue work in a text file", scripts)
    state = json.loads((run_dir(in_repo) / "state.json").read_text())
    assert state["status"] == "passed" and state["checks"]["definition"]["exit"] == 0
    first = (run_dir(in_repo) / "checks" / "definition.1.log").read_text()
    assert "`tests` is not a declared codebase cmd; declared: test" in first
    assert "| the definition compiles | pass | 2 |" in (run_dir(in_repo) / "PR.md").read_text()


def test_a_change_that_still_does_not_compile_is_handed_to_a_person(in_repo, app, tmp_path) -> None:
    main_before = git(app, "rev-parse", "main")
    bad = (in_repo / "agentic.rkt").read_text().replace("(check test #:cmd test)", BROKEN)
    scripts = mock_scripts(tmp_path, {"plan": plan("break it", "- nothing good"),
                                      "implement": {"files": {"agentic/agentic.rkt": bad}}})
    cp = improve(in_repo, "make a broken change", scripts, ok=False)
    assert cp.returncode == 1
    state = json.loads((run_dir(in_repo) / "state.json").read_text())
    assert state["status"] == "failed" and state["ship"]["action"] == "handoff" and state["ship"]["stopped_at"] == "definition"
    pr = (run_dir(in_repo) / "PR.md").read_text()
    assert pr.startswith("# [needs human] agentic layer: make a broken change")
    assert "## Why this stopped" in pr and "`tests` is not a declared codebase cmd" in pr
    assert git(app, "rev-parse", "main") == main_before and status(app) == ""


def test_a_direct_edit_of_a_generated_file_is_rejected(in_repo, app, tmp_path) -> None:
    generated = (in_repo / "commands" / "plan.md").read_text() + "\n- Every plan ends with Rollback.\n"
    scripts = mock_scripts(tmp_path, {"plan": plan("edit a prompt", "- plans end with Rollback"),
                                      "implement": {"raw_files": {"agentic/commands/plan.md": generated}}})
    cp = improve(in_repo, "Every plan must end with a Rollback section", scripts, ok=False)
    assert cp.returncode == 1
    state = json.loads((run_dir(in_repo) / "state.json").read_text())
    assert state["status"] == "failed" and state["ship"]["stopped_at"] == "regenerate"
    log = (run_dir(in_repo) / "checks" / "regenerate.1.log").read_text()
    assert "commands/plan.md" in log and "edited by hand" in log and "al eject" in log
    assert (run_dir(in_repo) / "PR.md").read_text().startswith("# [needs human]")
    assert "Rollback" not in (in_repo / "commands" / "plan.md").read_text() and status(app) == ""


def test_there_is_no_zero_touch_mode_for_layer_changes(in_repo, tmp_path) -> None:
    for review in ("auto", "zero-touch", "none"):
        cp = improve(in_repo, "ship it without anyone", None, "--review", review, ok=False)
        assert cp.returncode == 1 and "no zero-touch mode for layer changes" in cp.stderr
    assert not (in_repo / ".al-improve" / "agents").exists()
    # the definition itself cannot ask for it either: the compiler refuses #:changes layer + auto
    self_def = in_repo / ".al-improve-attempt"
    self_def.mkdir()
    (self_def / "agentic.rkt").write_text(
        '#lang al\n(agentic-layer #:name "x" (codebase #:root "." #:languages (racket) (cmd t "true"))\n'
        '  (environment worktree) (environment podman) (command review #:template review #:output json)\n'
        '  (command plan #:template plan-layer-change #:output path) (review auto #:require-checks (t))\n'
        '  (adw improve #:changes layer #:environment (worktree podman)\n'
        '    (steps (run plan #:command plan) (check t #:cmd t) (review) (ship))))\n')
    cp = al("compile", self_def, ok=False)
    assert "#:changes layer needs a review option of kind human or agent" in cp.stderr


def test_agent_review_adds_a_reviewer_before_the_person(in_repo, app, tmp_path) -> None:
    scripts = mock_scripts(tmp_path, {"plan": plan("queue work", "- tasks"),
                                      "implement": {"files": {"agentic/agentic.rkt": queue_work((in_repo / "agentic.rkt").read_text())}}})
    improve(in_repo, "queue work in a text file", scripts, "--review", "agent")
    state = json.loads((run_dir(in_repo) / "state.json").read_text())
    assert state["review"]["verdict"]["mode"] == "agent" and state["ship"]["action"] == "pr"
    assert "review-agent: a human merges" in state["ship"]["reasons"]
    assert "(review agent #:command review-layer #:body-cmd report)" in (in_repo / ".al-improve" / "agentic.rkt").read_text()


def test_improve_refuses_a_layer_with_uncommitted_changes(in_repo, app) -> None:
    (in_repo / "agentic.rkt").write_text((in_repo / "agentic.rkt").read_text() + "\n")
    cp = improve(in_repo, "anything", None, ok=False)
    assert cp.returncode == 1 and "not committed on `main`" in cp.stderr and "agentic/agentic.rkt" in cp.stderr


def test_a_sibling_layer_is_changed_in_its_own_repository(sibling, app, tmp_path) -> None:
    scripts = mock_scripts(tmp_path, {
        "plan": plan("focus line", "- plans mention the notes list"),
        "implement": {"files": {"agentic.rkt": (sibling / "agentic.rkt").read_text().replace(
            "(command plan #:template plan-chore #:model-set heavy #:output path #:validation (test))",
            '(command plan #:template plan-chore #:model-set heavy #:output path #:validation (test)\n'
            '           #:focus ("Name the notes list NOTES in every plan."))')}}})
    out = improve(sibling, "Plans must name the notes list", scripts).stdout
    assert "no commit yet" in out and f"after merging it into the layer's repository, run `al gen {sibling}`" in out
    pr = (run_dir(sibling) / "PR.md").read_text()
    assert "The prompt `plan` changed in the definition." in pr and "+- Name the notes list NOTES in every plan." in pr
    assert "| al check passes | pass | 1 |" in pr
    branch = branch_of(sibling)
    assert git(sibling, "diff", "--name-only", f"main...{branch}").splitlines()[:1]   # the change is in the layer repo
    git(sibling, "merge", "-q", "--ff-only", branch)
    assert al("gen", sibling).stdout.strip().endswith("(0 changed)")
    assert "Name the notes list NOTES" in (sibling / "commands" / "plan.md").read_text()
    assert status(app) == ""                                       # the application was never touched
    assert git(sibling, "status", "--porcelain") == ""
