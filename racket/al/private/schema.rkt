#lang racket/base

;; Registry → JSON Schema (draft 2020-12) for the agentic-layer-v1 IR.

(require racket/list "meta.rkt" "vocab.rkt")
(provide layer-schema)

(define (type->schema type)
  (cond
    [(memq type '(string symbol)) (hasheq 'type "string")]
    [(eq? type 'tier) (hasheq 'type "string" 'pattern "^[a-z][a-z0-9-]*$")]
    [(eq? type 'integer) (hasheq 'type "integer")]
    [(eq? type 'decimal) (hasheq 'type "number" 'minimum 0)]
    [(eq? type 'boolean) (hasheq 'type "boolean")]
    [(eq? (car type) 'enum) (hasheq 'type "string" 'enum (map symbol->string (cdr type)))]
    [(eq? (car type) 'list-of) (hasheq 'type "array" 'items (type->schema (cadr type)))]
    [(eq? (car type) 'record) (rec (cadr type))]
    [(memq (car type) '(ref optional-ref)) (hasheq 'type "string")]))

(define (field-schema f)
  (define s (hash-set (type->schema (field-type f)) 'description (field-doc f)))
  (if (field-none? f) (hash-set s 'type (list (hash-ref s 'type) "null")) s))

;; #:optional lists JSON keys (symbols) that are left out of `required`.
(define (object doc fields #:extra [extra (hasheq)] #:optional [optional '()])
  (define props
    (for/fold ([h extra]) ([f (in-list (emitted-fields fields))])
      (hash-set h (field-json-key f) (field-schema f))))
  (hasheq 'type "object"
          'description doc
          'additionalProperties #f
          'required (sort (map symbol->string (filter (lambda (k) (not (memq k optional))) (hash-keys props))) string<?)
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

(define (rec name #:extra [extra (hasheq)] #:optional [optional '()])
  (define r (find-record name))
  (object (record-doc r) (record-fields r) #:extra extra #:optional optional))

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
                         #:optional '(deliverTo)
                         #:extra (hash-set name-prop 'steps
                                           (array-of (hasheq 'oneOf (list (step 'step-run "run")
                                                                          (step 'step-check "check")
                                                                          (step 'step-script "script")
                                                                          (step 'step-review "review")
                                                                          (step 'step-ship "ship")
                                                                          (step 'step-fan-out "fan-out")))))))
                   'stages
                   (array-of (rec 'stage #:extra (hash-set name-prop 'origin
                                                           (hasheq 'type "string"
                                                                   'description "\"declared\", or \"preset NAME\" for a stage a (delivery …) preset made."))))
                   'promotions
                   (array-of (rec 'promotion
                                  #:extra (hasheq 'hotfix (hasheq 'type "boolean"
                                                                  'description "FROM is cut from the final stage TO and goes straight back into it (a hotfix): its own gate, merged back into the working stages.")
                                                  'origin (hasheq 'type "string"
                                                                  'description "\"declared\" (a (promotion …) clause) or \"default\" (a non-final stage without one)."))))
                   'delivery
                   (hasheq 'type (list "object" "null")
                           'description "The (delivery PRESET …) clause: {preset, params, expansion (the clauses it stands for, one string each)}; null when stages are declared by hand only."))])
              ([fam (in-list (all-families))])
      (hash-set h (string->symbol (family-key fam)) (family-schema fam))))
  (hasheq '$schema "https://json-schema.org/draft/2020-12/schema"
          '$id "https://al.local/schemas/agentic-layer-v1.schema.json"
          'title "agentic-layer-v1"
          'description "IR emitted by #lang al. Generated from racket/al/private/vocab.rkt — do not edit."
          'type "object"
          'additionalProperties #f
          ;; stages, cycle, delivery and promotions exist only in the IR of a layer that declares a stage
          'required (sort (map symbol->string (filter (lambda (k) (not (memq k '(stages cycle delivery promotions)))) (hash-keys props))) string<?)
          'properties props))
