# chore: Add unit tests for the pure helper functions of tools/explain.py. That file has no test of its own: tests/tools covers eject, in-repo layers, al improve and the dashboard, but nothing checks how `al explain` splits a definition into blocks or words a step. Acceptance criteria: (1) one new file, tests/tools/test_explain.py; no other file changes, and tools/explain.py itself is not modified. (2) The tests load tools/explain.py by its file path with importlib (it is a script, not a package), without calling main() and without compiling any layer: no racket, no network, no agent, no subprocess. (3) `listing`: an empty list gives "", one item gives that item, two items give "a and b", three give "a, b and c", empty strings are dropped, and the joining word can be changed (last="or"). (4) `split_blocks` on a small definition written inline in the test: the first block is the header and starts at line 1; a `  ;` comment line directly above a clause belongs to that clause's block; two consecutive `(trigger …)` clauses form one block while two consecutive `(adw …)` clauses stay two blocks; start and end are 1-based and inclusive; input without any clause gives []. (5) `describe_step`: a run step with when=class-bug says "only for bug work"; a check with a resolve command names it and its retries, and a check without one does not mention fixing; a review step with a patch command mentions the patch loop and one without does not; a ship step gives "ship". (6) `uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools -q` passes, and the new tests fail if the behaviour they describe changes.

## Summary

Add unit tests for the pure helper functions of tools/explain.py. That file has no test of its own: tests/tools covers eject, in-repo layers, al improve and the dashboard, but nothing checks how `al explain` splits a definition into blocks or words a step. Acceptance criteria: (1) one new file, tests/tools/test_explain.py; no other file changes, and tools/explain.py itself is not modified. (2) The tests load tools/explain.py by its file path with importlib (it is a script, not a package), without calling main() and without compiling any layer: no racket, no network, no agent, no subprocess. (3) `listing`: an empty list gives "", one item gives that item, two items give "a and b", three give "a, b and c", empty strings are dropped, and the joining word can be changed (last="or"). (4) `split_blocks` on a small definition written inline in the test: the first block is the header and starts at line 1; a `  ;` comment line directly above a clause belongs to that clause's block; two consecutive `(trigger …)` clauses form one block while two consecutive `(adw …)` clauses stay two blocks; start and end are 1-based and inclusive; input without any clause gives []. (5) `describe_step`: a run step with when=class-bug says "only for bug work"; a check with a resolve command names it and its retries, and a check without one does not mention fixing; a review step with a patch command mentions the patch loop and one without does not; a ship step gives "ship". (6) `uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools -q` passes, and the new tests fail if the behaviour they describe changes.

ADW `sdlc` run `987d41e7`, class `chore`, branch `chore/987d41e7-add-unit-tests-for-the-pure`.

## Plan

`<repo>/agentic/specs/chore-987d41e7-unit-tests-explain-helpers.md`

## Checks

- `test-compiler`: exit 0 — log `agents/987d41e7/checks/test-compiler.1.log`
- `test-runtime`: exit 0 — log `agents/987d41e7/checks/test-runtime.1.log`
- `test-tools`: exit 0 — log `agents/987d41e7/checks/test-tools.1.log`
- `test-dashboard`: exit 0 — log `agents/987d41e7/checks/test-dashboard.1.log`
- `test-integration`: exit 0 — log `agents/987d41e7/checks/test-integration.1.log`

## Review verdict

PASS (human): awaiting human review

- none

## Why a human is needed

- review-human: a human merges

## Artefacts

- log (log): file://<repo>/agentic/artefacts/987d41e7/log/test-compiler.1.log
- log (log): file://<repo>/agentic/artefacts/987d41e7/log/test-dashboard.1.log
- log (log): file://<repo>/agentic/artefacts/987d41e7/log/test-integration.1.log
- log (log): file://<repo>/agentic/artefacts/987d41e7/log/test-runtime.1.log
- log (log): file://<repo>/agentic/artefacts/987d41e7/log/test-tools.1.log
