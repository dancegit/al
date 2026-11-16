#lang racket/base
(require rackunit racket/list racket/string racket/runtime-path racket/file json
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
                        '(environment worktree) '(environment podman #:agent-inside #f)
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

(define (agent-inside-of . fields)
  (hash-ref (cadr (hash-ref (compile-layer-datum
                             (layer plan '(environment worktree) `(environment podman ,@fields)
                                    '(adw a #:environment (worktree podman) (steps (run plan #:command plan)))))
                            'environments))
            'agentInside))

(test-case "#:agent-inside is honoured when written"
  (check-equal? (agent-inside-of '#:agent-inside #f) #f)
  (check-equal? (agent-inside-of '#:agent-inside #t) #t))

(test-case "a container means the agent is inside unless the layer says otherwise"
  (check-equal? (agent-inside-of) #t)
  ;; so a plain container is enough for bypassPermissions
  (check-not-exn
   (lambda ()
     (compile-layer-datum
      (layer plan '(agent claude-code #:permission-mode bypassPermissions)
             '(environment worktree) '(environment podman)
             '(adw a #:environment (worktree podman) (steps (run plan #:command plan))))))))

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

;; ---------------------------------------------------------------------------------------- model tiers

(define (agent-of ir [id "claude-code"])
  (findf (lambda (a) (equal? (hash-ref a 'id) id)) (hash-ref ir 'agents)))
(define (tiers-of agent)
  (for/list ([m (hash-ref agent 'models)]) (list (hash-ref m 'tier) (hash-ref m 'model) (hash-ref m 'effort))))

(test-case "model tiers: claude-code defaults, shorthands and #:models compile to one list"
  (define ir (compile-layer-datum (layer plan simple-adw)))
  (check-equal? (tiers-of (agent-of ir))
                '(("base" "claude-opus-5-5" "default") ("heavy" "claude-opus-5-5" "default")))
  (check-false (hash-has-key? (agent-of ir) 'modelBase))
  (check-false (hash-has-key? (agent-of ir) 'modelHeavy))
  (define ir2 (compile-layer-datum
               (layer plan simple-adw '(agent claude-code #:model-base "b-1"
                                              #:models ((fast "claude-haiku-5-5") (heavy "h-2" #:effort high))))))
  (check-equal? (tiers-of (agent-of ir2))
                '(("base" "b-1" "default") ("fast" "claude-haiku-5-5" "default") ("heavy" "h-2" "high")))
  (define codex (agent-of (compile-layer-datum (layer plan simple-adw '(agent codex))) "codex"))
  (check-equal? (map car (tiers-of codex)) '("base" "heavy"))
  (check-false (hash-has-key? (agent-of (compile-layer-datum (layer plan simple-adw '(agent mock))) "mock") 'models)))

(test-case "model tiers: a tier defined twice, a bad name, inherit"
  (check-rejects (layer plan simple-adw '(agent claude-code #:model-heavy "x" #:models ((heavy "y"))))
                 #rx"tier `heavy` is given twice: by #:model-heavy and in #:models")
  (check-rejects (layer plan simple-adw '(agent claude-code #:models ((fast "a") (fast "b"))))
                 #rx"`fast` is given twice")
  (check-rejects (layer plan simple-adw '(agent claude-code #:models ((Fast "a"))))
                 #rx"expected a tier name")
  (check-rejects (layer plan simple-adw '(agent claude-code #:models ((inherit "a"))))
                 #rx"`inherit` is not a tier name")
  (check-rejects (layer plan simple-adw '(agent claude-code #:models ((fast))))
                 #rx"expected \\(TIER MODEL")
  (check-rejects (layer '(command plan #:template plan-chore #:model-set inherit) simple-adw)
                 #rx"inherit is only for run steps"))

(test-case "model tiers: every tier a workflow uses is declared on the agent that runs it"
  (define fast-plan '(command plan #:template plan-chore #:model-set fast))
  (check-rejects (layer fast-plan simple-adw)
                 #rx"\\(adw a\\): step `plan` \\(the #:model-set of command `plan`\\) uses model tier `fast`, but agent `claude-code` declares the tiers: base, heavy")
  (check-not-exn (lambda () (compile-layer-datum (layer fast-plan simple-adw '(agent claude-code #:models ((fast "h")))))))
  (check-not-exn (lambda () (compile-layer-datum (layer fast-plan simple-adw '(agent mock)))))
  ;; a step's own tier on the step's own agent
  (check-rejects (layer plan '(agent claude-code #:models ((fast "h"))) '(agent codex #:id other)
                        '(adw a (steps (run plan #:command plan #:model-set fast #:agent other))))
                 #rx"step `plan` \\(its #:model-set\\) uses model tier `fast`, but agent `other` declares the tiers: base, heavy")
  ;; a resolve command runs on the ADW's agent
  (check-rejects (layer plan '(command fix #:template resolve-test #:model-set deep)
                        '(adw a (steps (run plan #:command plan) (check t #:cmd test #:resolve fix))))
                 #rx"check `t` \\(the #:model-set of its resolve command `fix`\\) uses model tier `deep`")
  ;; the review command runs on the review option's agent
  (check-rejects (layer plan '(command review #:template review #:output json #:model-set fast)
                        '(agent claude-code #:models ((fast "h"))) '(agent codex #:id second)
                        '(review agent #:agent second)
                        '(adw a (steps (run plan #:command plan) (review) (ship))))
                 #rx"review `agent` \\(the #:model-set of review command `review`\\) uses model tier `fast`, but agent `second`"))

(test-case "model tiers: #:effort only where the CLI can take it"
  (check-equal? (third (cadr (tiers-of (agent-of (compile-layer-datum
                                                    (layer plan simple-adw '(agent claude-code #:models ((heavy "h" #:effort max)))))))))
                "max")
  (check-rejects (layer plan simple-adw '(agent codex #:models ((heavy "h" #:effort max))))
                 #rx"codex does not offer")
  (check-not-exn (lambda () (compile-layer-datum (layer plan simple-adw '(agent codex #:models ((heavy "h" #:effort xhigh)))))))
  (check-rejects (layer plan simple-adw '(agent custom #:command "x" #:models ((heavy "h" #:effort low))))
                 #rx"a custom agent has no way to pass it")
  (check-rejects (layer plan simple-adw '(agent claude-code #:models ((heavy "h" #:effort huge))))
                 #rx"one of: default, low, medium, high, xhigh, max"))

(test-case "review #:agent: names a declared agent, which obeys the sandbox rule"
  (define ir (compile-layer-datum (layer plan review-cmd '(agent claude-code) '(agent codex #:id second)
                                         '(review agent #:agent second)
                                         '(adw a (steps (run plan #:command plan) (review) (ship))))))
  (check-equal? (hash-ref (car (hash-ref ir 'reviews)) 'agent) "second")
  (check-equal? (hash-ref (car (hash-ref (compile-layer-datum (layer plan simple-adw '(review agent #:command plan))) 'reviews)) 'agent) "")
  (check-rejects (layer plan review-cmd '(review agent #:agent nobody) simple-adw) #rx"`nobody` is not a declared agent")
  (check-rejects (layer plan review-cmd '(agent claude-code) '(agent claude-code #:id wild #:permission-mode bypassPermissions)
                        '(review agent #:agent wild)
                        '(adw a (steps (run plan #:command plan) (review) (ship))))
                 #rx"agent `wild` uses bypassPermissions"))

;; ---------------------------------------------------------------------------------------- budget

(test-case "budget: absent = null; limits are numbers > 0"
  (check-equal? (hash-ref (compile-layer-datum (layer plan simple-adw)) 'budget) 'null)
  (define b (hash-ref (compile-layer-datum (layer plan simple-adw '(budget limits #:run-usd 5 #:step-usd 2.5))) 'budget))
  (check-equal? (hash-ref b 'runUsd) 5)
  (check-equal? (hash-ref b 'stepUsd) 2.5)
  (check-equal? (hash-ref b 'dayUsd) 'null)
  (check-true (string? (json-pretty b)))
  (check-rejects (layer plan simple-adw '(budget limits #:run-usd 0)) #rx"#:run-usd must be greater than 0")
  (check-rejects (layer plan simple-adw '(budget limits #:day-usd "50")) #rx"expected a number ≥ 0")
  (check-rejects (layer plan simple-adw '(budget limits #:step-usd -1)) #rx"expected a number ≥ 0")
  (check-rejects (layer plan simple-adw '(budget limits) '(budget limits #:id two)) #rx"only one budget"))

(test-case "budget: subscription usage threshold"
  (define (budget-of . fields)
    (hash-ref (compile-layer-datum (layer plan simple-adw `(budget limits ,@fields))) 'budget))
  ;; defaults: the threshold is off
  (define d (budget-of))
  (check-equal? (hash-ref d 'limitStopPercent) 0)
  (check-equal? (hash-ref d 'onLimit) "stop")
  (check-equal? (hash-ref d 'limitWaitMinutes) 300)
  ;; all three given, next to a dollar limit
  (define b (budget-of '#:run-usd 5 '#:limit-stop-percent 90 '#:on-limit 'wait '#:limit-wait-minutes 120))
  (check-equal? (hash-ref b 'limitStopPercent) 90)
  (check-equal? (hash-ref b 'onLimit) "wait")
  (check-equal? (hash-ref b 'limitWaitMinutes) 120)
  (check-equal? (hash-ref b 'runUsd) 5)
  (check-equal? (hash-ref b 'dayUsd) 'null)
  ;; the ends of the range, and an explicit 0 with stop
  (check-equal? (hash-ref (budget-of '#:limit-stop-percent 100 '#:on-limit 'wait '#:limit-wait-minutes 1) 'limitStopPercent) 100)
  (check-equal? (hash-ref (budget-of '#:limit-stop-percent 0 '#:on-limit 'stop) 'limitStopPercent) 0)
  ;; each rejection
  (check-rejects (layer plan simple-adw '(budget limits #:limit-stop-percent 101))
                 #rx"#:limit-stop-percent must be between 0 and 100 \\(got 101\\); 0 or leaving it out turns the threshold off")
  (check-rejects (layer plan simple-adw '(budget limits #:limit-stop-percent -5))
                 #rx"#:limit-stop-percent must be between 0 and 100 \\(got -5\\)")
  (check-rejects (layer plan simple-adw '(budget limits #:limit-stop-percent 90.5)) #rx"#:limit-stop-percent: expected an integer, got 90.5")
  (check-rejects (layer plan simple-adw '(budget limits #:limit-stop-percent "90")) #rx"#:limit-stop-percent: expected an integer")
  (check-rejects (layer plan simple-adw '(budget limits #:on-limit wait))
                 #rx"#:on-limit wait needs #:limit-stop-percent above 0")
  (check-rejects (layer plan simple-adw '(budget limits #:limit-stop-percent 0 #:on-limit wait))
                 #rx"#:on-limit wait needs #:limit-stop-percent above 0")
  (check-rejects (layer plan simple-adw '(budget limits #:limit-stop-percent 90 #:on-limit pause))
                 #rx"#:on-limit: expected one of: stop, wait, got pause")
  (check-rejects (layer plan simple-adw '(budget limits #:limit-stop-percent 90 #:on-limit wait #:limit-wait-minutes 0))
                 #rx"#:limit-wait-minutes must be at least 1 \\(got 0\\)")
  (check-rejects (layer plan simple-adw '(budget limits #:limit-wait-minutes -10)) #rx"#:limit-wait-minutes must be at least 1")
  (check-rejects (layer plan simple-adw '(budget limits #:limit-wait-minutes 1.5)) #rx"#:limit-wait-minutes: expected an integer")
  ;; the error of a layer file points at the (budget …) clause
  (define dir (make-temporary-file "al-limit-~a" 'directory))
  (call-with-output-file (build-path dir "agentic.rkt")
    (lambda (o) (write-string "#lang al\n(agentic-layer #:name \"x\"\n  (codebase #:root \".\" #:languages (c) (cmd test \"make\"))\n  (command plan #:template plan-chore)\n  (budget limits #:run-usd 5\n          #:limit-stop-percent 150)\n  (adw a (steps (run plan #:command plan))))\n" o)))
  (define msg (with-handlers ([exn:fail:syntax? exn-message]) (dynamic-require (build-path dir "agentic.rkt") #f) "no error"))
  (check-regexp-match #rx"agentic.rkt:5:2.*#:limit-stop-percent must be between 0 and 100 \\(got 150\\)" msg)
  (delete-directory/files dir)
  ;; the schema and the reference are generated from the same three fields
  (define props (hash-ref (cadr (hash-ref (hash-ref (hash-ref (layer-schema) 'properties) 'budget) 'oneOf)) 'properties))
  (check-equal? (hash-ref (hash-ref props 'limitStopPercent) 'type) "integer")
  (check-equal? (hash-ref (hash-ref props 'onLimit) 'enum) '("stop" "wait"))
  (check-equal? (hash-ref (hash-ref props 'limitWaitMinutes) 'type) "integer")
  (for ([needle '("#:limit-stop-percent" "#:on-limit" "#:limit-wait-minutes" "#:on-limit wait needs a #:limit-stop-percent above 0")])
    (check-true (string-contains? (reference-text) needle) needle))
  ;; the full example shows the fields
  (define full (hash-ref (dynamic-require (build-path examples "full.rkt") 'layer-spec) 'budget))
  (check-equal? (list (hash-ref full 'limitStopPercent) (hash-ref full 'onLimit) (hash-ref full 'limitWaitMinutes)) '(90 "wait" 120)))

;; ---------------------------------------------------------------------------------------- notify

(test-case "notify: none by default; email and command compile; several need ids"
  (define (notifiers . clauses) (hash-ref (compile-layer-datum (apply layer plan simple-adw clauses)) 'notifiers))
  (check-equal? (map (lambda (n) (hash-ref n 'kind)) (notifiers)) '("none"))
  (define mail (car (notifiers '(notify email))))
  (check-equal? (hash-ref mail 'events) '("needs-human" "failed"))
  (check-equal? (hash-ref mail 'toEnv) "AL_NOTIFY_TO")
  (check-equal? (hash-ref mail 'fromEnv) "AL_NOTIFY_FROM")
  (check-equal? (hash-ref mail 'smtpUrlEnv) "AL_NOTIFY_SMTP_URL")
  (check-equal? (hash-ref mail 'timeout) 10)
  (define two (notifiers '(notify email #:to-env "OPS_MAIL" #:events (shipped))
                         '(notify email #:id second #:events (needs-human failed passed shipped))
                         '(notify command #:run "ntfy publish \"$NTFY_TOPIC\"" #:events (needs-human))))
  (check-equal? (map (lambda (n) (hash-ref n 'id)) two) '("email" "second" "command"))
  (check-equal? (hash-ref (car two) 'toEnv) "OPS_MAIL")
  (check-equal? (hash-ref (caddr two) 'run) "ntfy publish \"$NTFY_TOPIC\"")
  (check-rejects (layer plan simple-adw '(notify email) '(notify email)) #rx"id email already used; add #:id")
  (check-rejects (layer plan simple-adw '(notify command)) #rx"missing required field #:run")
  (check-rejects (layer plan simple-adw '(notify email #:events (done))) #rx"one of: needs-human, failed, passed, shipped")
  (check-rejects (layer plan simple-adw '(notify email #:events ())) #rx"#:events is empty")
  (check-rejects (layer plan simple-adw '(notify command #:run "true" #:timeout 0)) #rx"#:timeout must be 1–60")
  (check-rejects (layer plan simple-adw '(notify slack)) #rx"unknown notify kind; kinds are: none, email, command"))

(test-case "notify: a private value in the layer file is rejected, and never echoed"
  (define (rejected-as value kw rx)
    (define message
      (with-handlers ([exn:al? exn-message])
        (compile-layer-datum (layer plan simple-adw `(notify email ,kw ,value)))
        "compiled"))
    (check-regexp-match rx message)
    (check-false (string-contains? message value) (format "the message repeats ~s" value)))
  (rejected-as "ops@example.org" '#:to-env #rx"#:to-env takes the NAME of an environment variable.*looks like an e-mail address")
  (rejected-as "Ops <ops@example.org>" '#:from-env #rx"#:from-env takes the NAME.*looks like an e-mail address")
  (rejected-as "smtps://user:secret@mail.example.org:465" '#:smtp-url-env #rx"#:smtp-url-env takes the NAME.*looks like a URL")
  (rejected-as "smtp://127.0.0.1:1025" '#:smtp-url-env #rx"looks like a URL")
  (rejected-as "mail.example.org" '#:smtp-url-env #rx"looks like a host name")
  (rejected-as "mail.example.org:587" '#:smtp-url-env #rx"looks like a host name")
  (rejected-as "localhost" '#:smtp-url-env #rx"looks like a host name")
  (rejected-as "my variable" '#:to-env #rx"is not a variable name")
  (check-rejects (layer plan simple-adw '(notify email #:to-env "ops@example.org")) #rx"local.env")
  (check-true (hash? (compile-layer-datum (layer plan simple-adw '(notify email #:to-env "TEAM_2_MAIL"))))))

(test-case "notify: schema and reference"
  (define items (hash-ref (hash-ref (hash-ref (layer-schema) 'properties) 'notifiers) 'items))
  (check-equal? (sort (map (lambda (s) (hash-ref (hash-ref (hash-ref s 'properties) 'kind) 'const)) (hash-ref items 'oneOf)) string<?)
                '("command" "email" "none"))
  (define ref (reference-text))
  (for ([needle '("### `notify`" "(notify email …)" "(notify command …)" "(notify none …)" "`#:smtp-url-env`"
                  "Default when omitted: `(notify none)`" "?insecure=plaintext")])
    (check-true (string-contains? ref needle) needle)))

;; ---------------------------------------------------------------------------------------- script step

(test-case "script step: runs a declared cmd, needs a worktree, is not a check"
  (define ir (compile-layer-datum (layer plan '(adw a (steps (script legacy #:cmd lint #:args ("$prompt")))))))
  (check-equal? (car (hash-ref (car (hash-ref ir 'adws)) 'steps))
                (hasheq 'kind "script" 'name "legacy" 'cmd "lint" 'args '("$prompt") 'timeout 0
                        'waitingExit '() 'journal ""))
  (check-rejects (layer plan '(adw a (steps (script legacy #:cmd nothing)))) #rx"`nothing` is not a declared codebase cmd")
  (check-rejects (layer plan '(adw a (steps (script #:cmd lint)))) #rx"needs a step name")
  (check-rejects (layer plan '(environment host) '(adw a (steps (script legacy #:cmd lint))))
                 #rx"check, script and ship steps need a `worktree` environment")
  ;; a script step is no check: zero-touch still needs one
  (check-rejects (layer plan review-cmd '(environment worktree) '(environment podman) '(review auto #:require-checks (test))
                        '(adw a #:environment (worktree podman) (steps (run plan #:command plan) (script s #:cmd test) (review) (ship))))
                 #rx"needs a \\(check …\\) step before its \\(review\\) step"))

(test-case "script step: waiting exit codes and a journal"
  (define ir (compile-layer-datum
              (layer plan '(adw a (steps (script watch #:cmd lint #:args ("--adw-id" "$adw_id")
                                                 #:waiting-exit (75) #:journal ".work/adw/{adw_id}"))))))
  (check-equal? (car (hash-ref (car (hash-ref ir 'adws)) 'steps))
                (hasheq 'kind "script" 'name "watch" 'cmd "lint" 'args '("--adw-id" "$adw_id") 'timeout 0
                        'waitingExit '(75) 'journal ".work/adw/{adw_id}"))
  (define (script-with . kws) (layer plan `(adw a (steps (script watch #:cmd lint ,@kws)))))
  (check-rejects (script-with '#:waiting-exit '(0))
                 #rx"\\(adw a\\) step \\(script watch\\): #:waiting-exit codes must be between 1 and 255 \\(got 0\\)")
  (check-rejects (script-with '#:waiting-exit '(75 256)) #rx"#:waiting-exit codes must be between 1 and 255 \\(got 256\\)")
  (check-rejects (script-with '#:waiting-exit 75) #rx"#:waiting-exit: expected a parenthesised list")
  (for ([bad '("/tmp/x" "../x" "a/../../b")])
    (check-rejects (script-with '#:journal bad)
                   #rx"#:journal must be a path relative to the run's worktree without `..` or a leading `/`"))
  (check-rejects (script-with '#:journal ".work/{run}")
                 #rx"#:journal: unknown placeholder {run}; the only one is {adw_id}"))

;; ---------------------------------------------------------------------------------------- include

(test-case "include: clauses from other files, at the top level and inside (codebase …)"
  (define spec (dynamic-require (build-path fixtures "include-main.rkt") 'layer-spec))
  (check-equal? (map (lambda (c) (hash-ref c 'name)) (hash-ref (hash-ref spec 'codebase) 'commands)) '("test" "lint"))
  (check-equal? (hash-ref (car (hash-ref (hash-ref spec 'codebase) 'conditionalDocs)) 'path) "AGENTS.md")
  (check-equal? (map (lambda (s) (hash-ref s 'name)) (hash-ref (car (hash-ref spec 'adws)) 'steps)) '("plan" "lint"))
  (check-rejects (layer plan simple-adw '(include "x.rkt")) #rx"resolved by `#lang al`"))

(define (load-error file)
  (with-handlers ([exn:fail:syntax? exn-message])
    (dynamic-require (build-path fixtures file) #f)
    "no error"))

(test-case "include: errors point into the included file"
  (define msg (load-error "include-bad-main.rkt"))
  (check-regexp-match #rx"include-bad-adw.rkt:3:0" msg)
  (check-regexp-match #rx"uses model tier `fast`" msg)
  (define msg2 (load-error "include-bad-cmd-main.rkt"))
  (check-regexp-match #rx"include-bad-cmd.rkt:3:0" msg2)
  (check-regexp-match #rx"\\(cmd typecheck\\) #:timeout: expected an integer" msg2))

(test-case "codebase sub-clause errors point at the sub-clause; include does not nest"
  (define dir (make-temporary-file "al-inc-~a" 'directory))
  (define (write-file name text) (call-with-output-file (build-path dir name) (lambda (o) (write-string text o))))
  (write-file "agentic.rkt" "#lang al\n(agentic-layer #:name \"x\"\n  (codebase #:root \".\" #:languages (c)\n    (cmd test \"make\")\n    (cmd test \"make check\"))\n  (command plan #:template plan-chore)\n  (adw a (steps (run plan #:command plan))))\n")
  (write-file "nested.rkt" "#lang al\n(agentic-layer #:name \"y\"\n  (codebase #:root \".\" #:languages (c) (cmd test \"make\"))\n  (include \"one.rkt\"))\n")
  (write-file "one.rkt" "(include \"two.rkt\")\n")
  (write-file "missing.rkt" "#lang al\n(agentic-layer #:name \"z\"\n  (codebase #:root \".\" #:languages (c) (cmd test \"make\"))\n  (include \"nope.rkt\"))\n")
  (define (err f) (with-handlers ([exn:fail:syntax? exn-message]) (dynamic-require (build-path dir f) #f) "no error"))
  (check-regexp-match #rx"agentic.rkt:5:4.*\\(cmd test\\): declared twice" (err "agentic.rkt"))
  (check-regexp-match #rx"one.rkt:1:0.*cannot include another file" (err "nested.rkt"))
  (check-regexp-match #rx"no such file" (err "missing.rkt"))
  (delete-directory/files dir))

(test-case "schema and reference: tiers, budget, script, include"
  (define schema (layer-schema))
  (define agent-alts (hash-ref (hash-ref (hash-ref (hash-ref schema 'properties) 'agents) 'items) 'oneOf))
  (define cc (findf (lambda (s) (equal? (hash-ref (hash-ref (hash-ref s 'properties) 'kind) 'const) "claude-code")) agent-alts))
  (check-true (hash-has-key? (hash-ref cc 'properties) 'models))
  (check-false (hash-has-key? (hash-ref cc 'properties) 'modelBase))
  (check-equal? (hash-ref (hash-ref (hash-ref (hash-ref (hash-ref cc 'properties) 'models) 'items) 'properties) 'tier)
                (hasheq 'type "string" 'pattern "^[a-z][a-z0-9-]*$" 'description "Tier name: lowercase letters, digits and dashes (not `inherit`)."))
  (define budget (hash-ref (hash-ref schema 'properties) 'budget))
  (check-equal? (hash-ref (car (hash-ref budget 'oneOf)) 'type) "null")
  (check-equal? (hash-ref (hash-ref (hash-ref (cadr (hash-ref budget 'oneOf)) 'properties) 'runUsd) 'type) '("number" "null"))
  (define ref (reference-text))
  (for ([needle '("### `model-tier`" "### `step-script`" "(budget limits …)" "shorthand for the `heavy` entry of #:models"
                  "(include \"FILE\")" "claude-fable-5-2" "#:waiting-exit" "#:journal")])
    (check-true (string-contains? ref needle) needle)))

;; ---------------------------------------------------------------------------------------
;; Delivery: stages, cycles, presets
;; ---------------------------------------------------------------------------------------

(define-runtime-path golden "golden")

(define (preset-expansion-of name)
  (hash-ref (hash-ref (compile-layer-datum (layer plan simple-adw (list 'delivery name))) 'delivery) 'expansion))

(define (golden-lines name)
  (filter (lambda (l) (not (string=? l "")))
          (string-split (file->string (build-path golden (format "delivery-~a.rktd" name))) "\n")))

(define zte-parts
  (list plan review-cmd
        '(agent claude-code #:permission-mode bypassPermissions)
        '(environment worktree) '(environment podman #:agent-inside #t)
        '(review auto #:require-checks (test lint))))
(define (zte-adw . kws)
  `(adw zte #:environment (worktree podman) ,@kws
        (steps (run plan #:command plan) (check t #:cmd test) (review) (ship))))
(define (stages-of ir) (hash-ref ir 'stages))
(define (stage-named ir name) (findf (lambda (s) (equal? (hash-ref s 'name) name)) (stages-of ir)))

(define keys-before-delivery
  '(agents artefacts budget codebase commands concurrency dashboard description environments experts forge
    hops layer llmTxt name notifiers promptInputs reviews schemaVersion services storage triggers voice adws))

(test-case "delivery: no stage declared = the IR of before"
  (for ([ir (list (compile-layer-datum (layer plan simple-adw))
                  (compile-layer-datum (apply layer (append zte-parts (list (zte-adw))))))])
    (for ([k '(stages cycle delivery)]) (check-false (hash-has-key? ir k) (format "~a" k)))
    (for ([a (hash-ref ir 'adws)]) (check-false (hash-has-key? a 'deliverTo)))
    (check-equal? (sort (map symbol->string (hash-keys ir)) string<?)
                  (sort (map symbol->string keys-before-delivery) string<?))))

(test-case "stage: compiles with its defaults"
  (define ir (compile-layer-datum (layer plan simple-adw '(stage main #:branch "main"))))
  (define s (car (stages-of ir)))
  (check-equal? (hash-ref s 'from) "")
  (check-equal? (hash-ref s 'into) "")
  (check-equal? (hash-ref s 'accepts) '("chore" "bug" "feature" "patch"))
  (check-equal? (hash-ref s 'agentsMayMerge) #t)
  (check-equal? (hash-ref s 'entry) "merge")
  (check-equal? (hash-ref s 'checks) '())
  (check-equal? (hash-ref s 'origin) "declared")
  (check-equal? (hash-ref ir 'cycle) 'null)
  (check-equal? (hash-ref ir 'delivery) 'null)
  (check-equal? (hash-ref (car (hash-ref ir 'adws)) 'deliverTo) ""))

(test-case "stage graph"
  (check-rejects (layer plan simple-adw '(stage main #:branch "main") '(stage other #:branch "other"))
                 #rx"two final stages")
  (check-rejects (layer plan simple-adw '(stage a #:branch "a" #:from b) '(stage b #:branch "b" #:from a))
                 #rx"stages a → b → a form a cycle")
  (check-rejects (layer plan simple-adw '(stage a #:branch "a" #:from nowhere) '(stage main #:branch "main"))
                 #rx"`nowhere` is not a declared stage; declared: a, main")
  (check-rejects (layer plan simple-adw '(stage trunk #:branch "trunk"))
                 #rx"must equal \\(codebase #:default-branch …\\) \"main\"")
  (check-rejects (layer plan simple-adw '(stage a #:branch "main" #:from main) '(stage main #:branch "main"))
                 #rx"branch \"main\" is used by two stages")
  (check-rejects (layer plan simple-adw '(stage main #:branch "main") '(stage main #:branch "main"))
                 #rx"\\(stage main\\): declared twice"))

(test-case "stage branch patterns"
  (define (with-stage . fields) (layer plan simple-adw `(stage a ,@fields #:from main) '(stage main #:branch "main")))
  (check-rejects (with-stage '#:branch "sprint/{sprint}") #rx"unknown placeholder \\{sprint\\}")
  (check-rejects (with-stage '#:branch "a..b") #rx"not a valid git branch name")
  (check-rejects (with-stage '#:branch "x y") #rx"not a valid git branch name")
  (check-rejects (with-stage '#:branch "/main") #rx"not a valid git branch name")
  (check-rejects (with-stage '#:branch "x.lock") #rx"not a valid git branch name")
  (check-rejects (with-stage '#:branch "{cycle}") #rx"\\{cycle\\} .*only on the stage of \\(cycle …\\)")
  (check-not-exn (lambda () (compile-layer-datum
                             (layer plan simple-adw '(cycle release #:stage rel)
                                    '(stage rel #:branch "release/{version}" #:from main) '(stage main #:branch "main")))))
  (check-not-exn (lambda () (compile-layer-datum (with-stage '#:branch "hotfix/{date}"))))
  (check-rejects (layer plan simple-adw '(stage main #:branch "{date}")) #rx"final stage.*placeholder"))

(test-case "cycle rules"
  (define (sprint-layer . cycle)
    (layer plan simple-adw cycle '(stage sprint #:branch "sprint/{cycle}" #:from main) '(stage main #:branch "main")))
  (check-rejects (layer plan simple-adw '(cycle sprint #:stage sprint)) #rx"declare the stage it repeats")
  (check-rejects (sprint-layer 'cycle 'sprint '#:stage 'main) #rx"is the final stage")
  (check-rejects (sprint-layer 'cycle 'sprint '#:stage 'sprint '#:length-days 0) #rx"#:length-days must be 1–366")
  (check-rejects (sprint-layer 'cycle 'sprint '#:stage 'sprint '#:length-days 400) #rx"#:length-days must be 1–366")
  (check-rejects (sprint-layer 'cycle 'sprint '#:stage 'sprint '#:name "{month}") #rx"#:name \"\\{month\\}\": unknown placeholder \\{month\\}")
  (check-rejects (sprint-layer 'cycle 'release '#:stage 'sprint '#:name "{n}") #rx"must contain \\{version\\}")
  (check-rejects (layer plan simple-adw '(cycle sprint #:stage sprint)
                        '(stage sprint #:branch "sprint/x" #:from main) '(stage main #:branch "main"))
                 #rx"must contain \\{cycle\\}")
  (check-rejects (layer plan simple-adw '(cycle sprint #:stage sprint) '(cycle release #:stage sprint)
                        '(stage sprint #:branch "sprint/{cycle}" #:from main) '(stage main #:branch "main"))
                 #rx"only one cycle"))

(test-case "deliver-to and class routing"
  (check-rejects (layer plan '(adw a #:deliver-to main (steps (run plan #:command plan))))
                 #rx"#:deliver-to needs declared stages")
  (check-rejects (layer plan '(stage main #:branch "main") '(adw a #:deliver-to elsewhere (steps (run plan #:command plan))))
                 #rx"`elsewhere` is not a declared stage; declared: main")
  (check-rejects (layer plan '(stage hot #:branch "hot" #:from main #:accepts (bug)) '(stage main #:branch "main")
                        '(adw a #:deliver-to hot #:classes (bug feature) (steps (run plan #:command plan) (review) (ship))))
                 #rx"stage `hot` does not accept class feature")
  (check-not-exn
   (lambda ()
     (compile-layer-datum
      (layer plan '(stage a #:branch "a" #:from main #:accepts (bug)) '(stage main #:branch "main")
             '(adw f #:classes (feature) (steps (run plan #:command plan))))))))

(test-case "zero-touch may only deliver where agents may merge"
  (check-not-exn (lambda () (compile-layer-datum (apply layer (append zte-parts (list '(delivery sprint) (zte-adw)))))))
  (check-rejects (apply layer (append zte-parts (list '(delivery sprint) (zte-adw '#:deliver-to 'main))))
                 #rx"\\(adw zte\\): review `auto` ships without a human, but class [a-z]+ is delivered to stage `main` \\(branch main\\), where agents may not merge; deliver to a stage with #:agents-may-merge #t \\(e.g. #:deliver-to sprint\\) or use a human or agent review")
  (check-not-exn
   (lambda ()
     (compile-layer-datum
      (layer plan review-cmd '(delivery sprint)
             '(adw h #:review human #:deliver-to main
                   (steps (run plan #:command plan) (check t #:cmd test) (review)))))))
  ;; with a (ship) step the stage must also accept the ADW's classes (main in the sprint preset accepts none)
  (check-rejects (layer plan review-cmd '(delivery sprint)
                        '(adw h #:review human #:deliver-to main
                              (steps (run plan #:command plan) (check t #:cmd test) (review) (ship))))
                 #rx"stage `main` does not accept class"))

(test-case "entry methods the forge forbids"
  (check-rejects (layer plan simple-adw '(forge github #:repo "o/r") '(stage main #:branch "main" #:entry fast-forward))
                 #rx"\\(stage main\\): #:entry fast-forward is not possible with \\(forge github")
  (define forgejo '(forge forgejo #:host "https://forgejo.example.org" #:repo "o/r"))
  (check-rejects (layer plan simple-adw forgejo '(stage main #:branch "main" #:entry fast-forward))
                 #rx"\\(stage main\\): #:entry fast-forward is not possible with \\(forge forgejo")
  (check-rejects (layer plan simple-adw forgejo '(stage main #:branch "main")
                        '(stage fast #:branch "fast" #:from main #:entry fast-forward))
                 #rx"\\(stage fast\\): #:entry fast-forward is not possible with \\(forge forgejo")
  (for ([method '(merge squash rebase)])
    (check-not-exn (lambda () (compile-layer-datum (layer plan simple-adw forgejo `(stage main #:branch "main" #:entry ,method))))
                   (format "~a" method)))
  (check-not-exn (lambda () (compile-layer-datum (layer plan simple-adw '(stage main #:branch "main" #:entry fast-forward))))))

(test-case "gitflow: the hotfix stage takes bugs"
  (define ir (compile-layer-datum
              (layer plan review-cmd '(delivery gitflow)
                     '(adw h #:review human #:classes (bug) #:deliver-to hotfix
                           (steps (run plan #:command plan) (check t #:cmd test) (review) (ship))))))
  (check-equal? (hash-ref (stage-named ir "hotfix") 'accepts) '("bug"))
  (check-equal? (hash-ref (car (hash-ref ir 'adws)) 'deliverTo) "hotfix"))

(test-case "deploy command from the stage"
  (define (deploy-layer main-stage)
    (append (list 'agentic-layer '#:name "t"
                  '(codebase #:root "." #:languages (python) (cmd test "pytest") (cmd lint "ruff check") (cmd deploy "./deploy.sh")))
            (list plan review-cmd '(agent claude-code) '(environment worktree) '(environment podman)
                  '(review auto #:require-checks (test) #:ship deploy)
                  main-stage (zte-adw))))
  (check-not-exn (lambda () (compile-layer-datum (deploy-layer '(stage main #:branch "main" #:deploy-cmd deploy)))))
  (check-rejects (deploy-layer '(stage main #:branch "main"))
                 #rx"#:ship deploy but no #:deploy-cmd, and stage `main` it delivers to has none")
  (check-rejects (layer plan review-cmd '(review auto #:require-checks (test) #:ship deploy)
                        '(adw a (steps (check t #:cmd test) (review) (ship))))
                 #rx"review `auto` has #:ship deploy but no #:deploy-cmd$"))

(test-case "delivery presets expand to their golden output"
  (for ([name '(trunk sprint gitflow release-train phase-gate)])
    (check-equal? (preset-expansion-of name) (golden-lines name) (format "~a" name))
    (check-not-exn (lambda () (compile-layer-datum (layer plan simple-adw (list 'delivery name)))))))

(test-case "delivery presets: parameters and overrides"
  (define ir (compile-layer-datum
              `(agentic-layer #:name "t"
                 (codebase #:root "." #:languages (python) #:default-branch "trunk" (cmd test "pytest"))
                 ,plan ,simple-adw (delivery sprint #:length-days 7 #:main "trunk"))))
  (check-equal? (hash-ref (hash-ref ir 'cycle) 'lengthDays) 7)
  (check-equal? (hash-ref (stage-named ir "main") 'branch) "trunk")
  (check-equal? (hash-ref (hash-ref ir 'delivery) 'preset) "sprint")
  (check-equal? (hash-ref (hash-ref (hash-ref ir 'delivery) 'params) 'lengthDays) 7)
  (define over (compile-layer-datum (layer plan simple-adw '(delivery sprint)
                                           '(stage sprint #:branch "iteration/{cycle}" #:from main)
                                           '(stage hotfix #:branch "hotfix/{date}" #:from main))))
  (check-equal? (map (lambda (s) (hash-ref s 'name)) (stages-of over)) '("sprint" "main" "hotfix"))
  (check-equal? (hash-ref (stage-named over "sprint") 'branch) "iteration/{cycle}")
  (check-equal? (hash-ref (stage-named over "sprint") 'origin) "declared")
  (check-equal? (hash-ref (stage-named over "main") 'origin) "preset sprint")
  (check-equal? (hash-ref (stage-named over "hotfix") 'origin) "declared")
  (check-rejects (layer plan simple-adw '(delivery waterfall))
                 #rx"unknown delivery preset waterfall; presets are: trunk, sprint, gitflow, release-train, phase-gate")
  (check-rejects (layer plan simple-adw '(delivery sprint) '(delivery trunk)) #rx"\\(delivery …\\) is declared twice")
  (check-rejects (layer plan simple-adw '(delivery sprint #:weeks 2)) #rx"unknown field #:weeks; fields are: #:main #:length-days")
  (check-rejects (layer plan simple-adw '(delivery sprint) '(cycle sprint #:stage sprint))
                 #rx"\\(delivery sprint\\) already declares a cycle")
  (define one (compile-layer-datum (layer plan simple-adw '(delivery phase-gate #:phases (a)))))
  (check-equal? (map (lambda (s) (list (hash-ref s 'name) (hash-ref s 'into))) (stages-of one))
                '(("a" "main") ("main" ""))))

(test-case "schema and reference: stage, cycle, delivery presets"
  (define schema (layer-schema))
  (define req (hash-ref schema 'required))
  (for ([k '("stages" "cycle" "delivery")])
    (check-false (member k req) k)
    (check-true (hash-has-key? (hash-ref schema 'properties) (string->symbol k)) k))
  (define adw (hash-ref (hash-ref (hash-ref schema 'properties) 'adws) 'items))
  (check-false (member "deliverTo" (hash-ref adw 'required)))
  (check-true (hash-has-key? (hash-ref adw 'properties) 'deliverTo))
  (define stage-props (hash-ref (hash-ref (hash-ref (hash-ref schema 'properties) 'stages) 'items) 'properties))
  (check-true (hash-has-key? stage-props 'name))
  (check-true (hash-has-key? stage-props 'origin))
  (define ref (reference-text))
  (for ([needle '("### `stage`" "(cycle sprint …)" "## Delivery presets" "(delivery PRESET")])
    (check-true (string-contains? ref needle) needle))
  (for ([name '(trunk sprint gitflow release-train phase-gate)])
    (check-true (string-contains? ref (format "(delivery ~a" name)) (format "~a" name))
    (check-true (string-contains? ref (car (golden-lines name))) (format "~a golden" name))))

(test-case "full.rkt: zte routes chore and bug to the sprint"
  (define full (dynamic-require (build-path examples "full.rkt") 'layer-spec))
  (define stages (hash-ref full 'stages))
  (define (route klass)
    (hash-ref (or (findf (lambda (s) (member klass (hash-ref s 'accepts))) stages) (last stages)) 'name))
  (define zte (findf (lambda (a) (equal? (hash-ref a 'name) "zte")) (hash-ref full 'adws)))
  (check-equal? (hash-ref zte 'deliverTo) "")
  (check-equal? (map route (hash-ref zte 'classes)) '("sprint" "sprint"))
  (define hotfix (findf (lambda (a) (equal? (hash-ref a 'name) "hotfix")) (hash-ref full 'adws)))
  (check-equal? (hash-ref hotfix 'deliverTo) "hotfix"))
