---
description: Fix exactly the clause of an agentic.rkt that the compiler rejected, then recompile
argument-hint: [agentic-rkt-path] [compiler-error-text]
---

# Repair Layer

Make a rejected `agentic.rkt` compile by correcting the one clause the compiler complained about, using the alternatives the error itself lists. Minimal, faithful to the author's intent.

## Variables

LAYER_SOURCE: $1
ERROR: $2
DSL_REFERENCE: docs/AL-DSL.llms.txt
COMPILE: tools/al compile $1 >/dev/null

## Instructions

- The only file you edit is LAYER_SOURCE.
- Read ERROR completely. It gives a source location (file, line, column), what was found, and the valid alternatives or the rule that was broken. The fix is at that location and is chosen from those alternatives.
- Read the reported clause and the record or family it belongs to in DSL_REFERENCE before editing. DSL_REFERENCE is the whole language; anything not in it does not exist.
- Fix exactly the reported clause. Do not reformat, reorder or "improve" anything else in the file.
- Preserve intent. Decide what the author meant and choose the alternative that expresses it:
  - unknown kind, field or value: replace it with the listed alternative closest in meaning, not the first in the list;
  - dangling reference: if the name is a misspelling of a declared name, correct the spelling; if the thing was meant to exist, declare it (before its first use) rather than removing the reference;
  - missing required field: add it with a value supported by the rest of the file or its `LLM.txt`; if nothing supports a value, stop and report;
  - rule violation (ship before review, zero-touch without a check, forge-dependent input without a forge, conflicting environments): add or move what the rule requires; do not downgrade the feature to escape the rule unless nothing declared can satisfy it.
- Never delete a clause just to make the error go away. Deleting is correct only when the clause is a duplicate, and then you say so.
- Never invent a cmd, path or option value that the file's `LLM.txt` does not support in order to satisfy the compiler.
- Recompile after each edit. A new error is a new round: apply the same rules to it. If the same error returns after your fix, re-read it; you misread the location or the alternatives.
- Stop after 8 rounds without convergence and report the remaining error verbatim.

## Workflow

1. Parse ERROR: location, found, expected.
2. Read that clause in LAYER_SOURCE and its definition in DSL_REFERENCE.
3. Choose the alternative that preserves intent; make the edit.
4. Run COMPILE.
5. Exit 0: done. Otherwise take the new error text and go to step 1.

## Validation

- `tools/al compile $1 >/dev/null` exits 0.
- The diff of LAYER_SOURCE touches only clauses named by an error in some round.
- No clause present before the repair is missing after it, except declared duplicates.

## Report

- One bullet per round: the error in one line, the clause, the alternative chosen and why it preserves intent.
- The final compile result.
- Anything you could not fix without inventing a fact, with the exact question a human must answer.
