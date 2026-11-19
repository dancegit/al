#lang al
;; Targeted testing: `suite-a` has a targeted and a smoke form and selects by changed tests, a path
;; map and imports; `suite-b` has only its full form. A run checks what its change touches; the full
;; suites are proven per batch (every 5 landed changes, or 6 hours after a landing). The zero-touch
;; workflow delivers into the sprint stage; a broken batch is bisected and a `fix` run is opened.
(agentic-layer #:name "targeted-testing-fixture"
  (codebase #:root "." #:languages (python)
    (cmd suite-a "run-tests" #:covers ("a") #:always #f
         #:targeted "run-tests {tests}" #:smoke "run-tests --smoke"
         #:path-map (("a/{name}.py" "a/tests/test_{name}.py"))
         #:select (changed-tests path-map imports))
    (cmd suite-b "sh b/test.sh" #:covers ("b") #:always #f)
    (paths #:source ("a" "b") #:tests ("a/tests" "b/tests")))
  (testing targeted
    #:batch ((every 5) (after-hours 6))
    #:escalate ((paths "a/core/**") (diff-lines 1500) (files 40) (lockfile) (layer) (unmapped) (class feature))
    #:on-batch-failure (bisect open-fix))
  (delivery sprint)
  (environment worktree)
  (environment podman #:id box #:packages ("git"))
  (command plan #:template plan-chore #:output path)
  (hop implement #:template run-plan #:takes plan)
  (command patch #:template plan-patch #:output path)
  (command commit #:template commit)
  (command review #:template review #:output json)
  (review human)
  (review auto #:id zero-touch #:ship merge #:require-checks (suite-a suite-b))
  (adw zte #:description "Plan, build, check what the change touches, review, merge into the sprint without a person"
    #:environment (worktree box) #:review zero-touch #:deliver-to sprint
    (steps
      (run plan #:command plan #:args ("$prompt"))
      (run build #:command implement #:args ("$out.plan"))
      (check tests #:cmds affected)
      (run commit #:command commit)
      (review #:patch patch #:implement implement)
      (ship)))
  (adw fix #:description "Fix what broke a batch; a person reviews"
    #:environment (worktree) #:review human #:classes (bug)
    (steps
      (run plan #:command plan #:args ("$prompt"))
      (run build #:command implement #:args ("$out.plan"))
      (check tests #:cmds affected)
      (run commit #:command commit)
      (review)
      (ship))))
