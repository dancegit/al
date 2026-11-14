#lang racket/base

;; #lang al — one (agentic-layer …) declaration per file, compiled at expansion time.
;; `racket agentic.rkt` prints the agentic-layer-v1 JSON IR; (require "agentic.rkt") gives `layer-spec`.

(require "private/json-pretty.rkt"
         (for-syntax racket/base
                     syntax/parse
                     "private/compiler.rkt"))

(provide (rename-out [al-module-begin #%module-begin])
         agentic-layer)

(define-syntax (agentic-layer stx)
  (raise-syntax-error 'agentic-layer "only valid as the single top-level declaration in #lang al" stx))

(define-syntax (al-module-begin stx)
  (syntax-parse stx
    [(_ declaration)
     (define spec
       (with-handlers ([exn:al?
                        (lambda (e)
                          (define parts (syntax->list #'declaration))
                          (define idx (exn:al-clause-index e))
                          (define where
                            (if (and idx parts (< (add1 idx) (length parts)))
                                (list-ref parts (add1 idx))
                                #'declaration))
                          (raise-syntax-error 'al (exn-message e) where))])
         (compile-layer-datum (syntax->datum #'declaration))))
     #`(#%module-begin
        (define layer-spec '#,spec)
        (provide layer-spec)
        (module+ main
          (display (json-pretty layer-spec))
          (newline)))]
    [(_)
     (raise-syntax-error 'al "expected one (agentic-layer …) declaration" stx)]
    [(_ declaration ...+)
     (raise-syntax-error 'al "expected exactly one (agentic-layer …) declaration" stx)]))
