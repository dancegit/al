#lang racket/base

;; Registry → JSON Schema (draft 2020-12) for the agentic-layer-v1 IR.

(require racket/list "meta.rkt" "vocab.rkt")
(provide layer-schema)

(define (type->schema type)
  (cond
    [(memq type '(string symbol)) (hasheq 'type "string")]
    [(eq? type 'integer) (hasheq 'type "integer")]
    [(eq? type 'boolean) (hasheq 'type "boolean")]
    [(eq? (car type) 'enum) (hasheq 'type "string" 'enum (map symbol->string (cdr type)))]
    [(eq? (car type) 'list-of) (hasheq 'type "array" 'items (type->schema (cadr type)))]
    [(memq (car type) '(ref optional-ref)) (hasheq 'type "string")]))

(define (object doc fields #:extra [extra (hasheq)])
  (define props
    (for/fold ([h extra]) ([f (in-list fields)])
      (hash-set h (field-json-key f) (hash-set (type->schema (field-type f)) 'description (field-doc f)))))
  (hasheq 'type "object"
          'description doc
          'additionalProperties #f
          'required (sort (map symbol->string (hash-keys props)) string<?)
          'properties props))

(define (kind-schema k)
  (object (kind-doc k) (kind-fields k)
          #:extra (hasheq 'id (hasheq 'type "string")
                          'kind (hasheq 'const (symbol->string (kind-name k)))
                          'status (hasheq 'const (symbol->string (kind-status k))))))

(define (family-schema fam)
  (define alts (map kind-schema (family-kinds fam)))
  (cond
    [(memq (family-name fam) singleton-families)
     (hasheq 'description (family-doc fam)
             'oneOf (if (family-default-kind fam) alts (cons (hasheq 'type "null") alts)))]
    [else (hasheq 'type "array" 'description (family-doc fam) 'items (hasheq 'oneOf alts))]))

(define (rec name #:extra [extra (hasheq)])
  (define r (find-record name))
  (object (record-doc r) (record-fields r) #:extra extra))

(define name-prop (hasheq 'name (hasheq 'type "string")))
(define (step name kind) (rec name #:extra (hash-set name-prop 'kind (hasheq 'const kind))))
(define (array-of s) (hasheq 'type "array" 'items s))

(define (layer-schema)
  (define props
    (for/fold ([h (hasheq
                   'schemaVersion (hasheq 'const "agentic-layer-v1")
                   'name (hasheq 'type "string" 'pattern "^[a-z0-9][a-z0-9._-]*$")
                   'description (hasheq 'type "string")
                   'llmTxt (hasheq 'type "string")
                   'codebase
                   (rec 'codebase
                        #:extra (hasheq 'commands (array-of (rec 'cmd #:extra name-prop))
                                        'paths (rec 'paths)
                                        'conditionalDocs (array-of (rec 'conditional-doc))))
                   'commands (array-of (rec 'command #:extra name-prop))
                   'hops (array-of (rec 'hop #:extra name-prop))
                   'experts (array-of (rec 'expert #:extra name-prop))
                   'adws
                   (array-of
                    (rec 'adw
                         #:extra (hash-set name-prop 'steps
                                           (array-of (hasheq 'oneOf (list (step 'step-run "run")
                                                                          (step 'step-check "check")
                                                                          (step 'step-review "review")
                                                                          (step 'step-ship "ship")
                                                                          (step 'step-fan-out "fan-out"))))))))])
              ([fam (in-list (all-families))])
      (hash-set h (string->symbol (family-key fam)) (family-schema fam))))
  (hasheq '$schema "https://json-schema.org/draft/2020-12/schema"
          '$id "https://al.local/schemas/agentic-layer-v1.schema.json"
          'title "agentic-layer-v1"
          'description "IR emitted by #lang al. Generated from racket/al/private/vocab.rkt — do not edit."
          'type "object"
          'additionalProperties #f
          'required (sort (map symbol->string (hash-keys props)) string<?)
          'properties props))
