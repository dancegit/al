#lang al
;; Checks that say what they cover: two suites for two directories, a cheap check for the
;; documentation, and one that is not deterministic. The zero-touch workflow asks for the affected
;; checks only, in its check steps (#:cmds affected) and at the gate (#:require affected), so a
;; README change runs `docs` and nothing else. That combination warns by design: a wrong #:covers
;; would ship unproven code.
(agentic-layer #:name "affected-checks-fixture"
  (codebase #:root "." #:languages (python)
    (cmd suite-a "sh a/test.sh" #:covers ("a") #:always #f)
    (cmd suite-b "sh b/test.sh" #:covers ("b") #:always #f)
    (cmd docs "sh check-docs.sh" #:covers ("README.md" "docs") #:always #f)
    (cmd flaky "sh flaky.sh" #:covers ("a") #:reuse #f)
    (paths #:source ("a" "b") #:docs ("README.md" "docs")))
  (environment worktree)
  (environment podman #:id box #:packages ("git"))
  (command plan #:template plan-chore #:output path)
  (hop implement #:template run-plan #:takes plan)
  (command patch #:template plan-patch #:output path)
  (command commit #:template commit)
  (command review #:template review #:output json)
  (review human)
  (review auto #:id zero-touch #:ship merge #:require-checks (suite-a suite-b docs) #:require affected)
  (adw zte #:description "Plan, build, run the checks the change affects, review, ship without a person"
    #:environment (worktree box) #:review zero-touch
    (steps
      (run plan #:command plan #:args ("$prompt"))
      (run build #:command implement #:args ("$out.plan"))
      (check tests #:cmds affected)
      (run commit #:command commit)
      (review #:patch patch #:implement implement)
      (ship)))
  (adw build #:description "The same checks; a person reviews"
    #:environment (worktree) #:review human
    (steps
      (run plan #:command plan #:args ("$prompt"))
      (run build #:command implement #:args ("$out.plan"))
      (check tests #:cmds affected)
      (review)
      (ship))))
