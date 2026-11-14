#lang al
(agentic-layer #:name "bad"
  (codebase #:root "." #:languages (go)
    (cmd test "go test ./..."))
  (command plan #:template plan-chore)
  (adw broken
    (steps
      (run plan #:command plan)
      (check unit #:cmd tests))))
