#lang racket/base
(require rackunit racket/list racket/string racket/runtime-path json
         "../private/compiler.rkt" "../private/schema.rkt" "../private/reference.rkt"
         "../private/meta.rkt" "../private/vocab.rkt" "../private/json-pretty.rkt")

(define-runtime-path fixtures "fixtures")
(define-runtime-path examples "../examples")

(define base-codebase
  '(codebase #:root "." #:languages (python) (cmd test "pytest") (cmd lint "ruff check")))
(define (layer . clauses) `(agentic-layer #:name "t" ,base-codebase ,@clauses))
(define plan '(command plan #:template plan-chore))
(define review-cmd '(command review #:template review #:output json))
(define simple-adw '(adw a (steps (run plan #:command plan))))

(define (rejects? datum rx)
  (with-handlers ([exn:al? (lambda (e)
                             (or (regexp-match? rx (exn-message e))
                                 (begin (eprintf "message was: ~a\n" (exn-message e)) #f)))])
    (compile-layer-datum datum)
    #f))

(define-syntax-rule (check-rejects datum rx) (check-true (rejects? datum rx) (format "~s" rx)))

(test-case "defaults fill every PITER slot"
  (define ir (compile-layer-datum (layer plan simple-adw)))
  (define adw (car (hash-ref ir 'adws)))
  (check-equal? (hash-ref adw 'input) '("cli"))
  (check-equal? (hash-ref adw 'trigger) '("manual"))
  (check-equal? (hash-ref adw 'environment) '("worktree"))
  (check-equal? (hash-ref adw 'review) "human")
  (check-equal? (hash-ref adw 'agent) "claude-code")
  (check-equal? (hash-ref (hash-ref ir 'storage) 'kind) "local")
  (check-equal? (hash-ref (hash-ref ir 'forge) 'kind) "none")
  (check-equal? (hash-ref ir 'voice) 'null)
  (check-equal? (hash-ref (hash-ref ir 'concurrency) 'maxRuns) 4))

(test-case "output is deterministic and JSON-parseable"
  (define a (json-pretty (compile-layer-datum (layer plan simple-adw))))
  (define b (json-pretty (compile-layer-datum (layer plan simple-adw))))
  (check-equal? a b)
  (check-true (hash? (string->jsexpr a))))

(test-case "compose inlines steps"
  (define ir (compile-layer-datum
              (layer plan
                     '(adw a (steps (run plan #:command plan)))
                     '(adw b (steps (compose a) (check t #:cmd test))))))
  (check-equal? (map (lambda (s) (hash-ref s 'name)) (hash-ref (cadr (hash-ref ir 'adws)) 'steps))
                '("plan" "t")))

(test-case "shape errors name the alternatives"
  (check-rejects (layer plan simple-adw '(prompt-input jira)) #rx"unknown prompt-input kind; kinds are: cli, issue, tasklist")
  (check-rejects (layer plan simple-adw '(trigger cron #:every 5)) #rx"unknown field #:every; fields are: #:interval-seconds")
  (check-rejects (layer plan simple-adw '(storage s3)) #rx"missing required field #:bucket")
  (check-rejects (layer plan simple-adw '(trigger cron #:interval-seconds "5")) #rx"expected an integer")
  (check-rejects (layer '(command plan #:template plan-everything) simple-adw) #rx"one of: prime, install")
  (check-rejects (layer plan simple-adw '(workflow x)) #rx"unknown clause \\(workflow")
  (check-rejects '(agentic-layer #:name "t" (command plan #:template plan-chore)) #rx"missing \\(codebase")
  (check-rejects (layer plan) #rx"at least one \\(adw")
  (check-rejects '(agentic-layer #:name "Bad Name" (codebase #:root "." #:languages (c) (cmd t "make"))) #rx"lowercase"))

(test-case "dangling references are rejected with the declared names"
  (check-rejects (layer plan '(adw a (steps (run p #:command planner)))) #rx"`planner` is not a declared command or hop; declared: plan")
  (check-rejects (layer plan '(adw a (steps (run plan #:command plan) (check t #:cmd unit)))) #rx"`unit` is not a declared codebase cmd; declared: test, lint")
  (check-rejects (layer plan '(adw a #:input (tasklist) (steps (run plan #:command plan)))) #rx"`tasklist` is not a declared prompt-input")
  (check-rejects (layer plan simple-adw '(review agent)) #rx"`review` is not a declared command")
  (check-rejects (layer plan '(adw a (steps (compose b)))) #rx"no such adw|not a declared adw"))

(test-case "semantic rules"
  (check-rejects (layer plan '(adw a (steps (run plan #:command plan) (ship)))) #rx"must come after a \\(review\\)")
  (check-rejects (layer plan '(agent claude-code) '(agent claude-code #:id wild #:permission-mode bypassPermissions)
                        '(adw a (steps (run plan #:command plan #:agent wild))))
                 #rx"agent `wild` uses bypassPermissions")
  (check-rejects (layer plan '(adw a (steps (run plan #:command plan #:agent nobody))))
                 #rx"`nobody` is not a declared agent")
  (check-rejects (layer plan review-cmd '(review auto #:require-checks (test))
                        '(adw a (steps (run plan #:command plan) (review) (ship))))
                 #rx"needs a \\(check …\\) step before")
  (check-rejects (layer plan review-cmd '(review auto #:require-checks ())
                        '(adw a (steps (run plan #:command plan) (check t #:cmd test) (review) (ship))))
                 #rx"#:require-checks must list")
  (check-rejects (layer plan review-cmd '(review auto #:require-checks (test) #:ship deploy)
                        '(adw a (steps (check t #:cmd test) (review) (ship))))
                 #rx"no #:deploy-cmd")
  (check-rejects (layer plan '(prompt-input issue) simple-adw) #rx"no \\(forge github\\|forgejo")
  (check-rejects (layer plan '(prompt-input voice) simple-adw) #rx"needs a \\(voice kokoro")
  (check-rejects (layer plan '(trigger forge-actions) simple-adw) #rx"listens to forge events")
  (check-rejects (layer plan '(agent claude-code #:permission-mode bypassPermissions) simple-adw)
                 #rx"only accepted when the agent itself runs inside a podman")
  (check-rejects (layer plan '(agent claude-code #:permission-mode bypassPermissions)
                        '(environment worktree) '(environment podman)
                        '(adw a #:environment (worktree podman) (steps (run plan #:command plan))))
                 #rx"add \\(environment podman #:agent-inside #t")
  (check-rejects (layer plan review-cmd '(review auto #:require-checks (test))
                        '(adw a (steps (run plan #:command plan) (check t #:cmd test) (review) (ship))))
                 #rx"must include a podman option")
  (check-rejects (layer plan '(environment host) '(adw a (steps (check t #:cmd test))))
                 #rx"need a `worktree` environment")
  (check-rejects (layer plan '(adw a (steps (run plan #:command plan) (run plan #:command plan))))
                 #rx"step name `plan` used twice")
  (check-rejects (layer plan '(adw a (steps (compose b))) '(adw b (steps (compose a)))) #rx"cycle")
  (check-rejects (layer plan simple-adw '(storage local) '(storage local #:id two)) #rx"only one storage")
  (check-rejects (layer plan simple-adw '(trigger cron) '(trigger cron)) #rx"id cron already used"))

(test-case "zero-touch layer that satisfies the rules compiles"
  (define ir (compile-layer-datum
              (layer plan review-cmd
                     '(agent claude-code #:permission-mode bypassPermissions)
                     '(environment worktree) '(environment podman #:agent-inside #t #:caches ("uv") #:credentials (claude))
                     '(concurrency slots #:max-runs 8)
                     '(review auto #:require-checks (test lint))
                     '(adw zte #:environment (worktree podman)
                           (steps (run plan #:command plan) (check t #:cmd test) (review) (ship))))))
  (check-equal? (hash-ref (car (hash-ref ir 'reviews)) 'ship) "merge")
  (check-equal? (hash-ref (hash-ref ir 'concurrency) 'maxRuns) 8)
  (check-equal? (hash-ref (cadr (hash-ref ir 'environments)) 'scope) "run"))

(define build-cmd '(command build #:template build))
(define candidate-adw '(adw candidate (steps (run plan #:command plan) (run build #:command build))))
(define (race-adw . fan-out-fields)
  `(adw parent (steps (fan-out race #:adw candidate ,@fan-out-fields) (check t #:cmd test))))

(test-case "fan-out compiles to a step of the parent"
  (define ir (compile-layer-datum
              (layer plan build-cmd candidate-adw
                     (race-adw '#:count 3 '#:pick 'first '#:max-parallel 2))))
  (define parent (cadr (hash-ref ir 'adws)))
  (check-equal? (car (hash-ref parent 'steps))
                (hasheq 'kind "fan-out" 'name "race" 'adw "candidate" 'count 3 'pick "first" 'maxParallel 2))
  (define defaults (car (hash-ref (cadr (hash-ref (compile-layer-datum
                                                   (layer plan build-cmd candidate-adw (race-adw)))
                                                  'adws))
                                  'steps)))
  (check-equal? (hash-ref defaults 'pick) "best")
  (check-equal? (hash-ref defaults 'count) 3)
  (check-equal? (hash-ref defaults 'maxParallel) 0))

(test-case "fan-out rules"
  (check-rejects (layer plan candidate-adw (race-adw '#:count 1)) #rx"#:count must be between 2 and 8")
  (check-rejects (layer plan candidate-adw (race-adw '#:count 9)) #rx"#:count must be between 2 and 8")
  (check-rejects (layer plan candidate-adw (race-adw '#:max-parallel 4))
                 #rx"#:max-parallel must be between 0 \\(= #:count\\) and #:count")
  (check-rejects (layer plan candidate-adw (race-adw '#:pick 'random)) #rx"one of: best, first, all")
  (check-rejects (layer plan '(adw parent (steps (fan-out race #:adw nobody))))
                 #rx"`nobody` is not a declared adw; declared: parent")
  (check-rejects (layer plan '(adw parent (steps (run plan #:command plan) (fan-out race #:adw parent))))
                 #rx"fan-out race: #:adw must name another ADW, not `parent` itself")
  (check-rejects (layer plan review-cmd '(adw candidate (steps (run plan #:command plan) (review) (ship)))
                        (race-adw))
                 #rx"fan-out race: child ADW `candidate` contains a \\(ship\\) step; candidates must not ship")
  (check-rejects (layer plan build-cmd candidate-adw
                        '(adw middle (steps (fan-out inner #:adw candidate)))
                        '(adw parent (steps (fan-out race #:adw middle))))
                 #rx"fan-out race: child ADW `middle` contains a \\(fan-out\\) step; fan-out does not nest")
  (check-rejects (layer plan
                        '(adw parent (steps (fan-out race #:adw child)))
                        '(adw child (steps (fan-out back #:adw parent))))
                 #rx"fan-out race: cycle parent → child → parent")
  (check-rejects (layer plan
                        '(adw parent (steps (fan-out race #:adw child)))
                        '(adw child (steps (compose parent))))
                 #rx"fan-out race: cycle parent → child")
  (check-rejects (layer plan '(environment host) '(environment worktree)
                        '(adw candidate #:environment (host) (steps (run plan #:command plan)))
                        '(adw parent #:environment (worktree) (steps (fan-out race #:adw candidate))))
                 #rx"fan-out race: child ADW `candidate` needs a `worktree` environment")
  (check-rejects (layer plan '(environment host) '(environment worktree)
                        '(adw candidate #:environment (worktree) (steps (run plan #:command plan)))
                        '(adw parent #:environment (host) (steps (fan-out race #:adw candidate))))
                 #rx"\\(adw parent\\): fan-out race needs a `worktree` environment")
  (check-rejects (layer plan build-cmd review-cmd candidate-adw
                        '(adw parent (steps (review) (fan-out race #:adw candidate) (ship))))
                 #rx"fan-out race must come before the ADW's \\(review\\) and \\(ship\\) steps"))

(test-case "#lang al reports the offending clause's source location"
  (define msg
    (with-handlers ([exn:fail:syntax? exn-message])
      (dynamic-require (build-path fixtures "dangling-ref.rkt") #f)
      "no error"))
  (check-regexp-match #rx"dangling-ref.rkt:6:2" msg)
  (check-regexp-match #rx"`tests` is not a declared codebase cmd; declared: test" msg))

(test-case "examples compile and expose layer-spec"
  (for ([f '("minimal.rkt" "full.rkt")])
    (define spec (dynamic-require (build-path examples f) 'layer-spec))
    (check-equal? (hash-ref spec 'schemaVersion) "agentic-layer-v1")))

(test-case "PITER breadth (implemented kinds)"
  (define (n fam) (count (lambda (k) (not (eq? (kind-status k) 'planned))) (family-kinds (find-family fam))))
  (check-true (>= (n 'prompt-input) 6))
  (check-true (>= (n 'environment) 5))
  (check-true (>= (n 'trigger) 6))
  (check-true (>= (n 'review) 3)))

(test-case "schema and reference cover every family, kind and record"
  (define schema (layer-schema))
  (define ref (reference-text))
  (for ([fam (all-families)])
    (check-true (hash-has-key? (hash-ref schema 'properties) (string->symbol (family-key fam))))
    (for ([k (family-kinds fam)])
      (check-true (string-contains? ref (format "(~a ~a …)" (family-name fam) (kind-name k))))))
  (for ([r (all-records)])
    (check-true (string-contains? ref (format "### `~a`" (record-name r)))))
  (check-true (string-contains? ref "#lang al")))

(test-case "schema and reference include the fan-out step"
  (define schema (layer-schema))
  (define step-alts
    (hash-ref (hash-ref (hash-ref (hash-ref (hash-ref (hash-ref (hash-ref schema 'properties) 'adws) 'items)
                                            'properties) 'steps) 'items) 'oneOf))
  (define fan-out (findf (lambda (s) (equal? (hash-ref (hash-ref (hash-ref s 'properties) 'kind) 'const) "fan-out"))
                         step-alts))
  (check-true (hash? fan-out))
  (check-equal? (hash-ref fan-out 'required) '("adw" "count" "kind" "maxParallel" "name" "pick"))
  (define ref (reference-text))
  (check-true (string-contains? ref "### `step-fan-out`"))
  (check-true (string-contains? ref "(fan-out NAME #:adw CHILD"))
  (check-true (string-contains? ref "fan-out"))
  (define full (dynamic-require (build-path examples "full.rkt") 'layer-spec))
  (check-true (for*/or ([a (hash-ref full 'adws)] [s (hash-ref a 'steps)])
                (equal? (hash-ref s 'kind) "fan-out"))))

(define (in-repo-layer root . clauses)
  `(agentic-layer #:name "t" (codebase #:root ,root #:languages (python) (cmd test "pytest")) ,@clauses))

(test-case "in-repo placement: the layer lives inside the repository, root leads back up"
  (define ir (compile-layer-datum (in-repo-layer ".." '(layer in-repo) plan simple-adw)))
  (check-equal? (hash-ref ir 'layer) (hasheq 'id "in-repo" 'kind "in-repo" 'status "stable" 'dir "agentic"))
  (check-equal? (hash-ref (hash-ref (compile-layer-datum (in-repo-layer "../.." '(layer in-repo #:dir "tools/agentic") plan simple-adw))
                                    'layer) 'dir)
                "tools/agentic")
  (check-rejects (in-repo-layer "." '(layer in-repo) plan simple-adw)
                 #rx"#:root …\\) must lead from the layer directory back to the repository root, i.e. \"..\" \\(got \".\"\\)")
  (check-rejects (in-repo-layer ".." '(layer in-repo #:dir "tools/agentic") plan simple-adw) #rx"i.e. \"../..\"")
  (check-rejects (in-repo-layer "../.." '(layer in-repo #:dir "../agentic") plan simple-adw) #rx"without `..`")
  (check-rejects (in-repo-layer "/abs" '(layer in-repo #:dir "/agentic") plan simple-adw) #rx"relative directory")
  (check-rejects (in-repo-layer "/srv/app" '(layer in-repo) plan simple-adw) #rx"must lead from the layer directory"))

(test-case "#:changes layer: never zero-touch, always a person"
  (define ir (compile-layer-datum (layer plan '(adw a #:changes layer (steps (run plan #:command plan))))))
  (check-equal? (hash-ref (car (hash-ref ir 'adws)) 'changes) "layer")
  (check-equal? (hash-ref (car (hash-ref (compile-layer-datum (layer plan simple-adw)) 'adws)) 'changes) "codebase")
  (check-not-exn (lambda () (compile-layer-datum (layer plan review-cmd '(review agent)
                                                        '(adw a #:changes layer (steps (run plan #:command plan) (review) (ship)))))))
  (check-rejects (layer plan review-cmd '(review auto #:require-checks (test) #:ship pr)
                        '(adw a #:changes layer (steps (run plan #:command plan) (check t #:cmd test) (review) (ship))))
                 #rx"#:changes layer needs a review option of kind human or agent .*no zero-touch mode for layer changes"))

(test-case "review #:body-cmd names a codebase cmd"
  (define ir (compile-layer-datum (layer plan '(review human #:body-cmd lint) simple-adw)))
  (check-equal? (hash-ref (car (hash-ref ir 'reviews)) 'bodyCmd) "lint")
  (check-equal? (hash-ref (car (hash-ref (compile-layer-datum (layer plan simple-adw)) 'reviews)) 'bodyCmd) "")
  (check-rejects (layer plan '(review human #:body-cmd report) simple-adw) #rx"`report` is not a declared codebase cmd"))

(test-case "dashboard: local on loopback by default"
  (define d (hash-ref (compile-layer-datum (layer plan simple-adw)) 'dashboard))
  (check-equal? (hash-ref d 'kind) "local")
  (check-equal? (hash-ref d 'port) 8765)
  (check-equal? (hash-ref d 'bind) "127.0.0.1")
  (check-equal? (hash-ref d 'also) '())
  (check-equal? (hash-ref d 'pollMs) 750)
  (check-equal? (hash-ref d 'systemd) #f)
  (check-equal? (hash-ref d 'tokenEnv) ""))

(test-case "dashboard: podman, none, also-layers"
  (define p (hash-ref (compile-layer-datum
                       (layer plan simple-adw '(dashboard podman #:port 9001 #:also ("../other.al") #:podman-socket #t)))
                      'dashboard))
  (check-equal? (hash-ref p 'kind) "podman")
  (check-equal? (hash-ref p 'port) 9001)
  (check-equal? (hash-ref p 'also) '("../other.al"))
  (check-equal? (hash-ref p 'podmanSocket) #t)
  (check-equal? (hash-ref (hash-ref (compile-layer-datum (layer plan simple-adw '(dashboard none))) 'dashboard) 'kind)
                "none")
  (check-equal? (hash-ref (hash-ref (compile-layer-datum (layer plan simple-adw '(dashboard podman))) 'dashboard)
                          'podmanSocket)
                #f))

(test-case "dashboard: a non-loopback bind needs a token"
  (check-rejects (layer plan simple-adw '(dashboard local #:bind "0.0.0.0")) #rx"not a loopback address.*#:token-env")
  (check-rejects (layer plan simple-adw '(dashboard local #:bind "192.168.1.5")) #rx"#:token-env")
  (define d (hash-ref (compile-layer-datum
                       (layer plan simple-adw '(dashboard local #:bind "0.0.0.0" #:token-env "AL_DASHBOARD_TOKEN")))
                      'dashboard))
  (check-equal? (hash-ref d 'tokenEnv) "AL_DASHBOARD_TOKEN")
  (for ([b '("127.0.0.1" "127.0.1.1" "::1" "localhost")])
    (check-equal? (hash-ref (hash-ref (compile-layer-datum (layer plan simple-adw `(dashboard local #:bind ,b))) 'dashboard) 'bind) b))
  (check-rejects (layer plan simple-adw '(dashboard local #:bind "0.0.0.0" #:token-env "not a name")) #rx"environment variable name")
  (check-rejects (layer plan simple-adw '(dashboard local #:port 70000)) #rx"#:port must be 1")
  (check-rejects (layer plan simple-adw '(dashboard local #:poll-ms 10)) #rx"#:poll-ms must be at least 100")
  (check-rejects (layer plan simple-adw '(dashboard local) '(dashboard none)) #rx"only one dashboard"))
