#lang al
;; The definition the quick-start rehearsal (tests/quickstart/run.sh) puts in place of the two steps
;; that need an agent login: scout-codebase, which completes LLM.txt, and author-layer, which writes
;; this file. It describes the tiny application the rehearsal creates at ~/code/myapp: one source
;; file and a test script. Every other choice is the default; the dry run uses the stand-in agent.
(agentic-layer #:name "myapp" #:description "Agentic layer of the quick-start rehearsal's tiny app"
  (codebase #:root "../myapp" #:languages (shell) #:description "A tiny app: app.sh and test.sh."
    (cmd test "./test.sh" #:timeout 60 #:description "the tests pass")
    (paths #:source ("app.sh") #:tests ("test.sh")))
  (command plan #:template plan-chore #:model-set heavy #:output path #:validation (test))
  (hop implement #:template run-plan #:takes plan)
  (adw plan-build #:description "Plan a change and implement the plan"
    (steps
      (run plan #:command plan #:args ("$prompt"))
      (run build #:command implement #:args ("$out.plan"))
      (check test #:cmd test))))
