"""Codebases that already have an agent setup: `al scout` finds and classifies it, `al adopt` brings
it into a layer (deterministically, idempotently, never touching the application), `al check` and
`al explain` know about it, and the adopted prompt and workflow script really run in a mock run."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import existing_setup
import suite_setup
from conftest import ROOT, al, sh, status

BASE = """#lang al
(agentic-layer #:name "larder" #:description "Agentic layer of Larder"
  (codebase #:root "../larder" #:languages (python)
    (cmd test "uv run pytest -q" #:description "the tests pass"))
  (command plan #:template plan-chore #:model-set heavy #:output path #:validation (test))
  (command commit #:template commit)
  (hop implement #:template run-plan #:takes plan)
  ;; ADWS
  (adw change
    (steps (run plan #:command plan #:args ("$prompt")) (run build #:command implement #:args ("$plan"))
           (check test #:cmd test) (run commit #:command commit) (review) (ship))))
"""


@pytest.fixture
def larder(tmp_path: Path) -> Path:
    return existing_setup.build(tmp_path)


@pytest.fixture
def layer(larder: Path) -> Path:
    layer = larder.with_name("larder.al")
    al("init", larder)
    (layer / "agentic.rkt").write_text(BASE)
    return layer


def tree(directory: Path) -> dict[str, str]:
    return {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob("*")) if p.is_file() and ".git" not in p.parts}


def test_scout_finds_and_classifies_the_existing_setup(larder):
    facts = json.loads(al("scout", larder, "--json").stdout)
    kinds = {i["path"]: i["kind"] for i in facts["existing_agent_setup"]}
    assert kinds == {
        "CLAUDE.md": "rules", "AGENTS.md": "rules", "larder/api/AGENTS.md": "rules", ".cursorrules": "rules",
        ".github/copilot-instructions.md": "rules",
        ".claude/commands/review-pr.md": "prompt", ".claude/commands/commit.md": "prompt",
        ".claude/commands/frontend/component.md": "prompt", ".claude/agents/test-writer.md": "agent-definition",
        ".claude/skills/release/SKILL.md": "skill",
        "adws/adw_plan_build.py": "workflow-script", "adws/adw_test.py": "workflow-script",
        ".github/workflows/claude.yml": "ci-agent", ".claude/settings.json": "agent-settings", ".mcp.json": "mcp",
        "specs/": "plans", "ai_docs/": "doc-cache"}
    by_path = {i["path"]: i for i in facts["existing_agent_setup"]}
    assert by_path["larder/api/AGENTS.md"]["scope"] == "larder/api"
    assert by_path[".claude/commands/review-pr.md"]["arguments"] == ["$ARGUMENTS", "hint: [pr-number]"]
    assert by_path[".claude/commands/review-pr.md"]["description"] == "Review a pull request against our rules"
    assert by_path[".claude/commands/frontend/component.md"]["arguments"] == ["$1", "$2", "hint: <name> <props>"]
    assert by_path["adws/adw_plan_build.py"]["runner"] == "uv"
    assert by_path["adws/adw_plan_build.py"]["description"].startswith("Plan and build a change")
    assert by_path[".github/workflows/claude.yml"]["events"] == ["issue_comment", "issues"]
    assert "hooks on PostToolUse" in by_path[".claude/settings.json"]["description"]
    assert by_path["CLAUDE.md"]["size"] == len(existing_setup.FILES["CLAUDE.md"])
    assert ".github/workflows/ci.yml" not in kinds and "adws/adw_modules/agent.py" not in kinds
    draft = al("scout", larder).stdout
    section = draft[draft.index("## Existing agent setup"):draft.index("## Evidence")]
    assert "| `.claude/commands/review-pr.md` | prompt |" in section and "UNVERIFIED" in section
    assert status(larder) == ""


def test_dry_run_changes_nothing(larder, layer):
    before_layer, before_app = tree(layer), tree(larder)
    out = al("adopt", layer, "--dry-run").stdout
    assert "dry run: nothing is written" in out and "would write: adopted/REPORT.md" in out
    assert tree(layer) == before_layer and tree(larder) == before_app and status(larder) == ""


def test_adopt_produces_a_layer_that_compiles_generates_and_checks(larder, layer):
    out = al("adopt", layer).stdout
    assert "renamed `commit` → `team-commit`: `commit` is a kit template" in out
    assert "({{name}}, {{props}}) kept literally" in out
    assert status(larder) == ""
    definition = (layer / "agentic.rkt").read_text()
    assert definition.count('(include "adopted/codebase.rkt")') == 1 and definition.count('(include "adopted/layer.rkt")') == 1
    ir = json.loads(al("compile", layer).stdout)
    docs = {d["path"]: d["when"] for d in ir["codebase"]["conditionalDocs"]}
    assert docs["CLAUDE.md"].startswith("Always: the team's own rules for Claude Code")
    assert docs["larder/api/AGENTS.md"].startswith("When working under larder/api/")
    assert {c["name"]: c["file"] for c in ir["commands"] if c["template"] == "custom"} == {
        "team-commit": "templates/custom/team-commit.md", "review-pr": "templates/custom/review-pr.md",
        "frontend-component": "templates/custom/frontend-component.md"}
    assert {c["name"]: c["run"] for c in ir["codebase"]["commands"] if c["name"].startswith("legacy-")} == {
        "legacy-adw-plan-build": "uv run adws/adw_plan_build.py", "legacy-adw-test": "uv run adws/adw_test.py"}
    legacy = next(a for a in ir["adws"] if a["name"] == "legacy-adw-plan-build")
    assert [s["kind"] for s in legacy["steps"]] == ["script", "review", "ship"] and legacy["review"] == "human"
    al("gen", layer)
    check = al("check", layer)
    assert "check: ok" in check.stdout
    rendered = (layer / "commands" / "frontend-component.md").read_text()
    assert "# Component {{name}}" in rendered and "the props $2 ({{props}})" in rendered
    assert "adopted by `al adopt` from .claude/commands/frontend/component.md" in rendered
    report = (layer / "adopted" / "REPORT.md").read_text()
    assert ("the rule says “Never commit directly; leave committing to the developer who asked for the change.” "
            "(`CLAUDE.md:8`), while the layer says “4. `git commit` with the message.” "
            "(`kit/templates/commands/commit.md:57`)") in report
    assert ".github/workflows/claude.yml already runs an agent (anthropics/claude-code-action)" in report
    assert "PROPOSAL `adws/adw_plan_build.py`" in report and "al improve" in report
    rules = (layer / "adopted" / "RULES.md").read_text()
    assert "- Do not rename existing routes: clients depend on them. (`larder/api/AGENTS.md:4`)" in rules
    assert status(larder) == ""


def test_adopting_twice_changes_nothing_and_a_hand_edit_is_kept(larder, layer):
    al("adopt", layer)
    first = tree(layer)
    out = al("adopt", layer).stdout
    assert "nothing changed: the layer already has all of it" in out
    assert tree(layer) == first
    custom = layer / "templates" / "custom" / "review-pr.md"
    custom.write_text(custom.read_text() + "\nMy own line.\n")
    refused = al("adopt", layer, ok=False)
    assert refused.returncode == 1 and "edited by hand since" in refused.stderr and "templates/custom/review-pr.md" in refused.stderr
    assert "My own line." in custom.read_text()


def test_only_rules(larder, layer):
    al("adopt", layer, "--only", "rules")
    assert (layer / "adopted" / "RULES.md").is_file()
    assert not (layer / "templates" / "custom").exists()
    assert "legacy-" not in (layer / "adopted" / "layer.rkt").read_text()
    assert "conflict" in al("adopt", layer, "--only", "rules", "--dry-run").stdout


def test_adopt_into_a_freshly_initialised_layer(larder):
    layer = larder.with_name("larder.al")
    al("init", larder)
    out = al("adopt", layer, "--app", larder).stdout
    assert "agentic.rkt does not exist yet" in out and '(include "adopted/codebase.rkt")' in out
    assert (layer / "adopted" / "layer.rkt").is_file() and not (layer / "agentic.rkt").exists()
    assert "say where the application is with --app APP" in al("adopt", layer, ok=False).stderr


def _mock_run(layer: Path, adw: str, *args: str) -> tuple[int, dict]:
    cp = sh(["uv", "run", "--quiet", layer / "adws" / f"adw_{adw}.py", *args], cwd=layer, timeout=600)
    run_id = args[args.index("--adw-id") + 1]
    return cp.returncode, json.loads((layer / "agents" / run_id / "state.json").read_text())


def test_an_adopted_prompt_renders_the_original_text_with_its_arguments(larder, layer):
    al("adopt", layer)
    definition = (layer / "agentic.rkt").read_text()
    (layer / "agentic.rkt").write_text(definition.replace(
        ";; ADWS", '(adw ask-review (steps (run review #:command review-pr #:args ("$prompt"))))'))
    al("gen", layer)
    code, state = _mock_run(layer, "ask-review", "42", "--dry-run", "--adw-id", "ad0b0001")
    assert code == 0 and state["status"] == "passed"
    prompt = (layer / "agents" / "ad0b0001" / "review" / "prompt.md").read_text()
    assert "Review pull request 42 of Larder." in prompt
    assert "- Check every changed function in `larder/` has a test in `tests/`." in prompt
    assert "$ARGUMENTS" not in prompt


def test_a_legacy_script_runs_as_a_step_and_its_failure_hands_off(larder, layer):
    al("adopt", layer)
    al("gen", layer)
    code, state = _mock_run(layer, "legacy-adw-plan-build", "Add a low-stock alert", "--adw-id", "ad0b0002")
    assert code == 0, state
    assert state["scripts"]["run"]["exit"] == 0
    assert state["outputs"]["run"].strip() == "planned and built: Add a low-stock alert"
    assert state["ship"]["action"] == "pr"
    code, state = _mock_run(layer, "legacy-adw-plan-build", "please fail", "--adw-id", "ad0b0003")
    assert code == 1 and state["status"] == "failed"
    assert state["scripts"]["run"]["exit"] == 3
    assert state["ship"]["action"] == "handoff" and state["ship"]["stopped_at"] == "run"
    assert "script legacy-adw-plan-build exited 3" in (layer / state["ship"]["target"]).read_text()
    assert status(larder) == ""


def test_explain_describes_adopted_commands_and_script_steps(larder, layer):
    al("adopt", layer)
    out = sh([ROOT / "tools" / "explain.py", layer]).stdout
    assert "review-pr: a prompt the team already had, adopted unchanged from .claude/commands/review-pr.md" in out
    assert ("script run: runs the codebase's own legacy-adw-plan-build command (uv run adws/adw_plan_build.py) with $prompt; "
            "if it fails, the run stops") in out
    assert "More of the definition, from another file" in out


def test_check_notices_a_missing_adopted_rule_file(larder, layer):
    al("adopt", layer)
    al("gen", layer)
    (layer / "adopted" / "RULES.md").unlink()
    out = al("check", layer, ok=False).stdout
    assert "adopted/RULES.md (written by `al adopt`) is missing" in out


def test_adopt_dry_run_diff_before_the_layer_exists(tmp_path):
    orbit = suite_setup.build(tmp_path)
    before = tree(orbit)
    out = al("adopt", tmp_path / "orbit.al", "--app", orbit, "--dry-run", "--diff").stdout
    assert "would write:" in out and "adopted/" in out
    assert "--- /dev/null" in out and "+++ b/adopted/REPORT.md" in out
    assert any(line.startswith("+") and not line.startswith("+++") for line in out.splitlines())
    assert not (tmp_path / "orbit.al").exists()
    assert tree(orbit) == before and status(orbit) == ""


def test_adopt_diff_without_dry_run_is_refused(larder, layer):
    cp = al("adopt", layer, "--diff", ok=False)
    assert cp.returncode in (1, 2)
    assert cp.returncode == 2 or "al: --diff goes with --dry-run" in cp.stderr
    assert not (layer / "adopted").exists()
