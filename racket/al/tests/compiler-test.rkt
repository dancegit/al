#lang racket/base
(require rackunit racket/list racket/string racket/runtime-path racket/file json
         "../private/compiler.rkt" "../private/schema.rkt" "../private/reference.rkt"
         "../private/meta.rkt" "../private/vocab.rkt" "../private/json-pretty.rkt"
         "../private/warnings.rkt")

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
    hops hosts layer llmTxt name notifiers promptInputs registry reviews schemaVersion services standIn storage triggers voice adws))

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
;; --- warnings: declarations that compile but have no effect ---------------------------------

(define (warnings . clauses) (layer-warnings (compile-layer-datum (apply layer plan clauses))))
(define (warned? ws rx) (for/or ([w (in-list ws)]) (regexp-match? rx w)))

(test-case "warning: an environment no workflow uses"
  (define ws (warnings '(environment worktree) '(environment worktree #:id spare)
                       '(environment home-manager #:packages ("git")) '(adw a #:environment (worktree) (steps (run plan #:command plan)))))
  (check-equal? ws '("environment `spare` is used by no workflow"))
  (check-equal? (warnings simple-adw) '()))   ; the implicit default worktree is every ADW's stack

(test-case "warning: credentials without an agent inside"
  (define (box inside)
    (warnings '(environment worktree) `(environment podman #:id box #:agent-inside ,inside #:credentials (claude))
              '(adw a #:environment (worktree box) (steps (run plan #:command plan)))))
  (check-equal? (box #f) '("#:credentials on (environment podman #:id box) has no effect without #:agent-inside #t"))
  (check-equal? (box #t) '()))

(test-case "warning: publish or services with scope command"
  (define ws (warnings '(environment worktree #:ports-per-run 1)
                       '(environment podman #:id box #:scope command #:publish (8000))
                       '(adw a #:environment (worktree box) (steps (run plan #:command plan)))))
  (check-equal? (length ws) 1)
  (check-regexp-match #rx"^#:publish on \\(environment podman #:id box\\) with #:scope command: each command gets a fresh container" (car ws))
  (check-equal? (warnings '(environment worktree #:ports-per-run 1)
                          '(environment podman #:id box #:publish (8000))
                          '(adw a #:environment (worktree box) (steps (run plan #:command plan))))
                '()))

(test-case "warning: nix dev shell packages the container lacks"
  (define (stack podman)
    (warnings '(environment worktree) '(environment nix-devshell #:packages ("python312" "nodejs_20" "uv" "ffmpeg"))
              podman
              '(adw a #:environment (worktree nix-devshell box) (steps (run plan #:command plan)))
              '(adw b #:environment (worktree nix-devshell box) (steps (run plan #:command plan)))))
  (check-equal? (stack '(environment podman #:id box #:packages ("git")))
                '("workflow a, b runs its commands in (environment podman #:id box), which does not provide what (environment nix-devshell #:id nix-devshell) declares: python312, nodejs_20, uv, ffmpeg"))
  ;; aliases (python ↔ python3, nodejs ↔ node), a setup line, and the base image all provide
  (check-equal? (stack '(environment podman #:id box #:base-image "docker.io/library/node:20" #:packages ("python3" "ffmpeg")
                                     #:setup ("pip install uv")))
                '()))

(test-case "warning: no nix-vs-container warning when the image is the repository's own"
  (check-equal? (warnings '(environment worktree) '(environment nix-devshell #:packages ("ffmpeg"))
                          '(environment podman #:id box #:containerfile "Containerfile")
                          '(adw a #:environment (worktree nix-devshell box) (steps (run plan #:command plan))))
                '()))

(test-case "no warnings for the shipped examples"
  (for ([f (in-list (directory-list examples))] #:when (regexp-match? #rx"[.]rkt$" (path->string f)))
    (check-equal? (layer-warnings (dynamic-require (build-path examples f) 'layer-spec)) '() (path->string f))))

(test-case "warnings never change the IR"
  (define with-conditions
    (layer plan '(environment worktree) '(environment worktree #:id spare)
           '(environment podman #:id box #:agent-inside #f #:credentials (claude))
           '(adw a #:environment (worktree box) (steps (run plan #:command plan)))))
  (define ir (compile-layer-datum with-conditions))
  (define before (json-pretty ir))
  (check-equal? (length (layer-warnings ir)) 2)
  (check-equal? (json-pretty ir) before)
  (check-equal? (json-pretty (compile-layer-datum with-conditions)) before))

;; ---------------------------------------------------------------------------------------
;; Promotion between stages
;; ---------------------------------------------------------------------------------------

(define (promotions-of ir) (hash-ref ir 'promotions))
(define (promotion-from ir name) (findf (lambda (p) (equal? (hash-ref p 'from) name)) (promotions-of ir)))
(define sprint-promotion-parts (list plan review-cmd '(delivery sprint)))
(define (promo-layer . clauses) (apply layer (append sprint-promotion-parts (list simple-adw) clauses)))
;; dev → qa → main, where agents may merge into qa
(define (dev-qa-layer . clauses)
  (apply layer plan simple-adw
         '(stage dev #:branch "dev" #:from main #:into qa)
         '(stage qa #:branch "qa" #:from main #:into main #:accepts () #:agents-may-merge #t)
         '(stage main #:branch "main" #:accepts () #:agents-may-merge #f)
         clauses))

(test-case "promotion: no stage = no promotions key"
  (define ir (compile-layer-datum (layer plan simple-adw)))
  (check-false (hash-has-key? ir 'promotions))
  (check-equal? (sort (map symbol->string (hash-keys ir)) string<?)
                (sort (map symbol->string keys-before-delivery) string<?)))

(test-case "promotion: every non-final stage has one, declared or default"
  (define ir (compile-layer-datum (layer plan simple-adw '(delivery sprint))))
  (check-equal? (length (promotions-of ir)) 1)
  (define p (car (promotions-of ir)))
  (check-equal? (hash-ref p 'from) "sprint")
  (check-equal? (hash-ref p 'to) "main")
  (check-equal? (hash-ref p 'approval) "human")
  (check-equal? (hash-ref p 'approvers) 1)
  (check-equal? (hash-ref p 'requires) '("checks" "acceptance"))
  (check-equal? (hash-ref p 'method) "merge")
  (check-equal? (hash-ref p 'version) 'null)
  (check-equal? (hash-ref p 'tag) "")
  (check-equal? (hash-ref p 'present) '("summary" "changes" "proof" "demo" "checks" "notes" "excluded"))
  (check-equal? (hash-ref p 'after) '())
  (check-equal? (hash-ref p 'backMerge) '())
  (check-equal? (hash-ref p 'adw) "a")
  (check-equal? (hash-ref p 'hotfix) #f)
  (check-equal? (hash-ref p 'origin) "default")
  (define gitflow (compile-layer-datum (layer plan simple-adw '(delivery gitflow))))
  (check-equal? (map (lambda (p) (hash-ref p 'from)) (promotions-of gitflow)) '("develop" "release" "hotfix"))
  (define hotfix (promotion-from gitflow "hotfix"))
  (check-equal? (hash-ref hotfix 'hotfix) #t)
  (check-equal? (hash-ref hotfix 'backMerge) '("develop"))
  (check-equal? (hash-ref (promotion-from gitflow "develop") 'hotfix) #f)
  (check-equal? (hash-ref (promotion-from gitflow "release") 'hotfix) #f))

(test-case "promotion: declared fields compile"
  (define ir (compile-layer-datum
              (layer plan review-cmd '(delivery gitflow)
                     '(adw other (steps (run plan #:command plan)))
                     '(adw h #:review human #:classes (bug) #:deliver-to hotfix
                           (steps (run plan #:command plan) (check t #:cmd test) (review) (ship)))
                     '(stage main #:branch "main" #:accepts () #:agents-may-merge #f
                             #:deploy-cmd test #:environment-name "production")
                     '(promotion hotfix -> main #:approvers 2 #:version (bump minor) #:after (deploy production)
                                 #:back-merge (develop)))))
  (define p (promotion-from ir "hotfix"))
  (check-equal? (hash-ref p 'version) (hasheq 'how "bump" 'level "minor"))
  (check-equal? (hash-ref p 'after) '("deploy" "production"))
  (check-equal? (hash-ref p 'backMerge) '("develop"))
  (check-equal? (hash-ref p 'adw) "h")
  (check-equal? (hash-ref p 'approvers) 2)
  (check-equal? (hash-ref p 'origin) "declared")
  (check-equal? (hash-ref (promotion-from ir "develop") 'adw) "other")
  (define keep (compile-layer-datum (promo-layer '(promotion sprint -> main #:version (keep) #:adw a))))
  (check-equal? (hash-ref (promotion-from keep "sprint") 'version) (hasheq 'how "keep" 'level "patch"))
  (check-equal? (hash-ref (promotion-from keep "sprint") 'adw) "a"))

(test-case "promotion: shape and stages"
  (check-rejects (promo-layer '(promotion sprint main))
                 #rx"\\(promotion …\\): expected \\(promotion FROM -> TO #:approval human …\\)")
  (check-rejects (promo-layer '(promotion sprint -> nowhere)) #rx"`nowhere` is not a declared stage; declared: sprint, main")
  (check-rejects (dev-qa-layer '(promotion dev -> main))
                 #rx"\\(promotion dev -> main\\): stage dev goes into qa \\(its #:into, default #:from\\); promote it into qa")
  (check-rejects (promo-layer '(promotion sprint -> main) '(promotion sprint -> main #:approvers 2))
                 #rx"\\(promotion sprint -> …\\): declared twice; a stage has one promotion")
  (check-rejects (layer plan simple-adw '(promotion sprint -> main))
                 #rx"\\(promotion …\\) needs declared stages: add \\(stage …\\) clauses or a \\(delivery PRESET …\\) clause")
  (check-rejects (promo-layer '(promotion main -> sprint)) #rx"stage main is the final stage"))

(test-case "promotion: agents may not approve into a protected stage"
  (check-rejects (promo-layer '(promotion sprint -> main #:approval agent))
                 #rx"\\(promotion sprint -> main\\): #:approval agent lets agents approve, but agents may not merge into stage main \\(#:agents-may-merge #f\\); use #:approval human")
  (check-rejects (promo-layer '(promotion sprint -> main #:approval auto))
                 #rx"\\(promotion sprint -> main\\): #:approval auto .*agents may not merge into stage main \\(#:agents-may-merge #f\\); use #:approval human")
  (check-not-exn (lambda () (compile-layer-datum (dev-qa-layer '(promotion dev -> qa #:approval agent #:approvers 2)))))
  (check-rejects (promo-layer '(promotion sprint -> main #:approvers 0)) #rx"#:approvers must be 1–9 \\(got 0\\)")
  (check-rejects (promo-layer '(promotion sprint -> main #:approvers 10)) #rx"#:approvers must be 1–9 \\(got 10\\)"))

(test-case "promotion: hotfix gate"
  (check-rejects (layer plan simple-adw '(delivery gitflow) '(promotion hotfix -> main #:approval auto))
                 #rx"\\(promotion hotfix -> main\\): a hotfix promotion goes into the final stage under its own gate; #:approval auto is not allowed")
  (define ir (compile-layer-datum (layer plan simple-adw '(delivery gitflow)
                                         '(promotion hotfix -> main #:approval human #:approvers 2))))
  (check-equal? (hash-ref (promotion-from ir "hotfix") 'approvers) 2)
  (check-equal? (hash-ref (promotion-from ir "hotfix") 'backMerge) '("develop")))

(test-case "promotion: tag, version, after, back-merge"
  (check-rejects (promo-layer '(promotion sprint -> main #:tag "v{version}"))
                 #rx"\\(promotion sprint -> main\\): #:tag \"v\\{version\\}\" needs #:version \\(bump …\\) or \\(keep\\)")
  (check-rejects (promo-layer '(promotion sprint -> main #:tag "v{month}")) #rx"#:tag \"v\\{month\\}\": unknown placeholder \\{month\\}")
  (check-rejects (promo-layer '(promotion sprint -> main #:version (bump) #:tag "v {version}")) #rx"not a valid tag name")
  (check-rejects (dev-qa-layer '(promotion dev -> qa #:tag "dev-{cycle}")) #rx"\\{cycle\\} .*\\(cycle …\\) #:stage")
  (check-not-exn (lambda () (compile-layer-datum (promo-layer '(promotion sprint -> main #:tag "sprint-{cycle}-{date}")))))
  (check-rejects (promo-layer '(promotion sprint -> main #:after (deploy))) #rx"#:after \\(deploy\\) needs #:deploy-cmd on stage main")
  (check-rejects (layer plan simple-adw '(delivery sprint)
                        '(stage main #:branch "main" #:accepts () #:agents-may-merge #f #:deploy-cmd test #:environment-name "production")
                        '(promotion sprint -> main #:after (deploy staging)))
                 #rx"#:after \\(deploy staging\\): stage main deploys to production \\(its #:environment-name\\)")
  (check-not-exn (lambda () (compile-layer-datum
                             (layer plan simple-adw '(delivery sprint)
                                    '(stage main #:branch "main" #:accepts () #:agents-may-merge #f #:deploy-cmd test #:environment-name "production")
                                    '(promotion sprint -> main #:after (deploy production))))))
  (check-rejects (promo-layer '(promotion sprint -> main #:after (restart))) #rx"#:after \\(restart\\): expected \\(deploy\\) or \\(deploy ENV\\)")
  (check-rejects (promo-layer '(promotion sprint -> main #:back-merge (sprint)))
                 #rx"#:back-merge names stage sprint, which is FROM or TO"))

(test-case "stage: frozen periods"
  (define ir (compile-layer-datum (layer plan simple-adw '(delivery sprint)
                                         '(stage sprint #:branch "sprint/{cycle}" #:from main #:frozen ("2026-12-21..2027-01-03")))))
  (check-equal? (hash-ref (stage-named ir "sprint") 'frozen) '("2026-12-21..2027-01-03"))
  (check-equal? (hash-ref (stage-named ir "sprint") 'frozenWhilePromoting) #f)
  (check-equal? (hash-ref (stage-named ir "main") 'frozen) '())
  (for ([bad '("2026-12-21" "2027-01-03..2026-12-21" "2026-02-30..2026-03-01")])
    (check-rejects (layer plan simple-adw '(delivery sprint) `(stage sprint #:branch "sprint/{cycle}" #:from main #:frozen (,bad)))
                   (regexp (regexp-quote (format "(stage sprint): #:frozen ~s: expected \"YYYY-MM-DD..YYYY-MM-DD\" with the first day not after the second" bad)))))
  (check-not-exn (lambda () (compile-layer-datum (layer plan simple-adw '(delivery sprint)
                                                        '(stage sprint #:branch "sprint/{cycle}" #:from main #:frozen ("2028-02-29..2028-02-29")))))))

(test-case "schema and reference: promotion"
  (define schema (layer-schema))
  (check-false (member "promotions" (hash-ref schema 'required)))
  (define items (hash-ref (hash-ref (hash-ref schema 'properties) 'promotions) 'items))
  (for ([k '(from to approval approvers requires method version tag present after backMerge adw hotfix origin)])
    (check-true (hash-has-key? (hash-ref items 'properties) k) (format "~a" k)))
  (check-equal? (hash-ref (hash-ref (hash-ref items 'properties) 'version) 'type) '("object" "null"))
  (define stage-props (hash-ref (hash-ref (hash-ref (hash-ref schema 'properties) 'stages) 'items) 'properties))
  (check-true (hash-has-key? stage-props 'frozen))
  (check-true (hash-has-key? stage-props 'frozenWhilePromoting))
  (define ref (reference-text))
  (for ([needle '("### `promotion`" "(promotion FROM -> TO" "### `version-change`")])
    (check-true (string-contains? ref needle) needle))
  (check-equal? promotion-placeholders '("{version}" "{cycle}" "{date}")))

(test-case "full.rkt: promotions"
  (define full (dynamic-require (build-path examples "full.rkt") 'layer-spec))
  (define (promo from) (findf (lambda (p) (equal? (hash-ref p 'from) from)) (hash-ref full 'promotions)))
  (check-equal? (hash-ref (promo "sprint") 'tag) "v{version}")
  (check-equal? (hash-ref (promo "sprint") 'version) (hasheq 'how "bump" 'level "minor"))
  (check-equal? (hash-ref (promo "hotfix") 'approvers) 2)
  (check-equal? (hash-ref (promo "hotfix") 'backMerge) '("sprint"))
  (check-equal? (hash-ref (promo "hotfix") 'hotfix) #t))
(test-case "#:may-commit says which commands may commit; it defaults to #f and read-only templates refuse it"
  (define (may-commit-of cmd-datum)
    (define ir (compile-layer-datum (layer plan cmd-datum simple-adw)))
    (define name (cadr cmd-datum))
    (hash-ref (findf (lambda (c) (equal? (hash-ref c 'name) (symbol->string name))) (hash-ref ir 'commands))
              'mayCommit))
  (check-equal? (may-commit-of '(command commit #:template commit)) #f)
  (check-equal? (may-commit-of '(command c #:template custom #:file "x.md" #:may-commit #t)) #t)
  (check-rejects (layer plan '(command r #:template review #:output json #:may-commit #t) simple-adw)
                 #rx"#:may-commit #t on template review"))
;; ---------------------------------------------------------------------------------- host, registry

(define host-base
  (list '(layer in-repo) '(environment worktree) '(environment podman) '(trigger cron) plan simple-adw))
(define (host-layer . clauses) (apply in-repo-layer ".." (append host-base clauses)))

(test-case "host and registry compile to hosts[] and registry"
  (define ir (compile-layer-datum
              (host-layer '(registry #:address-env "AL_REGISTRY")
                          '(host #:id server #:provision home-manager #:units (cron dashboard) #:dashboard-port 8770
                                 #:linger #t #:images pull))))
  (define h (car (hash-ref ir 'hosts)))
  (check-equal? (hash-ref h 'id) "server")
  (check-equal? (hash-ref h 'provision) "home-manager")
  (check-equal? (hash-ref h 'units) '("cron" "dashboard"))
  (check-equal? (hash-ref h 'dashboardPort) 8770)
  (check-equal? (hash-ref h 'linger) #t)
  (check-equal? (hash-ref h 'images) "pull")
  (check-equal? (hash-ref h 'fallbackBuild) #t)
  (define r (hash-ref ir 'registry))
  (check-equal? (hash-ref r 'addressEnv) "AL_REGISTRY")
  (check-equal? (hash-ref r 'usernameEnv) "")
  (check-equal? (hash-ref r 'passwordEnv) "")
  (check-equal? (hash-ref r 'tlsVerify) #t)
  (define plain (compile-layer-datum (apply in-repo-layer ".." host-base)))
  (check-equal? (hash-ref plain 'hosts) '())
  (check-equal? (hash-ref plain 'registry) 'null)
  (define defaults (car (hash-ref (compile-layer-datum (host-layer '(host))) 'hosts)))
  (check-equal? (hash-ref defaults 'id) "host")
  (check-equal? (hash-ref defaults 'provision) "none")
  (check-equal? (hash-ref defaults 'images) "build"))

(test-case "host rules"
  (check-rejects (host-layer '(host #:id server) '(host #:id server)) #rx"id server already used")
  (check-rejects (host-layer '(host #:id "srv.example.org")) #rx"host name")
  (check-rejects (host-layer '(host #:id server #:units (webhook))) #rx"#:units: `webhook` is not declared")
  (check-rejects (host-layer '(host #:id server #:dashboard-port 70000)) #rx"#:dashboard-port must be 0 or 1–65535")
  (check-rejects (host-layer '(host #:id server #:units (cron) #:dashboard-port 8770)) #rx"needs the dashboard unit")
  (check-rejects (host-layer '(host #:id server #:images pull)) #rx"#:images pull needs a \\(registry")
  (check-rejects (in-repo-layer ".." '(layer in-repo) '(trigger cron) plan simple-adw '(host #:id server #:images load))
                 #rx"#:images load needs a podman environment")
  (check-rejects `(agentic-layer #:name "t" ,base-codebase (layer sibling) ,plan ,simple-adw (host #:id server))
                 #rx"needs \\(layer in-repo")
  (check-rejects (host-layer '(host server)) #rx"\\(host …\\): unexpected server; fields are given as #:keyword value")
  (check-rejects (host-layer '(host #:id server #:ram 4)) #rx"unknown field #:ram"))

(test-case "registry rules"
  (check-rejects (host-layer '(registry #:address-env "registry.example.org")) #rx"looks like a host name")
  (check-rejects (host-layer '(registry #:username-env "REG_USER")) #rx"#:username-env and #:password-env")
  (check-rejects (host-layer '(registry) '(registry)) #rx"only one \\(registry"))
;; --- checks that need a container engine: #:in, skip rules, #:nested-engine -------------------

(define (check-step ir adw name)
  (findf (lambda (s) (equal? (hash-ref s 'name) name))
         (hash-ref (findf (lambda (a) (equal? (hash-ref a 'name) adw)) (hash-ref ir 'adws)) 'steps)))
(define (in-layer in . envs)
  (apply layer plan `(adw a #:environment (worktree) (steps (run plan #:command plan) (check t #:cmd test #:in ,in)))
         '(environment worktree) envs))

(test-case "check #:in names a declared environment"
  (check-rejects (in-layer 'nowhere '(environment podman #:id box))
                 #rx"`nowhere` is not a declared environment; declared: "))

(test-case "check #:in must be podman or nix-devshell"
  (check-rejects (in-layer 'host '(environment host))
                 #rx"\\(adw a\\): check `t` #:in `host` is a `host` environment; a check runs in the run's worktree inside a podman or nix-devshell environment")
  (check-rejects (in-layer 'spare '(environment worktree #:id spare))
                 #rx"check `t` #:in `spare` is a `worktree` environment")
  (check-equal? (hash-ref (check-step (compile-layer-datum (in-layer 'box '(environment podman #:id box))) "a" "t") 'in) "box")
  (check-equal? (hash-ref (check-step (compile-layer-datum (in-layer 'nix-devshell '(environment nix-devshell #:packages ("go"))))
                                      "a" "t") 'in)
                "nix-devshell"))

(test-case "check #:max-skipped below -1 is rejected"
  (check-rejects (layer plan '(adw a (steps (check t #:cmd test #:max-skipped -2))))
                 #rx"\\(adw a\\): check `t` #:max-skipped must be -1 \\(no limit\\) or more, got -2")
  (check-equal? (hash-ref (check-step (compile-layer-datum (layer plan '(adw a (steps (check t #:cmd test #:max-skipped 0)))))
                                      "a" "t") 'maxSkipped)
                0))

(test-case "skip rules default to -1 and empty"
  (define st (check-step (compile-layer-datum (layer plan '(adw a (steps (check t #:cmd test))))) "a" "t"))
  (check-equal? (hash-ref st 'maxSkipped) -1)
  (check-equal? (hash-ref st 'forbidSkipReason) "")
  (check-equal? (hash-ref st 'in) ""))

(test-case "podman #:nested-engine defaults to #f and compiles when #t"
  (define (box . fields)
    (define ir (compile-layer-datum (layer plan '(environment worktree) `(environment podman #:id box ,@fields)
                                           '(adw a #:environment (worktree box) (steps (run plan #:command plan))))))
    (hash-ref (findf (lambda (o) (equal? (hash-ref o 'id) "box")) (hash-ref ir 'environments)) 'nestedEngine))
  (check-equal? (box) #f)
  (check-equal? (box '#:nested-engine #t) #t))

(define (zero-touch in . envs)
  (apply warnings review-cmd '(review auto #:ship merge #:require-checks (test)) '(environment worktree)
         '(environment podman #:id box)
         `(adw zte #:environment (worktree box)
               (steps (run plan #:command plan) (check t #:cmd test) (check engine #:cmd lint #:in ,in) (review) (ship)))
         envs))

(test-case "warning: a zero-touch check outside its sandbox"
  (check-equal? (zero-touch 'nix-devshell '(environment nix-devshell #:packages ("go")))
                '("workflow zte ships without a person, but check engine runs #:in nix-devshell (a nix-devshell environment), outside its podman sandbox"))
  (check-equal? (zero-touch 'engine '(environment podman #:id engine)) '()))

(test-case "warning: an environment used only by a check's #:in is used"
  (define ws (warnings '(environment worktree) '(environment podman #:id engine)
                       '(adw a #:environment (worktree) (steps (run plan #:command plan) (check t #:cmd test #:in engine)))))
  (check-false (warned? ws #rx"is used by no workflow"))
  (check-equal? ws '()))

(test-case "the nested-engine fixture compiles without warnings"
  (define spec (dynamic-require (build-path fixtures "nested-engine.rkt") 'layer-spec))
  (check-equal? (layer-warnings spec) '())
  (check-equal? (hash-ref (check-step spec "zte" "engine-test") 'forbidSkipReason) "no podman")
  (check-equal? (hash-ref (check-step spec "build" "engine-test") 'in) "engine")
  (check-true (for/and ([o (in-list (hash-ref spec 'environments))] #:when (equal? (hash-ref o 'kind) "podman"))
                (hash-ref o 'nestedEngine))))
;; --- toolchains ------------------------------------------------------------------------------

(define dev-toolchain '(toolchain nix #:id dev #:packages ("git") #:features (browser agent-cli)))
(define (toolchain-layer #:toolchain [tc dev-toolchain] #:podman [podman '(environment podman #:toolchain dev #:agent-inside #t)]
                         #:adw [adw '(adw a #:environment (worktree podman) (steps (run plan #:command plan)))]
                         . more)
  (apply layer plan tc '(environment worktree) podman adw more))

(test-case "toolchain family and references"
  (define ir (compile-layer-datum
              (layer plan dev-toolchain
                     '(environment worktree)
                     '(environment nix-devshell #:toolchain dev)
                     '(environment podman #:toolchain dev #:agent-inside #t)
                     '(environment home-manager #:toolchain dev)
                     '(adw a #:environment (worktree nix-devshell podman) (steps (run plan #:command plan))))))
  (define tcs (hash-ref ir 'toolchains))
  (check-equal? (length tcs) 1)
  (check-equal? (hash-ref (car tcs) 'id) "dev")
  (check-equal? (hash-ref (car tcs) 'agentClis) '("claude-code"))
  (check-equal? (hash-ref (car tcs) 'features) '("browser" "agent-cli"))
  (for ([e (in-list (hash-ref ir 'environments))] #:unless (equal? (hash-ref e 'kind) "worktree"))
    (check-equal? (hash-ref e 'toolchain) "dev" (hash-ref e 'kind))))

(test-case "toolchain errors"
  (check-rejects (toolchain-layer #:podman '(environment podman #:id sandbox #:toolchain dev #:packages ("git"))
                                  #:adw '(adw a #:environment (worktree sandbox) (steps (run plan #:command plan))))
                 #rx"\\(environment podman sandbox\\): give either #:toolchain or #:packages, not both")
  (check-rejects (toolchain-layer '(environment nix-devshell #:toolchain dev #:nixpkgs "github:NixOS/nixpkgs/nixos-unstable"))
                 #rx"nix-devshell.*#:nixpkgs")
  (check-rejects (toolchain-layer #:podman '(environment podman #:toolchain dev #:base-image "docker.io/library/debian:stable"))
                 #rx"podman.*#:base-image")
  (check-rejects (toolchain-layer #:podman '(environment podman #:id sandbox #:toolchain dev #:nested-engine #t)
                                  #:adw '(adw a #:environment (worktree sandbox) (steps (run plan #:command plan))))
                 #rx"\\(environment podman sandbox\\): #:nested-engine needs the #:packages form \\(a Debian base image\\); it cannot be combined with #:toolchain")
  (check-rejects (toolchain-layer '(environment nix-devshell)) #rx"nix-devshell.*give #:toolchain or #:packages")
  (check-rejects (toolchain-layer '(environment home-manager)) #rx"home-manager.*give #:toolchain or #:packages")
  (check-rejects (toolchain-layer #:podman '(environment podman #:toolchain nope))
                 #rx"`nope` is not a declared toolchain")
  (check-rejects (toolchain-layer #:toolchain '(toolchain nix #:id dev #:features (gpu)))
                 #rx"one of: browser, agent-cli")
  (check-rejects (toolchain-layer #:toolchain '(toolchain nix #:id dev))
                 #rx"at least one package .* or feature")
  (check-rejects (toolchain-layer #:toolchain '(toolchain nix #:id dev #:packages ("git") #:env (("1BAD" "x"))))
                 #rx"#:env.*`1BAD` is not a variable name")
  (check-rejects (toolchain-layer #:podman '(environment podman #:toolchain dev #:agent-inside #f))
                 #rx"agent-cli.*no workflow runs its agent inside")
  (check-rejects (toolchain-layer '(agent custom #:command "my-agent")) ; first declared agent: the ADW's
                 #rx"agent-cli.*custom"))

(test-case "warning: a toolchain's packages and features count for the container"
  (define (stack tc)
    (layer-warnings
     (compile-layer-datum
      (layer plan tc '(environment worktree) '(environment nix-devshell #:packages ("python312" "uv" "ffmpeg"))
             '(environment podman #:id box #:toolchain dev)
             '(adw a #:environment (worktree nix-devshell box) (steps (run plan #:command plan)))))))
  (check-equal? (stack '(toolchain nix #:id dev #:packages ("python312" "uv") #:features (video))) '())
  (check-equal? (stack '(toolchain nix #:id dev #:packages ("python312")))
                '("workflow a runs its commands in (environment podman #:id box), which does not provide what (environment nix-devshell #:id nix-devshell) declares: uv, ffmpeg")))

(test-case "no toolchain, no new keys"
  (define spec (dynamic-require (build-path examples "minimal.rkt") 'layer-spec))
  (define ir (compile-layer-datum
              (layer plan '(environment worktree) '(environment nix-devshell #:packages ("uv"))
                     '(environment podman #:packages ("git")) '(environment home-manager #:packages ("uv"))
                     '(adw a #:environment (worktree nix-devshell podman) (steps (run plan #:command plan))))))
  (for ([x (list spec ir)])
    (check-false (hash-has-key? x 'toolchains))
    (for ([e (in-list (hash-ref x 'environments))])
      (check-false (hash-has-key? e 'toolchain) (hash-ref e 'kind)))))

(test-case "every feature is documented"
  (define tc (find-kind (find-family 'toolchain) 'nix))
  (define features (findf (lambda (f) (eq? (field-name f) 'features)) (kind-fields tc)))
  (check-equal? (cdr (cadr (field-type features))) (map car toolchain-features))
  (define ref (reference-text))
  (check-true (string-contains? ref "## Toolchain features"))
  (for ([f (in-list toolchain-features)])
    (check-true (string-contains? ref (format "- `~a` — ~a" (car f) (cdr f))) (symbol->string (car f)))))

;; --- checks that say what they cover: #:covers, #:always, #:reuse, #:cmds affected ------------

(define (cmd-of ir name)
  (findf (lambda (c) (equal? (hash-ref c 'name) name)) (hash-ref (hash-ref ir 'codebase) 'commands)))
(define (covering-layer cmds . clauses)
  `(agentic-layer #:name "t" (codebase #:root "." #:languages (python) ,@cmds) ,plan ,@clauses))
(define two-suites
  '((cmd unit "pytest") (cmd suite-a "sh a.sh" #:covers ("a") #:always #f)
    (cmd suite-b "sh b.sh" #:covers ("b" "docs/**"))))
(define (step-names ir adw)
  (map (lambda (s) (hash-ref s 'name))
       (hash-ref (findf (lambda (a) (equal? (hash-ref a 'name) adw)) (hash-ref ir 'adws)) 'steps)))

(test-case "cmd coverage defaults: covers (), always #t, reuse #t"
  (define c (cmd-of (compile-layer-datum (layer plan simple-adw)) "test"))
  (check-equal? (hash-ref c 'covers) '())
  (check-equal? (hash-ref c 'always) #t)
  (check-equal? (hash-ref c 'reuse) #t))

(test-case "cmd #:covers, #:always #f and #:reuse #f compile to the IR"
  (define ir (compile-layer-datum
              (covering-layer '((cmd e2e "bun run e2e" #:covers ("web" "*.md") #:always #f #:reuse #f)) simple-adw)))
  (define c (cmd-of ir "e2e"))
  (check-equal? (hash-ref c 'covers) '("web" "*.md"))
  (check-equal? (hash-ref c 'always) #f)
  (check-equal? (hash-ref c 'reuse) #f))

(test-case "cmd #:always #f needs #:covers"
  (check-rejects (covering-layer '((cmd e2e "bun run e2e" #:always #f)) simple-adw)
                 #rx"\\(cmd e2e\\): #:always #f needs #:covers: without declared paths the command would never run"))

(test-case "cmd #:covers refuses .. and absolute paths"
  (for ([bad (in-list '("../other" "a/../../b" "/etc" ""))])
    (check-rejects (covering-layer `((cmd e2e "bun run e2e" #:covers (,bad))) simple-adw)
                   #rx"\\(cmd e2e\\): #:covers \".*\" must be a path or glob relative to the repository root, without `..` or a leading `/`")))

(test-case "check #:when-affected defaults to #f"
  (define ir (compile-layer-datum
              (layer plan '(adw a (steps (run plan #:command plan) (check t #:cmd test)
                                         (check l #:cmd lint #:when-affected #t))))))
  (check-equal? (hash-ref (check-step ir "a" "t") 'whenAffected) #f)
  (check-equal? (hash-ref (check-step ir "a" "l") 'whenAffected) #t))

(test-case "review auto #:require defaults to all"
  (define (require-of . kws)
    (define ir (compile-layer-datum
                (layer plan review-cmd `(review auto #:ship pr #:require-checks (test) ,@kws) simple-adw)))
    (hash-ref (car (hash-ref ir 'reviews)) 'require))
  (check-equal? (require-of) "all")
  (check-equal? (require-of '#:require 'affected) "affected")
  (check-rejects (layer plan review-cmd '(review auto #:ship pr #:require-checks (test) #:require some) simple-adw)
                 #rx"one of: all, affected"))

(test-case "check #:cmds affected expands to one step per cmd with #:covers"
  (define ir (compile-layer-datum
              (covering-layer two-suites '(command fix #:template resolve-test)
                              '(adw a (steps (run plan #:command plan)
                                             (check tests #:cmds affected #:resolve fix #:retries 1)
                                             (check unit #:cmd unit))))))
  ;; names and order follow the cmd declarations; `unit` declares no #:covers and is left out
  (check-equal? (step-names ir "a") '("plan" "suite-a" "suite-b" "unit"))
  (for ([name (in-list '("suite-a" "suite-b"))])
    (define st (check-step ir "a" name))
    (check-equal? (hash-ref st 'kind) "check")
    (check-equal? (hash-ref st 'cmd) name)
    (check-equal? (hash-ref st 'whenAffected) #t)
    (check-equal? (hash-ref st 'resolve) "fix")
    (check-equal? (hash-ref st 'retries) 1))
  (check-equal? (hash-ref (check-step ir "a" "unit") 'whenAffected) #f)
  ;; the IR holds ordinary check steps only: the same keys as a written one
  (check-equal? (sort (map symbol->string (hash-keys (check-step ir "a" "suite-a"))) string<?)
                (sort (map symbol->string (hash-keys (check-step ir "a" "unit"))) string<?))
  ;; through (compose …) too
  (define composed (compile-layer-datum
                    (covering-layer two-suites
                                    '(adw base (steps (run plan #:command plan) (check tests #:cmds affected)))
                                    '(adw b (steps (compose base))))))
  (check-equal? (step-names composed "b") '("plan" "suite-a" "suite-b")))

(test-case "check #:cmds affected: shape errors"
  (define (with-step step [cmds two-suites])
    (covering-layer cmds `(adw a (steps (run plan #:command plan) ,step))))
  (check-rejects (with-step '(check tests #:cmds all))
                 #rx"step \\(check tests\\): #:cmds takes `affected` \\(one check step per cmd that declares #:covers\\); name a single cmd with #:cmd")
  (check-rejects (with-step '(check tests #:cmds affected #:cmd unit))
                 #rx"#:cmds affected stands for the cmds that declare #:covers; remove #:cmd")
  (check-rejects (with-step '(check tests #:cmds affected #:when-affected #f))
                 #rx"#:cmds affected stands for the cmds that declare #:covers; remove #:when-affected")
  (check-rejects (with-step '(check tests #:cmds affected) '((cmd unit "pytest")))
                 #rx"step \\(check tests\\): #:cmds affected, but no codebase cmd declares #:covers")
  (check-rejects (covering-layer two-suites
                                 '(adw a (steps (run suite-a #:command plan) (check tests #:cmds affected))))
                 #rx"step name `suite-a` used twice"))

(test-case "warning: #:require affected with a ship policy that merges without a person"
  (define (ws ship . kws)
    (apply warnings review-cmd `(review auto #:id zt #:ship ,ship #:require-checks (test) ,@kws
                                        ,@(if (eq? ship 'deploy) '(#:deploy-cmd lint) '()))
           '(environment worktree) '(environment podman #:id box)
           '((adw zte #:environment (worktree box)
                  (steps (run plan #:command plan) (check t #:cmd test) (review) (ship))))))
  (define (text ship)
    (format "review `zt` has #:require affected and #:ship ~a: a required check no changed path touches is not run before the change ships without a person; a wrong #:covers then ships unproven code" ship))
  (check-equal? (ws 'merge '#:require 'affected) (list (text "merge")))
  (check-equal? (ws 'deploy '#:require 'affected) (list (text "deploy")))
  (check-equal? (ws 'pr '#:require 'affected) '())
  (check-equal? (ws 'merge '#:require 'all) '())
  (check-equal? (ws 'merge) '()))

(test-case "the affected-checks fixture compiles with exactly that warning"
  (define spec (dynamic-require (build-path fixtures "affected-checks.rkt") 'layer-spec))
  (check-equal? (layer-warnings spec)
                '("review `zero-touch` has #:require affected and #:ship merge: a required check no changed path touches is not run before the change ships without a person; a wrong #:covers then ships unproven code"))
  (for ([adw (in-list '("zte" "build"))])
    (check-equal? (filter (lambda (n) (member n '("suite-a" "suite-b" "docs" "flaky"))) (step-names spec adw))
                  '("suite-a" "suite-b" "docs" "flaky") adw)
    (check-true (hash-ref (check-step spec adw "docs") 'whenAffected)))
  (check-equal? (hash-ref (cmd-of spec "flaky") 'reuse) #f)
  (check-equal? (hash-ref (cmd-of spec "flaky") 'always) #t)
  (check-equal? (hash-ref (cmd-of spec "docs") 'covers) '("README.md" "docs")))

(test-case "an IR without the new clauses differs from before only by the defaulted keys"
  (define spec (dynamic-require (build-path examples "minimal.rkt") 'layer-spec))
  (define c (cmd-of spec "test"))
  (check-equal? (sort (map symbol->string (hash-keys c)) string<?)
                '("always" "covers" "description" "name" "reuse" "run" "timeout"))
  (check-equal? (list (hash-ref c 'covers) (hash-ref c 'always) (hash-ref c 'reuse)) '(() #t #t))
  (check-equal? (step-names spec "plan-build") '("plan" "build" "test"))
  (define st (check-step spec "plan-build" "test"))
  (check-equal? (hash-ref st 'whenAffected) #f)
  (check-equal? (hash-remove st 'whenAffected)
                (hasheq 'kind "check" 'name "test" 'cmd "test" 'resolve "" 'retries 3 'collect '()
                        'in "" 'maxSkipped -1 'forbidSkipReason "")))

(test-case "environment sources"
  ;; a minimal layer with the given environment options, used by one ADW with a check step
  (define (layer-with #:cmds [cmds '()] #:steps [steps '((check t #:cmd test))] . options)
    (define ids
      (for/list ([o (in-list options)] #:when (eq? (car o) 'environment))
        (define p (memq '#:id o))
        (if p (cadr p) (cadr o))))
    `(agentic-layer #:name "t"
       (codebase #:root "." #:languages (python) (cmd test "pytest") ,@cmds)
       ,@(if (memq 'worktree (map cadr options)) '() '((environment worktree)))
       ,@options
       (adw a #:environment ,(remove-duplicates (cons 'worktree ids)) (steps ,@steps))))
  (define (env-of ir kind)
    (findf (lambda (o) (equal? (hash-ref o 'kind) kind)) (hash-ref ir 'environments)))
  ;; accepted forms
  (define flake (compile-layer-datum (layer-with '(environment nix-devshell #:flake "."))))
  (check-equal? (hash-ref (env-of flake "nix-devshell") 'flake) ".")
  (check-equal? (hash-ref (env-of flake "nix-devshell") 'shell) "default")
  (check-equal? (hash-ref (env-of flake "nix-devshell") 'packages) '())
  (define extras (compile-layer-datum (layer-with '(environment nix-devshell #:flake "nix" #:shell "ci" #:packages ("jq") #:browser #t))))
  (check-equal? (hash-ref (env-of extras "nix-devshell") 'shell) "ci")
  (check-equal? (hash-ref (env-of extras "nix-devshell") 'browser) #t)
  (define cf (compile-layer-datum (layer-with '(environment podman #:containerfile "Containerfile" #:target "dev" #:context "."))))
  (check-equal? (hash-ref (env-of cf "podman") 'containerfile) "Containerfile")
  (check-equal? (hash-ref (env-of cf "podman") 'target) "dev")
  (define dc (compile-layer-datum (layer-with '(environment podman #:devcontainer ".devcontainer/devcontainer.json" #:browser #t))))
  (check-equal? (hash-ref (env-of dc "podman") 'devcontainer) ".devcontainer/devcontainer.json")
  (check-equal? (hash-ref (env-of dc "podman") 'context) ".")
  (define prep (compile-layer-datum (layer-with #:cmds '((cmd install "uv sync")) '(environment worktree #:prepare (install)))))
  (check-equal? (hash-ref (env-of prep "worktree") 'prepare) '("install"))
  (check-equal? (hash-ref (env-of cf "worktree") 'prepare) '())
  ;; rejected forms
  (check-rejects (layer-with '(environment nix-devshell)) #rx"needs #:packages .* or #:flake")
  (check-rejects (layer-with '(environment nix-devshell #:packages ("jq") #:shell "ci")) #rx"#:shell needs #:flake")
  (check-rejects (layer-with '(environment nix-devshell #:flake "." #:shell "a b")) #rx"#:shell must be the name of a dev shell")
  (check-rejects (layer-with '(environment podman #:base-image "x" #:containerfile "C"))
                 #rx"at most one of #:base-image, #:containerfile, #:devcontainer")
  (check-rejects (layer-with '(environment podman #:containerfile "C" #:devcontainer "d.json"))
                 #rx"at most one of #:base-image, #:containerfile, #:devcontainer")
  (check-rejects (layer-with '(environment podman #:target "dev")) #rx"#:target needs #:containerfile")
  (check-rejects (layer-with '(environment podman #:context ".")) #rx"#:context needs #:containerfile")
  (check-rejects (layer-with '(environment podman #:containerfile "/abs/C")) #rx"relative to the repository root")
  (check-rejects (layer-with '(environment podman #:containerfile "../C")) #rx"relative to the repository root")
  (check-rejects (layer-with '(environment podman #:devcontainer "")) #rx"relative to the repository root")
  (check-rejects (layer-with '(environment nix-devshell #:flake "../x")) #rx"relative to the repository root")
  (check-rejects (layer-with '(environment podman #:containerfile "C")
                             '(environment podman #:id second #:containerfile "D"))
                 #rx"only the first declared podman option")
  (check-rejects (layer-with '(environment nix-devshell #:flake ".")
                             '(environment nix-devshell #:id other #:flake "sub"))
                 #rx"only the first declared nix-devshell option")
  (check-rejects (layer-with '(environment worktree #:prepare (nope)))
                 #rx"`nope` is not a declared codebase cmd; declared: test")
  (check-rejects (layer-with #:cmds '((cmd install "uv sync"))
                             #:steps '((check prepare #:cmd test))
                             '(environment worktree #:prepare (install)))
                 #rx"step name `prepare` is reserved")
  ;; without #:prepare the name is free
  (check-not-exn (lambda () (compile-layer-datum (layer-with #:steps '((check prepare #:cmd test)))))))

;; ---------------------------------------------------------------------------------------- stand-in

(define stand-in-example
  '(stand-in auto #:model-set heavy #:may (resume restart answer integrate settle)
             #:never (land promote) #:max-actions-per-run 3 #:max-per-hour 12
             #:escalate-to (dashboard notify)))

(test-case "stand-in: absent = null; defaults per kind"
  (define (stand-in-of . clauses) (hash-ref (compile-layer-datum (apply layer plan simple-adw clauses)) 'standIn))
  (check-equal? (stand-in-of) 'null)
  (define d (stand-in-of '(stand-in auto)))
  (check-equal? (hash-ref d 'kind) "auto")
  (check-equal? (hash-ref d 'may) '("resume"))
  (check-equal? (hash-ref d 'never) '())
  (check-equal? (hash-ref d 'maxActionsPerRun) 3)
  (check-equal? (hash-ref d 'maxPerHour) 12)
  (check-equal? (hash-ref d 'escalateTo) '("dashboard"))
  (check-equal? (hash-ref d 'modelSet) "heavy")
  (check-equal? (hash-ref d 'systemd) #f)
  (for ([k '(off suggest)])
    (define o (stand-in-of `(stand-in ,k)))
    (check-equal? (hash-ref o 'kind) (symbol->string k))
    (check-equal? (hash-ref o 'may) '("resume")))
  ;; the example of the feature
  (define e (stand-in-of stand-in-example '(notify email)))
  (check-equal? (hash-ref e 'kind) "auto")
  (check-equal? (hash-ref e 'may) '("resume" "restart" "answer" "integrate" "settle"))
  (check-equal? (hash-ref e 'never) '("land" "promote"))
  (check-equal? (hash-ref e 'escalateTo) '("dashboard" "notify"))
  (check-equal? (list (hash-ref e 'maxActionsPerRun) (hash-ref e 'maxPerHour) (hash-ref e 'modelSet)) '(3 12 "heavy"))
  (check-true (string? (json-pretty e)))
  ;; suggest may leave #:may empty: it only proposes
  (check-equal? (hash-ref (stand-in-of '(stand-in suggest #:may ())) 'may) '())
  ;; the full example declares one
  (define full (hash-ref (dynamic-require (build-path examples "full.rkt") 'layer-spec) 'standIn))
  (check-equal? (list (hash-ref full 'kind) (hash-ref full 'may) (hash-ref full 'maxPerHour)) '("suggest" ("resume" "restart") 6))
  ;; schema and reference name it
  (define prop (hash-ref (hash-ref (layer-schema) 'properties) 'standIn))
  (check-equal? (hash-ref (car (hash-ref prop 'oneOf)) 'type) "null")
  (check-true (string-contains? (reference-text) "(stand-in off|suggest|auto …) is declared at most once")))

(test-case "stand-in: rules"
  (define (with . clauses) (apply layer plan simple-adw clauses))
  (check-rejects (with '(stand-in suggest) '(stand-in auto #:id two)) #rx"only one stand-in may be declared")
  (check-rejects (with '(stand-in suggest #:may (resume restart) #:never (restart)))
                 #rx"\\(stand-in …\\): restart is in both #:may and #:never")
  (check-rejects (with '(stand-in auto #:may ()))
                 #rx"\\(stand-in auto\\): #:may is empty; list the decisions it may take, or use \\(stand-in suggest\\)")
  (check-rejects (with '(stand-in suggest #:max-actions-per-run 0)) #rx"#:max-actions-per-run must be between 1 and 20 \\(got 0\\)")
  (check-rejects (with '(stand-in suggest #:max-actions-per-run 21)) #rx"#:max-actions-per-run must be between 1 and 20 \\(got 21\\)")
  (check-rejects (with '(stand-in suggest #:max-per-hour 0)) #rx"#:max-per-hour must be between 1 and 1000 \\(got 0\\)")
  (check-rejects (with '(stand-in suggest #:max-per-hour 1001)) #rx"#:max-per-hour must be between 1 and 1000 \\(got 1001\\)")
  (check-not-exn (lambda () (compile-layer-datum (with '(stand-in suggest #:max-actions-per-run 20 #:max-per-hour 1000)))))
  (check-rejects (with '(stand-in suggest #:escalate-to ())) #rx"#:escalate-to is empty; list dashboard, notify or both")
  (check-rejects (with '(stand-in suggest #:escalate-to (notify)))
                 #rx"#:escalate-to notify needs a \\(notify …\\) that lists needs-human")
  (check-rejects (with '(stand-in suggest #:escalate-to (notify)) '(notify email #:events (failed)))
                 #rx"#:escalate-to notify needs a \\(notify …\\) that lists needs-human")
  (check-not-exn (lambda () (compile-layer-datum (with '(stand-in suggest #:escalate-to (notify)) '(notify email)))))
  (check-rejects (with '(stand-in suggest #:model-set fast))
                 #rx"\\(stand-in …\\): #:model-set fast is not a tier of agent claude-code, which runs ADW a")
  (check-not-exn (lambda () (compile-layer-datum
                             (with '(stand-in suggest #:model-set fast) '(agent claude-code #:models ((fast "claude-haiku-5-5")))))))
  (check-rejects (with '(stand-in suggest #:may (bless))) #rx"#:may")
  (check-rejects (with '(stand-in maybe)) #rx"stand-in")
  ;; land needs an automatic review that ships, used by an ADW with a (ship) step
  (check-rejects (with '(stand-in auto #:may (land)))
                 #rx"#:may land needs an automatic review that can pass: a \\(review auto … #:ship merge\\) used by an ADW with a \\(ship\\) step")
  (check-rejects (apply layer (append zte-parts (list '(review auto #:id pr-only #:require-checks (test) #:ship pr)
                                                      '(adw zte #:environment (worktree podman) #:review pr-only
                                                            (steps (run plan #:command plan) (check t #:cmd test) (review) (ship)))
                                                      '(stand-in auto #:may (land)))))
                 #rx"#:may land needs an automatic review that can pass")
  (check-not-exn (lambda () (compile-layer-datum (apply layer (append zte-parts (list (zte-adw) '(stand-in auto #:may (resume land))))))))
  ;; a suggestion to land needs nothing: only #:may is checked
  (check-not-exn (lambda () (compile-layer-datum (with '(stand-in suggest #:never (land))))))
  (check-rejects (with '(stand-in auto #:may (promote))) #rx"\\(stand-in …\\): #:may promote needs declared stages")
  (check-not-exn (lambda () (compile-layer-datum (promo-layer '(stand-in auto #:may (promote))))))
  (check-rejects (with '(stand-in suggest #:systemd #t) '(dashboard none))
                 #rx"#:systemd #t needs \\(dashboard local\\): the loop is the dashboard's")
  (check-not-exn (lambda () (compile-layer-datum (with '(stand-in suggest #:systemd #t)))))
  ;; the error of a layer file points at the (stand-in …) clause
  (define dir (make-temporary-file "al-standin-~a" 'directory))
  (call-with-output-file (build-path dir "agentic.rkt")
    (lambda (o) (write-string "#lang al\n(agentic-layer #:name \"x\"\n  (codebase #:root \".\" #:languages (c) (cmd test \"make\"))\n  (command plan #:template plan-chore)\n  (stand-in auto\n            #:may ())\n  (adw a (steps (run plan #:command plan))))\n" o)))
  (define msg (with-handlers ([exn:fail:syntax? exn-message]) (dynamic-require (build-path dir "agentic.rkt") #f) "no error"))
  (check-regexp-match #rx"agentic.rkt:5:2.*#:may is empty" msg)
  (delete-directory/files dir))
