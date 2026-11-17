---
description: {{description}}
argument-hint: [failed-adw-id-or-failure-description] [run-log-path]
---
{{step_block}}

# Self-Improve the Layer

`{{command_name}}` in layer `{{name}}`: when a run failed or produced poor work, find the part of the agentic layer that caused it and propose the edit that prevents the whole class of failure. Fix the system, not the output.

## Variables

FAILURE: $1
RUN_LOG: $2
LAYER_DIR: ${layer_dir}
LAYER_SOURCE: ${layer_dir}/agentic.rkt
DSL_REFERENCE: ${layer_dir}/AL-DSL.llms.txt
PROPOSAL_FILE: ${specs_dir}/improve-${adw_id}-<descriptive-name>.md

## Instructions

- You improve the layer, not the application. Do not edit application code, and do not repair the failed run's output by hand. Never switch branches, push, or touch the main checkout.
- Propose; do not apply. The only file you write is PROPOSAL_FILE.
- Gather evidence before theorising. If FAILURE is a run id, read `${layer_dir}/agents/<that id>/` (state, step outputs, logs); read RUN_LOG when given. Find the first step whose output was wrong, not the last step that crashed.
- Read the prompt that step actually received: its command file under `${layer_dir}/commands/` (or `hops/`), and the `(adw …)` it ran in, in LAYER_SOURCE.
- Think hard about the cause and name exactly one primary cause from this list:
  - instruction gap: the command never told the agent what it needed to know about this codebase;
  - wrong or missing validation: no check caught the problem, or the check proves the wrong thing;
  - workflow shape: a step is missing, misordered, lacks a resolve loop or retries, or carries too much in one step;
  - context: the step was given too much, too little, or the wrong argument;
  - environment: a tool, port, file or permission was missing;
  - not the layer: the request was ambiguous, or the model erred despite a correct prompt. Then say so and propose nothing.
- Prefer the edit with the widest leverage and the smallest surface, in this order: a `#:focus` line on the command; a `#:validation` entry or a `(check …)` step; a step-level change in the `(adw …)`; a new `(conditional-doc …)`; a new command or expert; a layer-local template (`al eject <layer> command NAME` copies the kit template into `${layer_dir}/templates/`, where it can be edited and survives regeneration).
- Generated files under `${layer_dir}/commands/` and `hops/` are overwritten on regeneration and `al gen` refuses to run over a hand edit: never propose editing them. Propose the edit in LAYER_SOURCE whenever the DSL can express it; DSL_REFERENCE is the whole language.
- End the proposal's Verification with the one-sentence request a person can give to `al improve ${layer_dir} "…"`, al's reviewed path: it makes the change, checks and regenerates the layer, and opens a pull request.
- A focus line is specific and checkable ("New endpoints need a test module mirroring the source path"), never generic ("be careful with tests").
- Give the edit as an exact before/after snippet with its file path, and state how to verify it: the compile command and the input that failed, which must now succeed.
{{focus}}

{{fabric}}

## Proposal Format

```md
# Improve: <name>

## Failure
<what went wrong, with the run id and the evidence: file paths, log lines>

## Cause
<the one primary cause from the list, the step and file it lives in, and why the layer allowed it>

## Proposed Edit
<file path; exact before and after>

## Verification
<`${layer_dir}/bin/al compile ${layer_dir}` exits 0; the failing input to re-run; what must be different>

## Not Changed
<alternatives considered and why they were rejected>
```

## Validation Commands

Do not run the codebase's commands. If you can, confirm the proposed DSL edit is well-formed by checking each form and keyword against DSL_REFERENCE.

## Report

The path of PROPOSAL_FILE, the primary cause in one sentence, and the proposed edit in one sentence. If the cause is not the layer, say that and write no proposal.

{{output_contract}}
