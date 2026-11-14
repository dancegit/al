---
description: {{description}}
argument-hint: [question]
---

# Question

`{{command_name}}` in layer `{{name}}`: answer one question about the codebase from the code itself, with citations. Read-only.

## Variables

QUESTION: $1

## Codebase

{{codebase_description}}

- Languages: {{languages}}
- Entry document: `{{readme}}`

## Instructions

- Read-only. Do not edit, create, install, build, commit or run anything that changes state. Never switch branches, push, or touch the main checkout.
- Answer from what is in `${worktree}` now: code first, then tests, then documentation. When documentation and code disagree, the code is right; say that they disagree.
- Search before you answer: `git ls-files` and `git grep` to locate, then read the code around each hit. Do not answer from the file name or from general knowledge of the language or framework.
- Cite every factual claim as `path:line` (or `path:start-end`), with paths relative to the worktree root. A claim you cannot cite is marked as an inference.
- Answer the question that was asked. No tour of the codebase, no unrequested advice, no plan.
- If QUESTION asks for a change, do not make it: explain what would have to change and where, and say that a planning command is the way to request it.
- If the answer is not in the repository, say so plainly and name where you looked.
- Read a conditional document when its condition matches the question.
{{focus}}

## Relevant Files

{{relevant_files}}

## Conditional Documentation

{{conditional_docs}}

{{fabric}}

## Workflow

1. Restate QUESTION in one line to fix its scope.
2. Locate the relevant code with `git ls-files` and `git grep`.
3. Read it, and the tests that exercise it.
4. Answer, citing as you go.

## Validation Commands

None to run: this command changes nothing. For reference when the question is about how something is checked or run:

{{all_commands}}

## Report

- The direct answer in one or two sentences.
- Supporting detail as bullets, each with its `path:line` citation.
- Inferences and unknowns, labelled as such.

{{output_contract}}
