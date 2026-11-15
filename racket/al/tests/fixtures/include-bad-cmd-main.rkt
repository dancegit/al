#lang al
(agentic-layer #:name "inc-bad-cmd"
  (codebase #:root "." #:languages (python)
    (cmd test "pytest")
    (include "include-bad-cmd.rkt"))
  (command plan #:template plan-chore)
  (adw a (steps (run plan #:command plan))))
