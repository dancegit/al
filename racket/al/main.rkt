#lang racket/base

;; #lang al — one (agentic-layer …) declaration per file, compiled at expansion time.
;; `racket agentic.rkt` prints the agentic-layer-v1 JSON IR; (require "agentic.rkt") gives `layer-spec`.
;;
;; (include "FILE") clauses are spliced in here, before the compiler sees the datum: FILE is read
;; as syntax (plain s-expressions, no #lang line) relative to the including file, so every error
;; the compiler raises about an included clause points into FILE, at that clause.

(require "private/json-pretty.rkt"
         (for-syntax racket/base
                     racket/list
                     racket/path
                     syntax/parse
                     "private/compiler.rkt"))

(provide (rename-out [al-module-begin #%module-begin])
         agentic-layer)

(define-syntax (agentic-layer stx)
  (raise-syntax-error 'agentic-layer "only valid as the single top-level declaration in #lang al" stx))

(begin-for-syntax
  (define (include-form? s)
    (define d (syntax->list s))
    (and d (pair? d) (eq? (syntax-e (car d)) 'include)))

  ;; The clauses of an included file, as syntax objects carrying that file's source locations.
  (define (read-included form base-dir)
    (define parts (syntax->list form))
    (unless (and (= (length parts) 2) (string? (syntax-e (cadr parts))))
      (raise-syntax-error 'al "expected (include \"FILE\") with one path in double quotes" form))
    (define rel (syntax-e (cadr parts)))
    (when (absolute-path? rel)
      (raise-syntax-error 'al "(include …) takes a path relative to the including file" form))
    (define path (simplify-path (build-path base-dir rel)))
    (unless (file-exists? path)
      (raise-syntax-error 'al (format "(include ~s): no such file ~a" rel (path->string path)) form))
    (define clauses
      (with-handlers ([exn:fail:read? (lambda (e)
                                        (raise-syntax-error 'al (format "(include ~s): ~a" rel (exn-message e)) form))])
        (call-with-input-file path
          (lambda (in)
            (port-count-lines! in)
            (let loop ([acc '()])
              (define x (read-syntax path in))
              (if (eof-object? x) (reverse acc) (loop (cons x acc))))))))
    (for ([c (in-list clauses)])
      (when (include-form? c)
        (raise-syntax-error 'al "an included file cannot include another file" c))
      (unless (syntax->list c)
        (raise-syntax-error 'al (format "(include ~s): expected clauses in parentheses, got ~s" rel (syntax->datum c)) c)))
    clauses)

  ;; Every element of `parts` with (include …) replaced by the included clauses.
  (define (splice parts base-dir)
    (append* (for/list ([p (in-list parts)])
               (if (include-form? p) (read-included p base-dir) (list p)))))

  ;; (agentic-layer …) with includes spliced at the top level and inside (codebase …).
  (define (expand-includes decl base-dir)
    (define parts (syntax->list decl))
    (if (not parts)
        decl
        (let* ([top (splice (cdr parts) base-dir)]
               [top (for/list ([p (in-list top)])
                      (define d (syntax->list p))
                      (if (and d (pair? d) (eq? (syntax-e (car d)) 'codebase))
                          (datum->syntax p (cons (car d) (splice (cdr d) base-dir)) p p)
                          p))])
          (datum->syntax decl (cons (car parts) top) decl decl))))

  (define (source-dir stx)
    (define src (syntax-source stx))
    (cond [(path? src) (let-values ([(dir _name _dir?) (split-path src)]) (if (path? dir) dir (current-directory)))]
          [(string? src) (let-values ([(dir _name _dir?) (split-path src)]) (if (path? dir) dir (current-directory)))]
          [else (current-directory)])))

(define-syntax (al-module-begin stx)
  (syntax-parse stx
    [(_ declaration)
     (define decl (expand-includes #'declaration (source-dir #'declaration)))
     (define spec
       (with-handlers ([exn:al?
                        (lambda (e)
                          (define parts (syntax->list decl))
                          (define idx (exn:al-clause-index e))
                          (define top (if (pair? idx) (car idx) idx))
                          (define clause
                            (and top parts (< (add1 top) (length parts)) (list-ref parts (add1 top))))
                          (define where
                            (cond [(not clause) decl]
                                  [(pair? idx)
                                   (define subs (syntax->list clause))
                                   (if (and subs (< (add1 (cdr idx)) (length subs)))
                                       (list-ref subs (add1 (cdr idx)))
                                       clause)]
                                  [else clause]))
                          (raise-syntax-error 'al (exn-message e) where))])
         (compile-layer-datum (syntax->datum decl))))
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
