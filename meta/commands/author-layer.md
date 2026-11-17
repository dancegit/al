---
description: Author a compiling agentic.rkt for a codebase from its LLM.txt
argument-hint: [llm-txt-path] [output-agentic-rkt-path] [piter-preferences]
---

# Author Layer

Write one `#lang al` file that declares the agentic layer for the codebase described by an `LLM.txt`, then compile it and repair it until the compiler accepts it. The compiler is the feedback loop.

## Variables

LLM_TXT: $1
OUTPUT: $2
PREFERENCES: $3 (optional: the choices `al author` derived from the repository's facts, each as a DSL clause with the fact behind it or the default it fell back to, e.g. the forge clause; or PITER choices in plain language; empty means the defaults)
DSL_REFERENCE: AL-DSL.llms.txt in OUTPUT's directory (the layer's own copy of docs/AL-DSL.llms.txt)
COMPILE: tools/al compile $2 >/dev/null

## Instructions

- Read DSL_REFERENCE in full before writing. It is the whole language: any form, kind, field or value not in it does not exist. Its `full.rkt` example is the model for shape and ordering.
- Read LLM_TXT in full. It is the only source of facts about the codebase. Do not read the application repository, and do not add a fact that LLM_TXT does not state.
- The only file you write is OUTPUT: `#lang al`, then exactly one `(agentic-layer …)` form with `#:name`, `#:description` and `#:llm-txt`.
- `(codebase …)` comes strictly from LLM_TXT: languages, default branch, description, package manager, readme; one `(cmd NAME "shell")` per row of its Commands table, with the table's name and the exact shell string, `#:description` from "proves what", and `#:timeout` of about three times the typical duration when that exceeds the default. `(paths …)` from Layout and Protected paths; one `(conditional-doc …)` per Conditional docs line.
- Never declare a cmd, path or artefact glob marked `UNVERIFIED`; list it in your report instead.
- `#:root` is the application checkout relative to OUTPUT's directory. Take it from PREFERENCES; if it is not given, use `../<name>` and say so in the report.
- Options follow PREFERENCES where it speaks and the decision table where it does not: a choice in PREFERENCES wins over the Decision Table, also where the table would choose otherwise (e.g. a forge clause with `#:host` and `#:repo` is written exactly as given). Omit a family entirely when its default is what you want. A forge token is never written into any file: the forge clause's `#:token-env` names the variable that holds it.
- Commands: `prime`, `classify` (json output), one planning command per class (`plan-chore`, `plan-bug`, `plan-feature`, plus `plan-patch`), `resolve-test`, `review` (json output), `document`, `commit`, `pull-request`; `e2e-test` when an e2e cmd exists. Hops: `run-plan` taking a plan, and `meta-command` and `meta-adw`.
- Every planning command is `#:model-set heavy`, `#:output path`, and has `#:validation` listing the declared cmds that prove that class of work (lint, typecheck, test; e2e for bug and feature when it exists).
- `#:focus` lines come from LLM_TXT Conventions and Work classes, placed on the command they govern (test placement on the planners, commit format on `commit`, proof requirements on `review`). Each is specific and checkable. Never write a generic line such as "follow best practices"; no focus line is better than a generic one.
- Declare one `(expert …)` per subsystem that LLM_TXT's Layout shows as a distinct, recurring area; none for a small codebase.
- ADWs: `plan-build` (classify, plan by class with `#:when`, build with the plan) and `sdlc` (compose `plan-build`, one check per validation cmd with `resolve-test` and collected artefacts, document, commit, review, ship). Add `zte` only when LLM_TXT's Work classes names classes safe for zero-touch, says the checks are fast and deterministic, and Artefacts is not `none`; restrict it to those classes.
- Existing agent setup: when LLM_TXT's `## Existing agent setup` lists anything, the layer builds on it and never silently contradicts it. If OUTPUT's directory has an `adopted/` folder (written by `al adopt`), put `(include "adopted/codebase.rkt")` inside `(codebase …)` and `(include "adopted/layer.rkt")` at the top level, and read `adopted/RULES.md` and `adopted/REPORT.md`. Otherwise tell the user in your report to run `tools/al adopt <layer>` and do it then. Carry rules over: each rule that governs one kind of work becomes a `#:focus` line on the command it governs, in the rule's own words. Reuse prompts the team already trusts (the adopted commands, `#:template custom`) in the ADWs instead of a kit command for the same job, unless a rule or the user says otherwise. Where a kit command would do something a rule forbids (the report's "For a person to decide" section lists such pairs), do not declare that step; leave it out and name the rule in your report. Proposed `legacy-…` ADWs stay as proposals: do not make them zero-touch.
- Delivery: when LLM_TXT's `## Delivery` names a style with evidence (not UNVERIFIED), write its proposed `(delivery PRESET …)` clause; otherwise declare no stage (runs then use the default branch, as before). Never let a zero-touch ADW (`review auto` with `#:ship merge` or `deploy`) deliver to a stage where agents may not merge (`#:agents-may-merge #f`, e.g. `main` in the sprint, gitflow, release-train and phase-gate presets): its classes must route to a stage agents may merge into. For an urgent route (hotfixes), add a `(stage hotfix …)` and an ADW with `#:deliver-to hotfix` and a human review.
- Promotion: when LLM_TXT says how the team accepts work (a review meeting, a sign-off, a release approval, a sprint demo), declare a `(promotion FROM -> TO …)` for each stage a person accepts: `#:approvers` as many people as sign off, `#:version (bump …)` and `#:tag "v{version}"` when the team tags releases, `#:after (deploy ENV)` when the next stage is deployed. Never `#:approval agent` or `#:approval auto` into a stage where agents may not merge (`#:agents-may-merge #f`); a stage without a declared promotion gets the default one (one person approves).
- Environment: when LLM_TXT's `## Environment` has a proposed declaration, start from it: copy its clauses, keep its `; from` comments, decide each `;; conflict:` line and say how in the report, and change it only where PREFERENCES or a stated fact requires. Prefer reuse (`#:flake`, `#:containerfile`, `#:devcontainer`) over retyping packages.
- Run COMPILE. On an error, fix exactly the clause it names, choosing from the alternatives it lists; never delete a clause to silence it. Repeat until it exits 0.

## Decision Table

| LLM.txt fact | DSL choice |
| --- | --- |
| Kind web-app or mobile; Artefacts lists images or video | `(artefact image …)` and `(artefact video …)` with those globs; an `e2e` cmd; an `e2e-test` command; `(check e2e … #:collect (…))`; review `#:artefacts` includes them |
| Kind library or cli; Artefacts lists junit or coverage | `(artefact junit …)`, `(artefact coverage …)` with those globs; the test cmd carries the report flag; `(check test … #:collect (junit))` |
| Any kind | `(artefact log)`, attached to every agent or auto review |
| Forge none, or no forge information | `(forge none)`; `(prompt-input tasklist)`; `(trigger cron)` and `(trigger manual)`; `(review human)` writes its result to the layer |
| Forge github or forgejo with a repo | that `(forge …)` with `#:repo` (and `#:host`); `(prompt-input issue)`; `(trigger webhook)`; keep `(trigger manual)` |
| Environment proposes reuse of a flake, Containerfile or dev container | that option as proposed (`#:flake`, `#:containerfile`, `#:devcontainer`); add `#:browser #t` when Browser says needed |
| Environment lists a lockfile | `(cmd install …)` as proposed and `#:prepare (install)` on the worktree option |
| Toolchain lists nixpkgs attributes and Environment proposes no reuse | `(environment worktree)` plus `(environment nix-devshell #:packages (…))` with exactly those attributes |
| Toolchain lists system packages or services, or zero-touch is enabled | add `(environment podman …)`; `#:network none` when no check needs the network |
| A command row says "needs network: yes" | never `#:network none` in an environment that runs it |
| Protected paths present | `(paths #:protected (…))` with every one; auto review keeps `#:block-protected-paths #t` |
| Zero-touch classes named, checks fast and deterministic, artefacts exist | `(review auto #:require-checks (…) #:allowed-classes (…) #:artefacts (…))`, `#:ship pr` unless PREFERENCES asks for merge or deploy; the `zte` adw |
| Zero-touch `none`, or checks slow or flaky, or no artefacts | no auto review and no `zte`; `(review agent …)` at most |
| A deploy cmd exists | declare the cmd; use `#:ship deploy` only when PREFERENCES asks for it |
| Kind monorepo | cmds compose the per-package commands exactly as LLM_TXT gives them; one expert per package that has its own Layout entry |
| Conditional docs present | one `(conditional-doc "path" "when …")` each |

## Validation

- `tools/al compile $2 >/dev/null` exits 0.
- Every cmd name and shell string in OUTPUT appears in LLM_TXT's Commands table; every protected path of LLM_TXT appears in `#:protected`.
- No auto review exists without `#:require-checks` and a `(check …)` before `(review)` in its adw.
- When `adopted/` exists, both include lines are present, and no step of the layer contradicts a rule in `adopted/RULES.md`.

## Report

- The path of OUTPUT and the final compile result, with the number of repair rounds.
- Under a `### Decisions` heading: the decisions taken, as "fact, then choice" bullets, including every default you relied on (`#:root` above all), and every choice PREFERENCES marks `default` (the facts were silent there).
- Under a `### Left out and why` heading: what was left out and why, as bullets: `UNVERIFIED` items, preferences the language cannot express, kit steps left out because a team rule forbids them (rule and source line).

## Fabric patterns

A command may apply vendored Fabric patterns with `#:fabric (pattern …)`; the generator copies each pattern into the layer and the command reads it as its method, while the command's own Report stays in charge of the output format. Use them where a pattern is the method the command needs, and only names that exist in the Fabric pattern library: `review_code` on the review command, `create_git_diff_commit` on commit, `write_pull-request` on pull-request, `create_prd` on plan-feature, `summarize_git_diff` on summarize, `create_stride_threat_model` or `ask_secure_by_design_questions` on plans for security-sensitive codebases.

## Scaling and sandboxing

Every run already gets its own git worktree. Declare `(concurrency slots …)` only when the defaults (4 runs, 2 agent calls, 2 checks at once) are wrong for the machine. A zero-touch ADW (`review auto` with `#:ship merge` or `deploy`) MUST list a podman option in its `#:environment`; give that option the toolchain from LLM.txt (`#:base-image`, `#:packages`, `#:setup`) and `#:caches` for the package managers the codebase uses. Use `#:agent-inside #t` with `#:credentials` when the agent itself should be sandboxed; it is required for `bypassPermissions`.
