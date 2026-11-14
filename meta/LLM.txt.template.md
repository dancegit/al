# <name>

> <One paragraph: what the codebase is, who uses it, what it is built with, and how it is shipped. Facts only.>

<!--
LLM.txt descriptor format for `al`. One file per codebase. It is the ONLY input an LLM gets when
authoring `agentic.rkt`, so it must be complete, exact and evidenced.
Rules: keep every section and its order; write `none` for an empty section; never invent a command;
every command in `## Commands` has a citation in `## Evidence`; mark anything not verified in the
repository as `UNVERIFIED`. Delete this comment and all `<…>` guidance in the final file.
-->

## Identity

- Repo URL: <https or ssh URL; `none` if local only>
- Default branch: <branch that ships>
- License: <SPDX id or `none`>
- Primary languages: <most important first>
- Kind: <one of: library | cli | service | web-app | mobile | monorepo | data/ml | infra>

## Layout

<One line per path, relative to the repo root: `path — purpose`. Cover source, tests, docs, config, scripts, generated output.>

- `<path>` — <purpose>
- `<tests path>` — <what kind of tests live here and how they are named>
- `<docs path>` — <what is documented here>

## Commands

<Only commands that work from the repo root on a fresh checkout after `install`. Names are drawn from:
install, lint, format, typecheck, test, test-fast, e2e, build, start, docs, deploy. One row per name that exists; omit names the codebase does not have.>

| name | shell command from repo root | proves what | typical duration | needs network? |
| --- | --- | --- | --- | --- |
| install | `<command>` | <dependencies resolve and install> | <e.g. 40s> | <yes/no> |
| lint | `<command>` | <…> | <…> | <…> |
| test | `<command, including the flags that emit reports>` | <…> | <…> | <…> |

## Toolchain

- nixpkgs: <attribute names with versions, e.g. `python312` (3.12), `uv` (0.5)>
- System packages: <libraries and tools not covered above; `none`>
- Services: <databases, queues, browsers, emulators a command needs, and which command; `none`>

## Artefacts

<What proof the codebase can emit, and the glob (relative to the repo root) where it lands. One line per type that exists:
junit, coverage, images, video, traces, html. Include the command and flag that produces it.>

- junit: `<glob>` — produced by `<command name>` <with which flag>
- coverage: `<glob>` — <…>
- images: `<glob>` — <…>

## Conventions

<Rules an agent must follow here, one line each, specific and checkable. Code style, naming, where tests go,
commit message format, changelog, dependency policy, error handling, public API rules.>

- <rule>

## Protected paths

<Paths an agent must not change without a human, one per line with the reason:
CI config, release and publishing, security-sensitive code, migrations, generated files, lock files where policy requires.>

- `<path>` — <reason>

## Work classes

- Chore: <what a typical chore looks like here; files touched; which commands prove it>
- Bug: <what a typical bug fix looks like; where the regression test goes>
- Feature: <what a typical feature looks like; what makes it user-visible; which artefact proves it>
- Safe for zero-touch: <classes that may ship without a human, and why (checks are fast, deterministic, and emit artefacts); or `none`>

## Forge

- Forge: <github | forgejo | none>
- Repo: <owner/name; host if not the default>
- CI: <CI system, config path, and which commands it runs>

## Conditional docs

<Documents an agent must read only in a specific situation: `doc path — when to read it`.>

- `<doc path>` — when <condition>

## Evidence

<One line per command in `## Commands`, and per protected path and artefact glob: `claim — file:line` where the repository states it.>

- `<name>` — `<file>:<line>`

## Unknowns

<Everything that could not be verified, and what a human would need to confirm. `none` if nothing.>

- <unknown>
