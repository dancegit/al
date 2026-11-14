#lang al
;; The smallest useful layer: every PITER choice is the default
;; (cli prompt, manual trigger, git worktree, human PR review, Claude Code, local artefacts).
(agentic-layer #:name "minimal"
  (codebase #:root "../app" #:languages (python)
    (cmd test "uv run pytest -q"))
  (command plan #:template plan-chore #:model-set heavy #:output path #:validation (test))
  (hop implement #:template run-plan #:takes plan)
  (adw plan-build
    (steps
      (run plan #:command plan #:args ("$prompt"))
      (run build #:command implement #:args ("$out.plan"))
      (check test #:cmd test))))
