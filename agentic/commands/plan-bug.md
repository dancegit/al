---
description: plan-bug for al
argument-hint: [prompt]
---

# Plan Bug

`plan-bug` in layer `al`: turn a bug report into a plan that fixes the root cause with the smallest possible change. Plan only; implement nothing.

## Variables

PROMPT: $1
PLAN_FILE: ${specs_dir}/bug-${adw_id}-<descriptive-name>.md

## Codebase

al generates an agentic layer (prompts, workflows, sandbox, triggers, a dashboard) for any git repository. An LLM writes LLM.txt and agentic.rkt in a small Racket language; a compiler checks the definition and a generator produces the layer from templates. The command is tools/al (Python, run with uv); the runtime and the dashboard are Python with the standard library only.

Languages: python, racket, javascript, shell. Entry document: `README.md`. Default branch: `main`.

## Instructions

- You are in `${worktree}`, a git worktree of the application. Read it and run read-only commands to reproduce; do not modify it. The only file you create is PLAN_FILE, which lives in the layer and never in the application tree. Never switch branches, push, or touch the main checkout.
- Research before writing: read `README.md`, then the Relevant Files, then trace the failing path through the code.
- Reproduce first. Record the exact steps and the observed versus expected result. If you cannot reproduce, say so under Steps to Reproduce and state what evidence the analysis rests on.
- Think hard about the root cause: the earliest point where state or logic goes wrong, cited as `path:line`. A symptom is not a root cause.
- Plan the minimal fix at the root cause. No refactoring, no drive-by cleanups, no new abstractions.
- Tests: plan a regression test that fails before the fix and passes after it; name its path. List any existing test that must change and why.
- Write PLAN_FILE in the Plan Format: keep every heading, replace every `<placeholder>`, add no sections (unless a codebase-specific instruction below says otherwise). `<descriptive-name>` is 3-6 lowercase hyphenated words.
- Keep the commands already present under `## Validation Commands` in the Plan Format unchanged and in order; add the reproduction command and bug-specific commands after them.
- If the bug is user-visible, name in Notes the artefacts that will prove the fix (screenshots, video, reports) and the step that produces them.
- Never plan a change under a protected path. If the fix cannot avoid one, plan everything else and add a line starting `REQUIRES HUMAN` to Notes naming the path and the reason.
- A new dependency is a last resort; add it with `inline script metadata in the script that needs it, or uv run --with for a test command; there is no manifest or lock file` and justify it in Notes.
- Read a conditional document when its condition matches what you are planning.

## Relevant Files

- `README.md`
- `racket/al`
- `tools`
- `kit/runtime`
- `kit/templates`
- `kit/schemas`
- `kit/dashboard`
- `kit/self`
- `meta`
- `racket/al/tests`
- `kit/runtime/tests`
- `kit/dashboard/tests`
- `tests/tools`
- `tests/integration`

Protected paths (do not plan changes here):

- `kit/runtime/adw_modules/review.py`
- `kit/runtime/adw_modules/ship.py`
- `kit/runtime/adw_modules/gitsafe.py`
- `kit/runtime/adw_modules/envs.py`
- `docs/demo`
- `tests/authored`
- `tests/integration/testbeds.toml`

## Conditional Documentation

- `docs/DESIGN.md` — When changing the language, the compiler or how the generator maps a definition to files
- `tests/README.md` — When adding or changing a test under tests/integration, or anything al verify does
- `kit/dashboard/README.md` — When changing anything under kit/dashboard
- `docs/AL-DSL.llms.txt` — When writing or changing any agentic.rkt or a test that contains one
- `meta/README.md` — When changing a prompt under meta/commands
- `docs/GUIDE.md` — When changing the behaviour or output of a tools/al subcommand, because the guide quotes it



## Plan Format

Codebase-specific instructions below take precedence over the defaults in this section.

```md
# Bug: <bug name>

## Bug Description
<symptoms, expected behaviour, actual behaviour>

## Problem Statement
<the precise problem this plan solves>

## Solution Statement
<the fix, and why it addresses the cause rather than the symptom>

## Steps to Reproduce
<numbered steps or one command; observed result; expected result>

## Root Cause Analysis
<the defect, cited as path:line, and the chain from it to the symptom>

## Relevant Files
<bullet per existing file: path, then why it is relevant and what changes in it>

### New Files
<bullet per file to create, with its purpose; "None" if none>

## Step by Step Tasks
IMPORTANT: execute every step in order, top to bottom.

### <step title>
- <one concrete action per bullet: file, symbol, change>

### Add regression test
- <test path, what it asserts, why it fails before the fix>

### Run validation
- Run every command under Validation Commands and fix until all pass.

## Validation Commands
Execute every command from the worktree root; all must exit 0.

- `PLTCOLLECTS="$PWD/racket:" raco test racket/al/tests/compiler-test.rkt` — The language rules: the compiler accepts the examples and rejects each invalid definition with the documented error
- `(cd kit/runtime && uv run --with pytest pytest -q)` — The runtime unit tests pass, including the shipping gate, the sandbox and attempts to escape the container
- `uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools -q` — The al command itself works: eject, in-repo layers, al improve, dashboard generation
- `(cd kit/dashboard && uv run --with pytest pytest -q tests)` — The dashboard's model and HTTP server work: unit and HTTP tests, no browser
- `uv run --with pytest --with pyyaml --with jsonschema pytest tests/integration -q` — The default tier of full runs on the five pinned example repositories passes with the mock agent, and generated prompts equal the golden snapshots
<the reproduction command, now passing, and any bug-specific commands with what each proves>

## Notes
<new dependencies; artefacts that prove a user-visible fix; REQUIRES HUMAN lines; risks and anything the implementer must know>
```

## Codebase-specific instructions

The layer's own instructions for this command (`#:focus` in `agentic.rkt`), if any. Where one says otherwise than a default above, it wins. They never change where the plan is written, the `REQUIRES HUMAN` rule or the Report section: the runtime reads those, so they are fixed.

- The regression test goes in the test directory of the area fixed (tests/tools, kit/runtime/tests, kit/dashboard/tests, racket/al/tests) and must fail before the fix.
- docs/AL-DSL.llms.txt and schemas/agentic-layer-v1.schema.json are generated from racket/al/private/vocab.rkt: never edit them by hand; a fix that needs the vocabulary or compiler changed is stated for a human instead of made.
- Tests must not need the network unless marked `network`, and must not call a real LLM.
- No personal paths and no private repository, employer or customer names in committed files.

## Validation Commands

Do not run these to validate anything now (running one to reproduce the bug is fine). They are the commands the plan embeds.

- `PLTCOLLECTS="$PWD/racket:" raco test racket/al/tests/compiler-test.rkt` — The language rules: the compiler accepts the examples and rejects each invalid definition with the documented error
- `(cd kit/runtime && uv run --with pytest pytest -q)` — The runtime unit tests pass, including the shipping gate, the sandbox and attempts to escape the container
- `uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools -q` — The al command itself works: eject, in-repo layers, al improve, dashboard generation
- `(cd kit/dashboard && uv run --with pytest pytest -q tests)` — The dashboard's model and HTTP server work: unit and HTTP tests, no browser
- `uv run --with pytest --with pyyaml --with jsonschema pytest tests/integration -q` — The default tier of full runs on the five pinned example repositories passes with the mock agent, and generated prompts equal the golden snapshots

## Report

Your final message must be ONLY the path of the file you created, on a single line, with no other text.
