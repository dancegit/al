#lang al
;; includes: codebase sub-clauses and top-level clauses from two files
(agentic-layer #:name "inc"
  (codebase #:root "." #:languages (python)
    (cmd test "pytest")
    (include "include-cmds.rkt"))
  (include "include-clauses.rkt"))
