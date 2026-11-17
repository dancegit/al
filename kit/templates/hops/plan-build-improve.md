---
description: {{description}}
argument-hint: [task]
---
{{step_block}}

# Plan, Build, Improve (orchestrating higher-order prompt)

`{{command_name}}` in layer `{{name}}`: run one task through three single-purpose stages (plan, build, improve the relevant expert) and pass each stage only what it needs. Act, learn, reuse.

## Variables

TASK: $1
WORK_CLASS: ${class}
ADW_ID: ${adw_id}
COMMANDS_DIR: ${layer_dir}/commands
HOPS_DIR: ${layer_dir}/hops
EXPERTS_DIR: ${layer_dir}/experts

## Instructions

- You orchestrate. You do not plan, write code or edit expertise yourself: each stage is a prompt file from the layer, executed by a fresh sub-agent with a clean context. If you cannot spawn sub-agents, run the stages yourself strictly in order and carry forward only the hand-off named below.
- Every stage works in `${worktree}` on branch `${branch}`. Never switch branches, push, or touch the main checkout. Plans and expertise live under `${layer_dir}`; only application code goes in the worktree.
- Stage inputs are exact. Do not pass your own summary, the conversation, or a previous stage's reasoning to the next stage.
- Stage 1, plan. Pick the planning command in COMMANDS_DIR that matches WORK_CLASS (read the front matter descriptions; default to the chore planner when the class is empty). If an expert in EXPERTS_DIR covers the task's area, tell the planner to read that expertise file first. Input: TASK. Output: the path of the plan file, which must exist under `${specs_dir}`.
- Stage 2, build. Use the layer's plan-running prompt in HOPS_DIR (or the implement command in COMMANDS_DIR). Input: the plan path, and nothing else. Output: its report, ending in a `git diff --stat`.
- Stage 3, improve. Choose the expert whose paths contain the most changed files in the diff stat; run its self-improve prompt from EXPERTS_DIR with the git-diff check switched on. Input: the diff summary from stage 2 (the `git diff --stat` output), and nothing else. Output: its report. If no expert covers any changed file, skip the stage and say so; do not create an expert here.
- Gate each stage on the one before. No plan file means stop. A build that reports failing validation or a `REQUIRES HUMAN` line means: do not run stage 3, report the build's result.
- Do not repair a stage's output by hand. If a stage fails, re-run that stage once with the same input; a second failure ends the run.
{{focus}}

## Relevant Files

Protected paths (a stage that needs to change one must stop with `REQUIRES HUMAN`):

{{protected_paths}}

{{fabric}}

## Workflow

1. List COMMANDS_DIR, HOPS_DIR and EXPERTS_DIR; choose the three prompts.
2. Run plan with TASK. Verify the plan file exists.
3. Run build with the plan path. Read its report and validation results.
4. Run improve for the matching expert with the diff summary.
5. Report.

## Validation Commands

The build stage runs these; you only check that its report shows each one passing.

{{validation_commands}}

## Report

- Plan: the plan file path.
- Build: the bullet summary and `git diff --stat` from the build stage, and each validation command's result.
- Improve: the expert chosen, and what it changed in its expertise file (or "no changes needed", or "skipped: no expert covers the diff").
- Any stage that failed or was skipped, with the reason.

{{worktree_report}}

{{output_contract}}
