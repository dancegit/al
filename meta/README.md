# meta — commands that build a layer for a new codebase

These prompts run in the `al` repository, not inside a generated layer. They drive an LLM through the two
things only an LLM authors: the descriptor (`LLM.txt`) and the layer declaration (`agentic.rkt`).
Everything after that is deterministic. Arguments are plain positional (`$1`, `$2`, `$3`).

## Order of use

| # | Step | Consumes | Produces |
| --- | --- | --- | --- |
| 1 | `tools/al scout` (deterministic) | an application checkout | a draft `LLM.txt` |
| 2 | `commands/scout-codebase.md` | `$1` checkout path, `$2` draft `LLM.txt`; reads `LLM.txt.template.md` | the final `LLM.txt`: verified, every command evidenced, gaps marked `UNVERIFIED` |
| 3 | `commands/author-layer.md` | `$1` `LLM.txt`, `$2` output path, `$3` PITER preferences (optional); reads `docs/AL-DSL.llms.txt` | an `agentic.rkt` that compiles |
| 4 | `commands/repair-layer.md` (only on a compile error) | `$1` `agentic.rkt`, `$2` compiler error text | the same file with the reported clause fixed, recompiled |
| 5 | `commands/review-layer.md` | `$1` `agentic.rkt`, `$2` `LLM.txt` | findings with severity and a pass/fail verdict |
| 6 | compiler, then generator (deterministic) | `agentic.rkt` | JSON IR, then the layer instantiated from `kit/` |

Loop: a blocker from step 5 goes back to step 3 (wrong choice) or step 2 (wrong or missing fact).
A compile error at any point goes to step 4. Generate only on `verdict: pass`.

## Files

- `LLM.txt.template.md` — the descriptor format: fixed sections in a fixed order. Step 2 fills it; steps 3 and 5 read nothing else about the codebase.
- `commands/scout-codebase.md` — reads the repository read-only and never invents a command; each one is cited in `Evidence`. It confirms or corrects the `## Delivery` section (the branching style and the proposed `(delivery …)` preset) with branch names, merges and CONTRIBUTING lines.
- `commands/author-layer.md` — maps `LLM.txt` facts to DSL choices through its decision table, then iterates against the compiler. It writes the proposed `(delivery …)` preset when `## Delivery` is evidenced, never lets a zero-touch ADW deliver where agents may not merge, and uses `#:deliver-to` for urgent routes. Its report puts the decisions under `### Decisions` and the omissions under `### Left out and why`, which the dashboard's Job page and `al author` show as sections.
- `commands/repair-layer.md` — fixes exactly what the compiler reported, from the alternatives the error lists; never deletes a clause to silence it.
- `commands/review-layer.md` — checks truth and fitness, which the compiler cannot: commands traceable to `LLM.txt`, protected paths carried over, no zero-touch without checks and artefacts, focus lines specific.

## Principles

- `LLM.txt` is the only source of codebase facts; `docs/AL-DSL.llms.txt` is the whole language. Anything in neither does not exist.
- The compiler is the closed loop for coherence; `review-layer` is the closed loop for truth.
- Fix the system, not the output: when a generated layer misbehaves, correct `LLM.txt` or `agentic.rkt` and regenerate; do not hand-edit the generated files.
