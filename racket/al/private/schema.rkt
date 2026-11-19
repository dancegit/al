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
    [(eq? (car type) 'pairs) (rec (cadr type))]
    [(memq (car type) '(ref optional-ref)) (hasheq 'type "string")]))

(define (field-schema f)
  (define s (hash-set (type->schema (field-type f)) 'description (field-doc f)))
  (if (field-none? f) (hash-set s 'type (list (hash-ref s 'type) "null")) s))

;; The compiler leaves these out of the IR when they are empty, so that a layer without a toolchain
;; compiles to the same IR as before toolchains existed: they are not required.
(define (omitted-when-empty? f) (equal? (field-type f) '(optional-ref toolchain)))
(define optional-families '(toolchain))

;; #:optional lists JSON keys (symbols) that are left out of `required`.
(define (object doc fields #:extra [extra (hasheq)] #:optional [optional '()])
  (define props
    (for/fold ([h extra]) ([f (in-list (emitted-fields fields))])
      (hash-set h (field-json-key f) (field-schema f))))
  (define left-out (append optional (map field-json-key (filter omitted-when-empty? fields))))
  (hasheq 'type "object"
          'description doc
          'additionalProperties #f
          'required (sort (map symbol->string (filter (lambda (k) (not (memq k left-out))) (hash-keys props))) string<?)
          'properties props))

;; Keys the compiler derives for an option of a family, beyond its declared fields.
(define (derived-props fam)
  (case (family-name fam)
    [(toolchain)
     (hasheq 'agentClis (hasheq 'type "array" 'items (hasheq 'type "string")
                                'description "Derived: the agent kinds (sorted, without mock) whose workflows run their agent inside an environment using this toolchain; feature agent-cli provides their CLIs."))]
    [else (hasheq)]))

(define (kind-schema fam k)
  (object (kind-doc k) (kind-fields k)
          #:extra (for/fold ([h (derived-props fam)])
                            ([(key v) (in-hash (hasheq 'id (hasheq 'type "string")
                                                       'kind (hasheq 'const (symbol->string (kind-name k)))
                                                       'status (hasheq 'const (symbol->string (kind-status k)))))])
                    (hash-set h key v))))

(define (family-schema fam)
  (define alts (map (lambda (k) (kind-schema fam k)) (family-kinds fam)))
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
                        #:extra (hasheq 'commands (array-of (rec 'cmd #:extra name-prop #:optional '(needsHome)))
                                        'paths (rec 'paths)
                                        'conditionalDocs (array-of (rec 'conditional-doc))))
                   'commands (array-of (rec 'command #:extra name-prop))
                   'hops (array-of (rec 'hop #:extra name-prop))
                   'experts (array-of (rec 'expert #:extra name-prop))
                   'hosts (array-of (rec 'host))
                   'registry (hasheq 'oneOf (list (rec 'registry) (hasheq 'type "null")))
                   'testing (rec 'testing)
                   'adws
                   (array-of
                    (rec 'adw
                         #:optional '(deliverTo pinnedHome)
                         #:extra (hash-set (hash-set name-prop 'pinnedHome
                                                     (hasheq 'type "array" 'items (hasheq 'type "string")
                                                             'description "Only with a (placement …): why the workflow always runs on `home`, one sentence each; empty = it may run on any listed host."))
                                           'steps
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
          ;; stages, cycle, delivery and promotions exist only in the IR of a layer that declares a stage,
          ;; toolchains and placement only in the IR of a layer that declares one, testing only with a (testing …) clause
          'required (sort (for/list ([k (in-list (hash-keys props))]
                                     #:unless (or (memq k '(stages cycle delivery promotions testing placement))
                                                  (member (symbol->string k)
                                                          (for/list ([fam (in-list (all-families))] #:when (memq (family-name fam) optional-families))
                                                            (family-key fam)))))
                            (symbol->string k))
                          string<?)
          'properties props))
