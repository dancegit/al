---
description: {{description}}
argument-hint: [domain]
---
{{step_block}}

# Meta-Expert (expert meta-prompt)

`{{command_name}}` in layer `{{name}}`: create a new agent expert for one domain of the codebase: declare it in the layer's source and seed the expertise file it will maintain itself from now on (act, learn, reuse).

## Variables

DOMAIN: $1
LAYER_SOURCE: ${layer_dir}/agentic.rkt
DSL_REFERENCE: ${layer_dir}/AL-DSL.llms.txt
EXPERTISE_FILE: ${layer_dir}/experts/<expert-name>/expertise.yaml
COMPILE: ${layer_dir}/bin/al compile ${layer_dir} >/dev/null

## Codebase

{{codebase_description}}

- Languages: {{languages}}
- Entry document: `{{readme}}`

## Instructions

- You change two files, both in the layer: LAYER_SOURCE (one added line) and EXPERTISE_FILE (new). Do not edit application code or any generated file of the layer (`al gen` overwrites them and refuses to run over a hand edit). Never switch branches, push, or touch the main checkout.
{{layer_change_rule}}
- Your current directory is `${worktree}`. Research DOMAIN there, read-only: find the directories that implement it, their entry points, their tests and how they are exercised.
- An expert is worth having when the domain is a coherent slice of the codebase that work returns to repeatedly. If DOMAIN is the whole codebase, or a single file, or already covered by an expert declared in LAYER_SOURCE, stop and report that instead.
- `<expert-name>` is 1-2 lowercase hyphenated words, unique among declared experts.
- Add exactly one `(expert NAME #:domain "…" #:paths (…))` line after the last existing `(expert …)` clause, or before the first `(adw …)` clause if there is none. `#:domain` is one sentence saying what the expert knows. `#:paths` are the codebase paths, relative to the codebase root, that the expertise is validated against: as narrow as the domain allows. Set `#:max-lines` only if the default in DSL_REFERENCE does not fit.
- Run COMPILE after the edit and fix the line from the alternatives in any error until it exits 0. Never change another clause to get there.
- Seed EXPERTISE_FILE as YAML with these top-level keys, in this order: `overview`, `key_files` (each with `path`, `purpose`, `lines`), `key_operations`, `patterns`, `testing`, `known_issues`. If another expert's file exists under `${layer_dir}/experts/`, mirror its shape exactly, including its header comment.
- The header comment states: mental model, not a source of truth; never hand-edit.
- Every entry is verified against the code as it is now and carries a `path` or `path:line` reference. Record how things work and where, not what the code says line by line. No guesses, no generalities about the language or framework.
- Seed lean: the 5-15 files that matter, the operations someone changing this domain performs, the patterns they must follow, how it is tested, the traps. Stay well under the line cap; the expert grows the file itself.
- Read a conditional document when its condition matches DOMAIN.
{{focus}}

## Relevant Files

{{relevant_files}}

## Conditional Documentation

{{conditional_docs}}

{{fabric}}

## Workflow

1. Read the `expert` record in DSL_REFERENCE and the declared experts in LAYER_SOURCE.
2. Research DOMAIN in the worktree; settle the paths.
3. Add the `(expert …)` line; run COMPILE until it exits 0.
4. Write EXPERTISE_FILE.
5. Re-open three references from the file at random and confirm each points at what it claims.

## Validation Commands

- `${layer_dir}/bin/al compile ${layer_dir} >/dev/null`: exits 0 when the new declaration is valid.
- EXPERTISE_FILE parses as YAML (use any YAML parser available on this machine) and is under the line cap.

## Report

- The `(expert …)` line added, in a code block, and the compile result.
- The path of EXPERTISE_FILE and its line count.
- Two example questions this expert can now answer.
- The one-sentence request a person can give to `al improve` to have this expert added through review.

{{output_contract}}
