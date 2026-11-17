#lang al
;; A zero-touch workflow whose integration tests build and start containers: the run's container
;; (`box`) gets a rootless podman of its own (#:nested-engine #t), and the check fails when a test
;; was skipped because no podman was there. A second workflow runs its engine test #:in a separate
;; podman option `engine` instead of its own stack.
(agentic-layer #:name "nested-engine-fixture"
  (codebase #:root "." #:languages (python)
    (cmd test "./test.sh")
    (cmd engine-test "sh engine-test.sh"))
  (environment worktree)
  (environment podman #:id box #:packages ("git") #:nested-engine #t #:memory "2g" #:pids-limit 1024)
  (environment podman #:id engine #:packages ("git") #:nested-engine #t #:agent-inside #f)
  (command plan #:template plan-chore #:output path)
  (command review #:template review #:output json)
  (review human)
  (review auto #:id zero-touch #:ship merge #:require-checks (engine-test))
  (adw zte #:description "Plan, build and test inside a sandbox with its own container engine; ships without a person"
    #:environment (worktree box) #:review zero-touch
    (steps
      (run plan #:command plan #:args ("$prompt"))
      (check engine-test #:cmd engine-test #:forbid-skip-reason "no podman")
      (review)
      (ship)))
  (adw build #:description "Plan and build on the host; the engine test runs in its own container"
    #:environment (worktree) #:review human
    (steps
      (run plan #:command plan #:args ("$prompt"))
      (check test #:cmd test)
      (check engine-test #:cmd engine-test #:in engine #:max-skipped 0)
      (review)
      (ship))))
