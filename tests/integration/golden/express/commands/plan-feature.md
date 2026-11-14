---
description: plan-feature for <NAME>
argument-hint: [prompt]
---

# Plan Feature

`plan-feature` in layer `<NAME>`: turn a feature request into a phased plan that another agent can implement end to end without asking a single question. Plan only; implement nothing.

## Variables

PROMPT: $1
PLAN_FILE: ${specs_dir}/feature-${adw_id}-<descriptive-name>.md

## Codebase

express is a web framework for Node.js, published to the npm registry as the package express and consumed through require('express'). CommonJS JavaScript with no build step: index.js re-exports lib/express.js. Tested with mocha and supertest, linted with eslint 8, published to npm by a GitHub Actions workflow when a GitHub release is published.

Languages: javascript. Entry document: `Readme.md`. Default branch: `master`.

## Instructions

- You are in `${worktree}`, a git worktree of the application. Read it, do not modify it. The only file you create is PLAN_FILE, which lives in the layer and never in the application tree. Never switch branches, push, or touch the main checkout.
- Research before writing: read `Readme.md`, then the Relevant Files, then the closest existing feature. Follow its patterns, naming and file layout; do not invent a new architecture. Think hard about the smallest design that delivers the user value in PROMPT. Leave out everything PROMPT does not ask for.
- Write PLAN_FILE in exactly the Plan Format: keep every heading, replace every `<placeholder>`, add no sections. `<descriptive-name>` is 3-6 lowercase hyphenated words.
- Be surgical: list only files that must change or be created, and name the function, type or block in each.
- Tests: name every test to add, with its path, under Testing Strategy, and give each a step in Step by Step Tasks. Tests come before or with the code they cover, never after validation. Acceptance Criteria are observable and checkable: each one maps to a test, a validation command or a named artefact.
- Keep the commands already present under `## Validation Commands` in the Plan Format unchanged and in order; add feature-specific commands after them.
- If the feature is user-visible, name in Notes the artefacts that will prove it (screenshots, video, reports), where they land, and the step that produces them.
- Never plan a change under a protected path. If the feature cannot avoid one, plan everything else and add a line starting `REQUIRES HUMAN` to Notes naming the path and the reason.
- A new dependency is added with `npm install` and justified in Notes. Read a conditional document when its condition matches what you are planning.
- A new member of lib/response.js, lib/request.js or lib/application.js gets its tests in test/res.<member>.js, test/req.<member>.js or test/app.<member>.js.
- State the user-visible change as a public API signature or the HTTP response (status, headers, body) an application produces.
- The global Buffer is forbidden; take Buffer from the node:buffer module.
- Code must run on every Node.js version from 18 and must not create global variables: mocha runs with --check-leaks.

## Relevant Files

- `Readme.md`
- `lib`
- `index.js`
- `test`

Protected paths (do not plan changes here):

- `.github`
- `LICENSE`
- `.npmrc`

## Conditional Documentation

- `examples/README.md` — When adding, renaming or removing a directory under examples/
- `History.md` — When a change alters behaviour that an earlier release entry describes



## Plan Format

```md
# Feature: <feature name>

## Feature Description
<what the feature does and the value it delivers>

## User Story
As a <type of user>, I want to <action or goal>, so that <benefit>.

## Problem Statement
<the problem or opportunity this feature addresses>

## Solution Statement
<the approach, and how it fits the existing design>

## Relevant Files
<bullet per existing file: path, why it is relevant, what changes in it; then, under a `### New Files` heading, a bullet per file to create with its purpose>

## Implementation Plan
### Phase 1: Foundation
<shared groundwork needed before the feature itself: types, schema, config, scaffolding>
### Phase 2: Core Implementation
<the feature's main behaviour>
### Phase 3: Integration
<wiring into existing entry points, interfaces and documentation>

## Step by Step Tasks
IMPORTANT: execute every step in order, top to bottom. The last step runs every command under Validation Commands and fixes until all pass.
### <step title>
- <one concrete action per bullet: file, symbol, change>

## Testing Strategy
### Unit Tests
<test path, then what it asserts>
### Integration Tests
<test path, then the flow it covers>
### Edge Cases
<each edge case and the test that covers it>

## Acceptance Criteria
<checkable statements, each tied to a test, command or artefact>

## Validation Commands
Execute every command from the worktree root; all must exit 0.

- `npm run lint` — eslint reports no violation of .eslintrc.yml in any JavaScript file of the repository
- `npm run test-ci` — the whole mocha suite (test/ and test/acceptance/) passes with global-leak checking, under nyc, writing coverage/lcov.info and a text coverage table to stdout
<additional feature-specific commands, each with what it proves; delete this line if none>

## Notes
<new dependencies; artefacts that prove the user-visible result; REQUIRES HUMAN lines; deliberate omissions and follow-ups>
```

## Validation Commands

Do not run these now. They are the commands the plan embeds; the implementer runs them.

- `npm run lint` — eslint reports no violation of .eslintrc.yml in any JavaScript file of the repository
- `npm run test-ci` — the whole mocha suite (test/ and test/acceptance/) passes with global-leak checking, under nyc, writing coverage/lcov.info and a text coverage table to stdout

## Report

Your final message must be ONLY the path of the file you created, on a single line, with no other text.
