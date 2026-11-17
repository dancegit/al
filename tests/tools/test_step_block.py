"""The "Your step" block: every kit command and hop template opens with `{{step_block}}`, the commit
command gets the commit variant and build-type prompts end their report with "State of the
worktree". At run time the block names the run's workflow and this step. `al check` notes a
layer-local template without the block, and ignores a run's scratch directory."""
from __future__ import annotations

import re
import shutil

import pytest

from conftest import KIT, al, sh


def _body(text: str) -> str:
    """The text after the front matter."""
    match = re.match(r"---\n.*?\n---\n", text, re.DOTALL)
    assert match, "no front matter"
    return text[match.end():]


@pytest.mark.parametrize("path", sorted([*(KIT / "templates" / "commands").glob("*.md"),
                                         *(KIT / "templates" / "hops").glob("*.md")]), ids=lambda p: p.name)
def test_every_kit_command_and_hop_template_opens_with_the_step_block(path) -> None:
    assert _body(path.read_text()).splitlines()[0] == "{{step_block}}"


def test_the_commit_command_gets_the_commit_variant_and_build_hops_the_build_variant(sibling) -> None:
    commit = (sibling / "commands" / "commit.md").read_text()
    assert _body(commit).startswith("## Your step")
    assert "You are the step that commits" in commit and "change no file" in commit
    assert "Leave your work as uncommitted changes" not in commit
    implement = (sibling / "hops" / "implement.md").read_text()
    assert _body(implement).startswith("## Your step")
    assert "Leave your work as uncommitted changes" in implement
    report = implement[implement.index("## Report"):]
    assert "### State of the worktree" in report
    assert report.index("### State of the worktree") < report.index("Finish with a concise report")
    assert "{{" not in implement and "{{" not in commit


def test_plan_templates_leave_delivery_to_the_workflow(sibling) -> None:
    plan = (sibling / "commands" / "plan.md").read_text()
    assert "delivery is the workflow's" in plan and "apply onto the worktree, uncommitted" in plan


def test_al_check_notes_an_ejected_template_without_the_step_block(sibling) -> None:
    al("eject", sibling, "hop", "implement")
    ejected = sibling / "templates" / "hops" / "run-plan.md"
    ejected.write_text(ejected.read_text().replace("{{step_block}}\n", "", 1))
    al("gen", sibling)
    cp = al("check", sibling)
    assert cp.returncode == 0
    assert 'note: template hops/run-plan.md (yours) has no "Your step" block' in cp.stdout
    assert "{{step_block}}" in cp.stdout


def test_al_check_ignores_the_run_scratch_directory(in_repo) -> None:
    scratch = in_repo / "agents" / "1234abcd" / "scratch"
    scratch.mkdir(parents=True)
    (scratch / "validate.txt").write_text("notes of a run\n")
    cp = al("check", in_repo, ok=False)
    assert "PROBLEM" not in cp.stdout and cp.stdout.rstrip().endswith("check: ok"), cp.stdout


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_a_generated_build_prompt_starts_with_the_step_block_of_its_workflow(sibling) -> None:
    cp = sh(["uv", "run", "--quiet", sibling / "adws" / "adw_change.py", "Add a note", "--dry-run",
             "--adw-id", "0badc0de"], cwd=sibling, timeout=600,
            env={k: v for k, v in __import__("os").environ.items() if k != "AL_APP_ROOT"})
    prompt = (sibling / "agents" / "0badc0de" / "build" / "prompt.md").read_text()
    body = _body(prompt) if prompt.startswith("---\n") else prompt
    assert body.startswith("## Your step"), (cp.stdout + cp.stderr)[-2000:]
    assert "Workflow `change`" in body and "**build (this step)**" in body and "`commit` commits" in body
    assert "agents/0badc0de/scratch" in body
