---
description: meta-adw for <NAME>
argument-hint: [workflow-description]
---

# Meta-ADW (workflow meta-prompt)

`new-adw` in layer `<NAME>`: turn the description of a workflow into a new `(adw …)` clause in the layer's source, and iterate against the compiler until it compiles. The compiler is the feedback loop.

## Variables

WORKFLOW: $1
LAYER_SOURCE: ${layer_dir}/agentic.rkt
DSL_REFERENCE: ${layer_dir}/AL-DSL.llms.txt
COMPILE: ${layer_dir}/bin/al compile ${layer_dir} >/dev/null

## Instructions

- You edit exactly one file, LAYER_SOURCE. Do not edit application code, generated files in the layer, or DSL_REFERENCE. Never switch branches, push, or touch the main checkout.
- Read DSL_REFERENCE in full first. It is the complete language: any form, kind, field or value not listed there does not exist and will be rejected. Read the `adw` record, the four step records and "Rules the compiler enforces" twice.
- Read LAYER_SOURCE in full and list what is declared: cmds, prompt inputs, triggers, environments, reviews, agents, artefacts, commands, hops and existing adws. The new workflow may use only these names.
- Do not declare new options, commands or cmds to make the workflow possible. If WORKFLOW needs something that is not declared, build the closest workflow from what exists and report what is missing.
- Add one `(adw NAME …)` clause after the last existing one. `NAME` is 1-3 lowercase hyphenated words and unique. Give it a `#:description`.
- Compose, do not copy: when the first steps are an existing adw, start with `(compose THAT-ADW)`.
- One agent, one prompt, one purpose: each `(run …)` step names one command or hop and passes it only the arguments it needs, using the argument variables listed under `step-run`.
- Close every loop: after a step that changes code, add a `(check …)` for each relevant cmd, with a `#:resolve` command when one is declared, and `#:collect` the artefacts that check produces.
- `(review)` comes after the checks; `(ship)` only after `(review)`. A zero-touch review needs at least one `(check …)` before it.
- Choose PITER fields (`#:input`, `#:trigger`, `#:environment`, `#:review`, `#:agent`) only where WORKFLOW differs from the defaults; a workflow whose steps write needs a worktree environment.
- Change nothing else in the file. Never delete or loosen an existing clause to get past an error.
- Run COMPILE after every edit. On an error, read its message: it names the location and lists the valid alternatives. Fix exactly that clause from the listed alternatives, then compile again. Repeat until it exits 0.




## Workflow

1. Read DSL_REFERENCE and LAYER_SOURCE.
2. Write the step list in plain words; map each step to a declared command, hop or cmd.
3. Add the `(adw …)` clause.
4. Run COMPILE; repair until it exits 0.
5. If the layer is a git repository, run `git -C ${layer_dir} diff -- agentic.rkt` and confirm the diff is the new clause only; otherwise re-read the file and confirm nothing else changed.

## Validation Commands

- `${layer_dir}/bin/al compile ${layer_dir} >/dev/null`: exits 0 when the layer is coherent; every error is source-located and lists the alternatives.

## Report

- The new `(adw …)` clause, in a code block.
- The final compile result, and how many compile-and-repair rounds it took.
- Anything WORKFLOW asked for that could not be expressed with declared names, as the declarations a human would have to add.

Finish with a concise report in plain markdown. No preamble.
