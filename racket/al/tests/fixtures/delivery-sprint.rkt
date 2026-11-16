#lang al
;; A small layer that delivers into a sprint branch: (delivery sprint) and a human-reviewed ADW.
;; Its compiled `stages`, `cycle`, `delivery` and `promotions` are kit/runtime/tests/data/delivery-sprint.json,
;; so the runtime's delivery and promotion tests run on the compiler's real output.
(agentic-layer #:name "sprint-fixture"
  (codebase #:root "." #:languages (python)
    (cmd test "./test.sh"))
  (delivery sprint)
  (promotion sprint -> main #:version (bump minor) #:tag "v{version}")
  (command plan #:template plan-chore #:output path)
  (adw sdlc #:description "Plan and build; a person reviews the sprint's pull request"
    (steps
      (run plan #:command plan #:args ("$prompt"))
      (check test #:cmd test)
      (review)
      (ship))))
