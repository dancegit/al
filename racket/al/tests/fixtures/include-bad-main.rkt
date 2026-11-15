#lang al
(agentic-layer #:name "inc-bad"
  (codebase #:root "." #:languages (python)
    (cmd test "pytest"))
  (command plan #:template plan-chore #:model-set fast)
  (include "include-bad-adw.rkt"))
