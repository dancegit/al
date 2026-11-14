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
  (check-equal? (hash-ref ir 'voice) 'null))

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
                 #rx"only accepted inside a podman environment")
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
                     '(environment worktree) '(environment podman)
                     '(review auto #:require-checks (test lint))
                     '(adw zte #:environment (worktree podman)
                           (steps (run plan #:command plan) (check t #:cmd test) (review) (ship))))))
  (check-equal? (hash-ref (car (hash-ref ir 'reviews)) 'ship) "merge"))

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
