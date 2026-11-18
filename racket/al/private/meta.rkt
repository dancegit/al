#lang racket/base

;; The meta-DSL. Everything `#lang al` can say is declared here ONCE, as data:
;;
;;   (define-family NAME #:key "jsonKey" #:letter "Pi" #:default KIND-or-#f "doc")
;;   (define-kind FAMILY KIND [#:status stable|experimental|planned] "doc"
;;     [field type default "doc"] ...)
;;   (define-record NAME [#:positional (FIELD …)] "doc" [field type default "doc"] ...)
;;   (define-preset CLAUSE NAME "doc" #:expand PROC [field type default "doc"] ...)
;;
;; A preset is a named shorthand written (CLAUSE NAME #:field value …), e.g. (delivery sprint
;; #:length-days 7). Its fields are checked like any other; PROC receives them as a hash (JSON
;; keys, the compiled values) and returns the clauses the preset stands for, as data, so the
;; compiler splices them in and the reference (and `al explain`) can print what it expands to.
;;
;; From these declarations three things are derived, so they cannot drift:
;;   1. the DSL forms the compiler accepts   (private/compiler.rkt)
;;   2. the JSON Schema of the emitted IR    (private/schema.rkt)
;;   3. the LLM-facing reference document    (private/reference.rkt)
;;
;; Field types:
;;   string | integer | boolean | symbol | (enum a b …) | (list-of T)
;;   decimal           a number ≥ 0, integer or with a fraction (2, 2.5)
;;   tier              the name of a model tier (base, heavy, fast, …); which tiers exist is a semantic rule
;;   (ref F)           id of a declared option in family F, or `command` / `cmd` / `adw` / `artefact`
;;   (optional-ref F)  same, or "" for none
;;   (record R)        one small record R written as a list: its #:positional fields first, in order,
;;                     then #:keyword value pairs, e.g. (heavy "claude-fable-5-2" #:effort high).
;;                     In a (list-of (record R)) the first positional field is the item's key: unique.
;;   (pairs R)         one record R written as a list of (field value) pairs, e.g.
;;                     ((host build-2) (classes (chore bug))): every pair names a field of R and is
;;                     checked like `#:field value`; the IR holds R as an object.
;;   (shorthand F KEY) a string that is shorthand for the item KEY of the (list-of (record R)) field F
;;                     of the same form; it is folded into F (giving KEY both ways is an error) and is
;;                     not emitted itself.
;; A default of #:required makes the field mandatory; #:none means "not set" (JSON null).

(require racket/list racket/string)

(provide define-family define-kind define-record define-preset
         (struct-out family) (struct-out kind) (struct-out field) (struct-out record) (struct-out preset)
         all-families all-records find-family find-kind find-record
         all-presets find-preset preset-default-params preset-expansion
         field-required? field-none? field-shorthand? kebab->camel field-json-key field-keyword
         emitted-fields)

(struct field (name type default doc) #:transparent)
(struct kind (name status doc fields) #:transparent)
(struct family (name key letter default-kind doc [kinds #:mutable]) #:transparent)
(struct record (name doc fields positional) #:transparent)
(struct preset (clause name doc fields expand) #:transparent)

(define families '())
(define records '())
(define presets '())

(define (all-families) (reverse families))
(define (all-records) (reverse records))
(define (find-family name) (findf (lambda (f) (eq? (family-name f) name)) families))
(define (find-record name) (findf (lambda (r) (eq? (record-name r) name)) records))
(define (find-kind fam name) (findf (lambda (k) (eq? (kind-name k) name)) (family-kinds fam)))

(define (all-presets) (reverse presets))
(define (find-preset clause name)
  (findf (lambda (p) (and (eq? (preset-clause p) clause) (eq? (preset-name p) name))) presets))
(define (register-preset! p) (set! presets (cons p presets)))

;; A field default as the compiler emits it: symbols (enum values, names) become strings.
(define (default->json v)
  (cond [(symbol? v) (symbol->string v)]
        [(list? v) (map default->json v)]
        [else v]))
;; The parameters of preset P with every field at its default (JSON keys).
(define (preset-default-params p)
  (for/fold ([h (hasheq)]) ([f (in-list (preset-fields p))])
    (hash-set h (field-json-key f) (default->json (field-default f)))))
;; The clauses P stands for with PARAMS (default: its defaults), as data.
(define (preset-expansion p [params (preset-default-params p)])
  ((preset-expand p) params))

(define (register-family! f) (set! families (cons f families)))
(define (register-record! r) (set! records (cons r records)))
(define (register-kind! fam-name k)
  (define f (find-family fam-name))
  (unless f (error 'define-kind "unknown family ~a" fam-name))
  (set-family-kinds! f (append (family-kinds f) (list k))))

(define (field-required? f) (eq? (field-default f) '#:required))
(define (field-none? f) (eq? (field-default f) '#:none))
(define (field-shorthand? f) (and (pair? (field-type f)) (eq? (car (field-type f)) 'shorthand)))
;; The fields that appear in the IR (shorthands are folded into their target field).
(define (emitted-fields fields) (filter (lambda (f) (not (field-shorthand? f))) fields))

(define (kebab->camel sym)
  (define parts (string-split (symbol->string sym) "-"))
  (string->symbol
   (apply string-append
          (car parts)
          (for/list ([p (in-list (cdr parts))])
            (string-append (string-upcase (substring p 0 1)) (substring p 1))))))

(define (field-json-key f) (kebab->camel (field-name f)))
(define (field-keyword f) (string->keyword (symbol->string (field-name f))))

(define-syntax-rule (define-family name #:key key #:letter letter #:default default-kind doc)
  (register-family! (family 'name key letter 'default-kind doc '())))

(define-syntax define-kind
  (syntax-rules ()
    [(_ fam name #:status st doc [fname ftype fdefault fdoc] ...)
     (register-kind! 'fam (kind 'name 'st doc (list (field 'fname 'ftype 'fdefault fdoc) ...)))]
    [(_ fam name doc [fname ftype fdefault fdoc] ...)
     (define-kind fam name #:status stable doc [fname ftype fdefault fdoc] ...)]))

(define-syntax define-record
  (syntax-rules ()
    [(_ name #:positional (p ...) doc [fname ftype fdefault fdoc] ...)
     (register-record! (record 'name doc (list (field 'fname 'ftype 'fdefault fdoc) ...) '(p ...)))]
    [(_ name doc [fname ftype fdefault fdoc] ...)
     (register-record! (record 'name doc (list (field 'fname 'ftype 'fdefault fdoc) ...) '()))]))

(define-syntax-rule (define-preset clause name doc #:expand proc [fname ftype fdefault fdoc] ...)
  (register-preset! (preset 'clause 'name doc (list (field 'fname 'ftype 'fdefault fdoc) ...) proc)))
