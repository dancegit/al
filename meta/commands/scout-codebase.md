---
description: Verify and complete a draft LLM.txt for an application checkout by reading the repository
argument-hint: [app-checkout-path] [draft-llm-txt-path] [scout-json-path]
---

# Scout Codebase

Turn the deterministic scout's draft into a final, evidenced `LLM.txt`: the single descriptor from which an agentic layer for this codebase will be authored.

## Variables

APP_DIR: $1
DRAFT: $2
FACTS: $3 (optional: the layer's `scout.json` written by `tools/al scout APP --into LAYER`, with `runs`: each detected command run once in a throwaway worktree, with its exit code, duration and result)
TEMPLATE: meta/LLM.txt.template.md

## Instructions

- Read-only on APP_DIR. Do not install, build, run tests, or modify anything there. The only file you write is DRAFT, replaced by the final `LLM.txt`. The measured runs in FACTS are the only command results you use: `al scout --into` ran them, you do not run them again.
- Read TEMPLATE first. The output has exactly its sections, in its order, with nothing added; an empty section says `none`. Remove the template's comment and guidance text.
- The draft is a starting point produced without judgement by `tools/al scout`. Treat every line of it as a claim to verify; keep, correct or delete it.
- Never invent a command. A command goes in `## Commands` only when a file in the repository states it, and that file and line are cited in `## Evidence`. A command that is merely conventional for the language or framework is not evidenced.
- Commands must work from the repository root. Where the repository runs them from a subdirectory, wrap them (`(cd sub && command)`) and cite where the subdirectory is established.
- Prefer the command the project's own CI runs over one found only in prose; when they differ, use CI's and record the other in `## Unknowns`.
- Mark anything you could not verify in the repository as `UNVERIFIED`, in place, and repeat it in `## Unknowns` with what a human must confirm. An honest `UNVERIFIED` is worth more than a plausible guess.
- Write for an LLM that will see nothing but this file: exact paths, exact commands, exact globs. No adjectives, no history.

## Workflow

1. Read TEMPLATE and DRAFT. Run `git -C APP_DIR ls-files` to see the tree, and `git -C APP_DIR remote -v` and `git -C APP_DIR symbolic-ref --short HEAD` for identity.
2. Read the sources of truth for commands, in this order: CI configuration; Makefile, justfile, task runner files, tox or nox configuration; package and build manifests and their script sections; CONTRIBUTING and developer docs; README.
3. Commands: establish the exact install, lint, format, typecheck, test, test-fast, e2e, build, start, docs and deploy commands that exist. For each: what it proves, typical duration and whether it needs the network. Take the duration and the exit code from FACTS' `runs` when the command ran there with exit 0 (cite `scout.json` in `## Evidence`). A command that failed, timed out or did not run (`not run: needs network`) stays `UNVERIFIED` in place, with the reason from its `result` (e.g. `UNVERIFIED (failed: exit 2)`), and goes into `## Unknowns`. Without FACTS: from CI logs or configuration timeouts if present, else `UNVERIFIED`.
4. Layout: source, test and documentation paths, with how tests are named and where a new test for a given source file goes.
5. Toolchain: language and tool versions from version files, manifests, lock files and CI matrices, as nixpkgs attribute names; system packages and services that commands need.
6. Artefacts: what proof each command can emit and where it lands: junit XML (the flag that enables it), coverage, screenshots, video, traces, HTML reports. Put the flag into the command in `## Commands` when the test tool supports it and the configuration shows how.
7. User-visible change: state under `## Work classes` what a user-visible change looks like in this codebase (a screen, an endpoint response, CLI output, a public API signature) and which artefact can prove it.
8. Protected paths: CI configuration, release and publishing scripts, security-sensitive code (authentication, cryptography, secrets handling), migrations, generated files, vendored code. Give the reason for each.
9. Conventions: the rules stated in CONTRIBUTING, linter and formatter configuration, commit and pull request templates, and those visible in every recent commit. One line each, checkable.
10. Forge and conditional docs: forge, host, repository and CI system from the remote and CI files (FACTS' `forge`, `forge_host`, `repo` and `forge_evidence` are the deterministic reading; confirm or correct them). Read every CI file (`.forgejo/workflows/`, `.github/workflows/`, `.gitea/workflows/`, `.gitlab-ci.yml`) for the work classes it distinguishes (jobs or triggers per kind of change) and the artefacts it uploads (paths and globs); record each under `## Work classes` and `## Artefacts` with the CI file and line in `## Evidence`, and list the CI files you read under `## Forge`. Documents that matter only in specific situations, each with its condition.
11. Delivery: the draft's `## Delivery` section is `al scout`'s proposal of the branching style (trunk, sprint, gitflow, release branches, phase gates) and a `(delivery …)` preset. Confirm or correct it with evidence: branch names (`git -C APP_DIR for-each-ref --format='%(refname:short)' refs/heads refs/remotes`), merges on the default branch (`git -C APP_DIR log --first-parent --merges -20 --format='%h %ad %s' --date=short`), tags, CI triggers and CONTRIBUTING lines, each cited. State which branches only people may change; anything you cannot evidence stays `UNVERIFIED`.
12. Existing agent setup: the draft's table lists what `al scout` found (rule files, prompt libraries, workflow scripts, agent CI, settings, plans, doc caches, an existing al layer). Read each item. Complete its description (what it is for, in one line) and its arguments, and fill `in use?` with `yes` or `no` and the evidence: a reference from CI, a Makefile or task runner, CONTRIBUTING or the README, a recent commit that touched or used it (`git -C APP_DIR log -3 --format='%h %ad %s' --date=short -- <path>`). With no evidence either way the cell stays `UNVERIFIED`: never guess that something is used. Under the table, list the rules of every rule file that an agent must not break (one line each, with `file:line`), and add any item the scout missed. Do not run any script or workflow you find.
13. Write the final file to DRAFT. Then check it against the Validation list below and fix every miss.

## Validation

- Every row of `## Commands` has a line in `## Evidence` with a `file:line` that exists in APP_DIR.
- Every path in Layout, Protected paths and Conditional docs exists in `git ls-files` output (or is a directory prefix of entries in it).
- Every artefact glob is produced by a command listed in `## Commands`.
- Every row of `## Existing agent setup` names a path that exists in `git ls-files` output, and every `in use?` cell is `yes` or `no` with evidence, or `UNVERIFIED`.
- Section headings match TEMPLATE exactly and no guidance text remains.
- `git -C APP_DIR status --short` is unchanged from before you started.

## Report

Output the final `LLM.txt` content, exactly as written to DRAFT, and after it three lines: the number of commands evidenced, the number of items marked `UNVERIFIED`, and the lines of the draft you removed as unsupported.
