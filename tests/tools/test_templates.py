"""Part A: customisation that survives regeneration — layer-local templates, `al eject`, and no
silent loss of hand edits to generated files."""
from __future__ import annotations

import json

from conftest import KIT, al, manifest


def test_layer_local_template_wins_for_every_user_and_keeps_the_placeholder_contract(sibling) -> None:
    before = (sibling / "commands" / "plan.md").read_text()
    out = al("eject", sibling, "command", "plan").stdout
    copy = sibling / "templates" / "commands" / "plan-chore.md"
    assert copy.read_text() == (KIT / "templates/commands/plan-chore.md").read_text()      # unrendered
    assert "used by plan, tidy" in out and "al gen" in out and "--list" in out
    copy.write_text(copy.read_text().replace("## Relevant Files", "- LAYER-LOCAL RULE: every plan ends with Rollback.\n\n## Relevant Files"))
    al("gen", sibling)
    for name in ("plan", "tidy"):                                  # both commands use plan-chore
        text = (sibling / "commands" / f"{name}.md").read_text()
        assert "LAYER-LOCAL RULE" in text and "{{" not in text
    assert (sibling / "commands" / "plan.md").read_text() != before
    assert "LAYER-LOCAL RULE" not in (sibling / "commands" / "commit.md").read_text()
    copy.write_text(copy.read_text() + "\n{{no_such_placeholder}}\n")
    cp = al("gen", sibling, ok=False)
    assert cp.returncode == 1 and "templates/commands/plan-chore.md" in cp.stderr and "{{no_such_placeholder}}" in cp.stderr


def test_hop_and_expert_templates_resolve_from_the_layer_first(sibling) -> None:
    al("eject", sibling, "hop", "implement")
    hop = sibling / "templates" / "hops" / "run-plan.md"
    hop.write_text(hop.read_text().replace("# Run Plan", "# Run Plan (our way)"))
    al("eject", sibling, "expert", "storage")
    question = sibling / "templates" / "experts" / "question.md"
    question.write_text(question.read_text() + "\nLAYER EXPERT NOTE\n")
    al("gen", sibling)
    assert "# Run Plan (our way)" in (sibling / "hops" / "implement.md").read_text()
    assert "LAYER EXPERT NOTE" in (sibling / "experts" / "storage" / "question.md").read_text()
    notes = al("check", sibling).stdout
    assert "note: template overridden by the layer: templates/hops/run-plan.md (used by implement" in notes
    assert "templates/experts/question.md (used by storage" in notes


def test_eject_refuses_to_overwrite_an_override_unless_forced(sibling) -> None:
    al("eject", sibling, "command", "plan")
    copy = sibling / "templates" / "commands" / "plan-chore.md"
    copy.write_text("mine\n")
    cp = al("eject", sibling, "command", "tidy", ok=False)          # same template, already ejected
    assert cp.returncode == 1 and "already ejected" in cp.stderr and "--force" in cp.stderr
    assert copy.read_text() == "mine\n"
    al("eject", sibling, "command", "tidy", "--force")
    assert copy.read_text() == (KIT / "templates/commands/plan-chore.md").read_text()
    assert al("eject", sibling, "command", "nope", ok=False).stderr.count("not a declared command") == 1


def test_eject_list_shows_where_every_template_resolves_and_drift(sibling) -> None:
    listing = al("eject", sibling, "--list").stdout
    assert "commands/plan-chore.md" in listing and "kit" in listing and "hops/meta-adw.md" in listing
    al("eject", sibling, "command", "plan")
    al("eject", sibling, "command", "commit")
    copy = sibling / "templates" / "commands" / "plan-chore.md"
    copy.write_text(copy.read_text() + "\nedited\n")
    record = sibling / "templates" / ".al-eject.json"
    data = json.loads(record.read_text())
    data["commands/commit.md"] = "0" * 64                          # as if the kit changed after the eject
    record.write_text(json.dumps(data))
    rows = {line.split()[0]: line for line in al("eject", sibling, "--list").stdout.splitlines()[1:] if line.strip()
            and not line.strip().startswith("compare:")}
    assert "layer copy: edited in the layer" in rows["commands/plan-chore.md"]
    assert "[layer copy, identical to the kit]" in rows["commands/commit.md"]
    (sibling / "templates/commands/commit.md").write_text("ours\n")
    commit_row = next(l for l in al("eject", sibling, "--list").stdout.splitlines() if l.startswith("commands/commit.md"))
    assert "the KIT'S VERSION HAS CHANGED since you ejected it" in commit_row and "edited in the layer" in commit_row
    assert "compare: diff -u" in al("eject", sibling, "--list").stdout


def test_hand_edit_of_a_generated_file_stops_al_gen_before_writing_anything(sibling) -> None:
    generated = sibling / "commands" / "plan.md"
    generated.write_text(generated.read_text() + "\nmy precious hand edit\n")
    source = sibling / "agentic.rkt"
    source.write_text(source.read_text().replace('#:description "Plan, build, test; a person reviews"',
                                                 '#:description "Changed description"'))
    ir_before = (sibling / "agentic-layer.json").read_text()
    readme_before = (sibling / "README.md").read_text()
    cp = al("gen", sibling, ok=False)
    assert cp.returncode == 3
    assert "commands/plan.md" in cp.stderr and "edited by hand" in cp.stderr
    assert f"al eject {sibling} command plan" in cp.stderr and "templates/commands/plan-chore.md" in cp.stderr
    assert f"al gen {sibling} --discard-edits" in cp.stderr
    assert "my precious hand edit" in generated.read_text()
    assert (sibling / "agentic-layer.json").read_text() == ir_before and (sibling / "README.md").read_text() == readme_before
    assert any("commands/plan.md was edited by hand" in line for line in al("check", sibling, ok=False).stdout.splitlines())
    out = al("gen", sibling, "--discard-edits").stdout
    assert "my precious hand edit" not in generated.read_text()
    assert "Changed description" in (sibling / "agentic-layer.json").read_text()
    assert "changed" in out


def test_files_without_a_template_point_at_the_definition(sibling) -> None:
    script = sibling / "adws" / "adw_change.py"
    script.write_text(script.read_text() + "# edited\n")
    cp = al("gen", sibling, ok=False)
    assert "adws/adw_change.py" in cp.stderr and "change agentic.rkt instead" in cp.stderr


def test_a_manifest_written_by_al_0_1_still_loads_and_starts_recording(sibling) -> None:
    files = sorted(manifest(sibling)["files"])
    (sibling / ".al-manifest.json").write_text(json.dumps({"generator": "al 0.1", "files": files}, indent=1) + "\n")
    (sibling / ".al-manifest.local.json").unlink()
    generated = sibling / "commands" / "plan.md"
    generated.write_text(generated.read_text() + "\nedited under an old manifest\n")
    al("gen", sibling)                                             # hash unknown: not blocked …
    assert "edited under an old manifest" not in generated.read_text()
    assert isinstance(manifest(sibling)["files"], dict)             # … and from now on recorded
    assert len(manifest(sibling)["files"]["commands/plan.md"]) == 64
    generated.write_text(generated.read_text() + "\nedited again\n")
    assert al("gen", sibling, ok=False).returncode == 3


def test_generation_is_deterministic_and_a_second_gen_is_a_no_op(sibling) -> None:
    first = {p.relative_to(sibling).as_posix(): p.read_bytes() for p in sorted(sibling.rglob("*"))
             if p.is_file() and ".git/" not in p.as_posix()}
    out = al("gen", sibling).stdout
    assert out.strip().endswith("(0 changed)")
    second = {p.relative_to(sibling).as_posix(): p.read_bytes() for p in sorted(sibling.rglob("*"))
              if p.is_file() and ".git/" not in p.as_posix()}
    assert first == second
    data = manifest(sibling)
    assert data["generator"] == "al 0.2" and "bin/al" not in data["files"]            # machine-specific: local manifest
    assert "bin/al" in json.loads((sibling / ".al-manifest.local.json").read_text())["files"]


FORMAT_TEMPLATES = {"commit": "## Message Format", "pull-request": "## Body Format", "document": "## Document Format",
                    "plan-chore": "## Plan Format", "plan-bug": "## Plan Format", "plan-feature": "## Plan Format",
                    "plan-patch": "## Plan Format"}


def test_templates_that_prescribe_a_format_say_codebase_instructions_take_precedence() -> None:
    for name, heading in FORMAT_TEMPLATES.items():
        text = (KIT / "templates" / "commands" / f"{name}.md").read_text()
        precedence = text.index(heading) + len(heading)
        assert text[precedence:].lstrip().startswith("Codebase-specific instructions below take precedence over the "
                                                     "defaults in this section."), name
        section = text.index("## Codebase-specific instructions")
        assert precedence < section < text.index("{{focus}}") < text.index("## Report"), name
        assert text.count("{{focus}}") == 1 and "fixed" in text[section:text.index("{{focus}}")], name


def test_a_focus_line_is_rendered_where_it_takes_precedence_and_the_output_contract_stays_last(sibling) -> None:
    rkt = sibling / "agentic.rkt"
    rkt.write_text(rkt.read_text().replace("(command commit #:template commit)",
                                           '(command commit #:template commit #:focus ("One summary line, no trailer lines."))'))
    al("gen", sibling)
    text = (sibling / "commands" / "commit.md").read_text()
    assert (text.index("## Message Format") < text.index("Codebase-specific instructions below take precedence")
            < text.index("## Codebase-specific instructions") < text.index("- One summary line, no trailer lines.")
            < text.index("## Report") < text.index("Finish with a concise report"))
    plan = (sibling / "commands" / "plan.md").read_text()                   # no focus: the section stays, empty
    assert plan.index("## Codebase-specific instructions") < plan.index("## Report") < plan.index("ONLY the path")


def test_implement_reports_each_validation_command_with_exit_code_and_log() -> None:
    text = (KIT / "templates" / "commands" / "implement.md").read_text()
    assert "${artefacts_dir}/validation/" in text and "-rs" in text
    report = text[text.index("## Report"):]
    assert "exit code" in report and "log" in report


SIX = ["## What is asked and why", "## What exists today", "## The change, step by step", "## How it is proven",
       "## What could go wrong", "## What is left out"]
PLAN_LAYOUT = {
    "plan-chore": SIX + ["### Commands", "### Checkable results"],
    "plan-bug": SIX + ["### How to reproduce", "### Cause", "### Add regression test", "### Tests", "### Commands",
                       "### Checkable results"],
    "plan-feature": SIX + ["### New files", "### Tests", "### Commands", "### Checkable results"],
    "plan-patch": ["## What is asked and why", "## The change, step by step", "## How it is proven", "## What is left out"],
    "plan-layer-change": ["## Request", "## Summary", "## What changes for you", "## How", "## The change, step by step",
                          "## How it is proven", "## What is left out"],
}
EARLIER_LAYOUT = ["Step by Step Tasks", "Testing Strategy", "Acceptance Criteria", "Implementation Plan", "User Story",
                  "Problem Statement", "Solution Statement", "Root Cause Analysis", "Steps to Reproduce", "Issue Summary",
                  "Chore Description", "Bug Description", "Feature Description"]


def test_plan_templates_use_our_own_layout() -> None:
    for name, headings in PLAN_LAYOUT.items():
        text = (KIT / "templates" / "commands" / f"{name}.md").read_text()
        start = text.index("```md\n", text.index("## Plan Format"))
        block = text[start:text.index("\n```\n", start)]
        found = [line for line in block.splitlines() if line.startswith("## ")]
        assert found == [h for h in headings if h.startswith("## ")], f"{name}: the plan's sections, in order"
        for heading in headings:
            assert f"\n{heading}\n" in block, f"{name}: {heading} is missing from its Plan Format"
        assert "{{validation_commands}}" in block[block.index("## How it is proven"):], name
        for old in EARLIER_LAYOUT:
            assert old not in text, f"{name} still names {old!r}, a section of the earlier layout"
        outside = text[:start] + text[start + len(block):]
        for section in ("## Variables", "## Instructions", "## Relevant Files", "## Plan Format", "## Validation Commands",
                        "## Report"):
            assert f"\n{section}\n" in outside, f"{name}: the prompt's own section {section} keeps its name"


def test_plan_templates_carry_the_proof_plan_table(sibling) -> None:
    import sys

    sys.path.insert(0, str(KIT / "runtime"))
    from adw_modules import proofplan, templates

    for name in ("plan-feature", "plan-bug", "plan-chore"):
        text = (KIT / "templates" / "commands" / f"{name}.md").read_text()
        parsed = proofplan.parse(text)
        assert parsed["table"] == "parsed" and parsed["rows"][0]["id"] == "C1", name
        assert "### Proof plan" in text and "${approvals}" in text and "${proof_dir}/" in text, name
        assert "a row it cannot prove here stops the run" in text, name
    generated = (sibling / "commands" / "plan.md").read_text()   # plan-chore, rendered by al gen
    assert proofplan.parse(generated)["table"] == "parsed" and "${approvals}" in generated
    rendered = templates.render(generated, {"approvals": "vocabulary", "proof_dir": ".al-proof"}, [])
    assert "Pre-approved for this run: vocabulary." in rendered and "`.al-proof/`" in rendered


def test_plan_prompts_list_the_declared_checks_by_name(sibling) -> None:
    generated = (sibling / "commands" / "plan.md").read_text()   # plan-chore, rendered by al gen
    assert "Declared checks (name, then the command verbatim):\n- `test` — `./test.sh`" in generated
    assert "it never names another command" in generated
    assert "add `-rA` (pytest) so its log has a line per test" in generated
    for name in ("plan-bug", "plan-feature", "plan-chore"):
        text = (KIT / "templates" / "commands" / f"{name}.md").read_text()
        assert "{{declared_checks}}" in text, name
        assert "add `-rA` (pytest) so its log has a line per test" in text, name
