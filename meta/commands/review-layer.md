---
description: Audit an agentic.rkt against the LLM.txt it was authored from
argument-hint: [agentic-rkt-path] [llm-txt-path]
---

# Review Layer

Audit a compiled layer declaration against its codebase descriptor: is the layer we wrote the layer this codebase needs, and can every claim in it be traced to `LLM.txt`? Read-only.

## Variables

LAYER_SOURCE: $1
LLM_TXT: $2
DSL_REFERENCE: docs/AL-DSL.llms.txt

## Instructions

- Read-only. Edit nothing; findings go in your report. Repairs are a separate command.
- Read LLM_TXT, then LAYER_SOURCE, then the relevant records in DSL_REFERENCE. Judge the layer against LLM_TXT, not against your own idea of the codebase.
- Compiling is necessary, not sufficient: the compiler checks coherence, you check truth and fitness. Run `tools/al compile $1 >/dev/null` first; a compile failure is a single blocker finding and ends the audit.
- Cite both sides of every finding: the clause in LAYER_SOURCE (line) and the section of LLM_TXT it contradicts or lacks support in.
- Each finding gets a concrete fix: the exact clause to add, change or remove.
- Do not report style. Do not report a default as a problem unless LLM_TXT contradicts it.

## Checks

1. Commands: every `(cmd …)` name and shell string appears in LLM_TXT's Commands table, character for character. An invented or altered command is a blocker. A Commands row with no cmd is a warning.
2. `UNVERIFIED`: nothing marked `UNVERIFIED` in LLM_TXT is declared as a cmd, path or artefact glob. Blocker.
3. Protected paths: every path under LLM_TXT's Protected paths is in `(paths #:protected …)`. A missing one is a blocker.
4. Zero-touch: an auto review that ships by merge or deploy exists only if LLM_TXT's Work classes names zero-touch classes; its `#:allowed-classes` is a subset of them; `#:require-checks` is non-empty and covers lint or typecheck, and test; its adw has those checks before `(review)`, collects at least one artefact beyond `log`, and keeps `#:block-protected-paths #t`. Any miss is a blocker.
5. Artefacts: every artefact glob matches a glob in LLM_TXT's Artefacts; every artefact LLM_TXT offers for user-visible work is declared and collected by the check that produces it. A user-visible codebase (web-app, mobile) with no image or video artefact in its review is a blocker for zero-touch and a warning otherwise.
6. Validation: every planning command has `#:validation`, naming cmds that prove that class of work. Missing is a warning; empty on a zero-touch path is a blocker.
7. Focus lines: each `#:focus` string is specific to this codebase (names a path, a tool, a convention from LLM_TXT) and checkable. A generic line ("write clean code", "follow best practices", "add tests") is a warning; a line that contradicts LLM_TXT's Conventions is a blocker. A Convention with no focus line anywhere is a note.
8. Closed loops: every adw that writes code has at least one `(check …)` after the build step, and checks have a `#:resolve` command when one is declared. Missing is a warning.
9. Forge and inputs: the forge, repo and prompt inputs match LLM_TXT's Forge section; issue inputs or webhook triggers with forge `none` in LLM_TXT are a blocker.
10. Environment: nix packages equal LLM_TXT's Toolchain attributes; a check that needs the network does not run in an environment with `#:network none`. Mismatch is a warning.
11. Conditional docs: each LLM_TXT line has a `(conditional-doc …)`. Missing is a note.
12. Existing agent setup: every rule file in LLM_TXT's `## Existing agent setup` reaches the agents (a `(conditional-doc …)`, usually from `adopted/codebase.rkt`); a missing one is a warning. A step, `#:focus` line or command that contradicts a rule of that setup (or of `adopted/RULES.md`) is a blocker, with both texts quoted. A prompt the team uses (`in use? yes`) that the layer replaces with a kit command for the same job, without saying why, is a warning. A CI workflow that already runs an agent on the same events as one of the layer's triggers is a warning. An existing al layer elsewhere in the repository is a note.

## Severities

- `blocker`: the layer would act on something false, bypass a protection, or ship without proof. Must be fixed before generating.
- `warning`: the layer works but is weaker than LLM_TXT allows. Fix before relying on it unattended.
- `note`: an improvement with no risk attached.

## Report

A list of findings, blockers first, each on this pattern:

`[severity] check <number>: <what is wrong> (agentic.rkt:<line>; LLM.txt <section>). Fix: <exact change>.`

Then one line: `verdict: pass` when there are no blockers, otherwise `verdict: fail`, with the count per severity. If there are no findings at all, say so; do not pad the list.
