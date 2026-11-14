#lang racket/base
;; racket racket/al/cli.rkt schema|reference|counts
(require racket/cmdline racket/list
         "private/json-pretty.rkt" "private/schema.rkt" "private/reference.rkt"
         "private/meta.rkt" "private/vocab.rkt")

(module+ main
  (define what (command-line #:program "al" #:args (what) what))
  (cond
    [(equal? what "schema") (display (json-pretty (layer-schema))) (newline)]
    [(equal? what "reference") (display (reference-text))]
    [(equal? what "counts")
     (for ([fam (in-list (all-families))])
       (printf "~a\t~a\t~a\n" (family-name fam)
               (count (lambda (k) (not (eq? (kind-status k) 'planned))) (family-kinds fam))
               (length (family-kinds fam))))]
    [else (eprintf "usage: cli.rkt schema|reference|counts\n") (exit 2)]))
