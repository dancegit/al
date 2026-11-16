#lang racket/base

;; (agentic-layer …) datum  →  agentic-layer-v1 IR (a jsexpr of immutable hasheqs).
;;
;; Shape checking is entirely driven by the registry in vocab.rkt. On top of that the
;; compiler enforces the semantic rules (vocab.rkt `semantic-rules`): these are what make
;; an LLM-authored layer coherent rather than merely well-formed.

(require racket/list racket/string "meta.rkt" "vocab.rkt")

(provide compile-layer-datum (struct-out exn:al))

;; clause-index: index into (cdr datum) of the clause being compiled, or #f; or a pair
;; (INDEX . SUB) for a sub-clause of (codebase …): SUB indexes (cdr <the codebase form>).
(struct exn:al exn:fail (clause-index))

(define current-clause (make-parameter #f))
(define pending-refs (make-parameter #f)) ; box of (list target id who clause)

(define (fail fmt . args)
  (raise (exn:al (apply format fmt args) (current-continuation-marks) (current-clause))))

(define (names xs) (string-join (map (lambda (x) (format "~a" x)) xs) ", "))

(define (split-kwargs lst who)
  (let loop ([l lst] [pos '()] [kws '()])
    (cond [(null? l) (values (reverse pos) (reverse kws))]
          [(keyword? (car l))
           (when (null? (cdr l)) (fail "~a: keyword ~a has no value" who (car l)))
           (when (assq (car l) kws) (fail "~a: keyword ~a given twice" who (car l)))
           (loop (cddr l) pos (cons (cons (car l) (cadr l)) kws))]
          [else (loop (cdr l) (cons (car l) pos) kws)])))

(define (note-ref! target id who)
  (define b (pending-refs))
  (set-box! b (cons (list target id who (current-clause)) (unbox b))))

(define (name->string v who)
  (cond [(symbol? v) (symbol->string v)]
        [(string? v) v]
        [else (fail "~a: expected a name, got ~s" who v)]))

(define tier-rx #px"^[a-z][a-z0-9-]*$")

(define (coerce type v who)
  (define (bad what) (fail "~a: expected ~a, got ~s" who what v))
  (cond
    [(eq? v '#:none) 'null]
    [(eq? type 'string) (if (string? v) v (bad "a string in double quotes"))]
    [(eq? type 'integer) (if (exact-integer? v) v (bad "an integer"))]
    [(eq? type 'decimal)
     (if (and (real? v) (>= v 0) (not (eqv? v +inf.0)))
         (if (and (exact? v) (not (integer? v))) (exact->inexact v) v)
         (bad "a number ≥ 0, e.g. 5 or 2.5"))]
    [(eq? type 'boolean) (if (boolean? v) v (bad "#t or #f"))]
    [(eq? type 'symbol) (name->string v who)]
    [(eq? type 'tier)
     (define s (name->string v who))
     (unless (regexp-match? tier-rx s)
       (bad "a tier name: lowercase letters, digits and dashes, e.g. base, heavy, fast"))
     s]
    [(and (pair? type) (eq? (car type) 'enum))
     (define s (cond [(symbol? v) v] [(string? v) (string->symbol v)] [else #f]))
     (if (and s (memq s (cdr type)))
         (symbol->string s)
         (bad (format "one of: ~a" (names (cdr type)))))]
    [(and (pair? type) (eq? (car type) 'list-of))
     (unless (list? v) (bad "a parenthesised list, e.g. (a b)"))
     (define items (for/list ([x (in-list v)]) (coerce (cadr type) x who)))
     (define item-type (cadr type))
     (when (and (pair? item-type) (eq? (car item-type) 'record))
       (define key (record-key (cadr item-type)))
       (when key
         (define dup (check-duplicates (map (lambda (h) (hash-ref h key)) items)))
         (when dup (fail "~a: `~a` is given twice" who dup))))
     items]
    [(and (pair? type) (eq? (car type) 'record)) (compile-item (cadr type) v who)]
    [(and (pair? type) (eq? (car type) 'shorthand)) (if (string? v) v (bad "a string in double quotes"))]
    [(and (pair? type) (memq (car type) '(ref optional-ref)))
     (define s (name->string v who))
     (unless (and (eq? (car type) 'optional-ref) (string=? s ""))
       (note-ref! (cadr type) s who))
     s]
    [else (error 'al "vocabulary bug: unknown field type ~s" type)]))

;; JSON key of the first positional field of an item record (its key within a list), or #f.
(define (record-key name)
  (define r (find-record name))
  (and (pair? (record-positional r))
       (kebab->camel (car (record-positional r)))))

;; (POS … #:field value …) → hash, for a (record R) field.
(define (compile-item name v who)
  (define r (find-record name))
  (define pos-names (record-positional r))
  (define shape (string-join (append (map (lambda (p) (string-upcase (symbol->string p))) pos-names)
                                     (list "[#:field value …]")) " "))
  (unless (list? v) (fail "~a: expected (~a), got ~s" who shape v))
  (define-values (pos kws) (split-kwargs v who))
  (unless (= (length pos) (length pos-names))
    (fail "~a: expected (~a), got ~s" who shape v))
  (define iwho (format "~a (~a …)" who (car v)))
  (compile-fields (record-fields r)
                  (append (for/list ([p (in-list pos-names)] [x (in-list pos)])
                            (cons (string->keyword (symbol->string p)) x))
                          kws)
                  iwho))

(define (compile-fields fields kws who #:init [init (hasheq)] #:extra [extra '()])
  (for ([p (in-list kws)])
    (unless (or (memq (car p) extra)
                (findf (lambda (f) (eq? (field-keyword f) (car p))) fields))
      (fail "~a: unknown field ~a; fields are: ~a" who (car p)
            (string-join (map (lambda (f) (format "~a" (field-keyword f))) fields) " "))))
  (define h
    (for/fold ([h init]) ([f (in-list fields)])
      (define p (assq (field-keyword f) kws))
      (define fwho (format "~a ~a" who (field-keyword f)))
      (hash-set h (field-json-key f)
                (cond [p (coerce (field-type f) (cdr p) fwho)]
                      [(field-required? f)
                       (fail "~a: missing required field ~a — ~a" who (field-keyword f) (field-doc f))]
                      [else (coerce (field-type f) (field-default f) fwho)]))))
  (fold-shorthands fields kws h who))

;; A (shorthand F KEY) field is folded into the list field F as the item KEY: given explicitly
;; while F also lists KEY → error; F without KEY → the shorthand's value (given or default) is
;; added in front. The shorthand itself is not emitted.
(define (fold-shorthands fields kws h who)
  (for/fold ([h h]) ([f (in-list (reverse fields))] #:when (field-shorthand? f))
    (define target (cadr (field-type f)))
    (define key (symbol->string (caddr (field-type f))))
    (define tf (findf (lambda (x) (eq? (field-name x) target)) fields))
    (define item-type (cadr (field-type tf)))
    (define rkey (record-key (cadr item-type)))
    (define items (hash-ref h (field-json-key tf)))
    (define listed? (findf (lambda (i) (equal? (hash-ref i rkey) key)) items))
    (define given? (assq (field-keyword f) kws))
    (when (and listed? given?)
      (fail "~a: tier `~a` is given twice: by ~a and in ~a; keep one" who key (field-keyword f) (field-keyword tf)))
    (define h* (hash-remove h (field-json-key f)))
    (if listed?
        h*
        (hash-set h* (field-json-key tf)
                  (cons (compile-item (cadr item-type) (list (string->symbol key) (hash-ref h (field-json-key f)))
                                      (format "~a ~a" who (field-keyword f)))
                        items)))))

(define (rec-fields name) (record-fields (find-record name)))

;; --- options: (FAMILY KIND #:id ID #:field value …) -------------------------------------

(define (compile-option fam datum)
  (define fname (family-name fam))
  (define rest (cdr datum))
  (define kind-names (map kind-name (family-kinds fam)))
  (when (or (null? rest) (not (symbol? (car rest))))
    (fail "(~a …): expected a kind name first; kinds are: ~a" fname (names kind-names)))
  (define k (find-kind fam (car rest)))
  (unless k
    (fail "(~a ~a …): unknown ~a kind; kinds are: ~a" fname (car rest) fname (names kind-names)))
  (define who (format "(~a ~a)" fname (kind-name k)))
  (define-values (pos kws) (split-kwargs (cdr rest) who))
  (unless (null? pos) (fail "~a: unexpected ~s; fields are given as #:keyword value" who (car pos)))
  (define id (cond [(assq '#:id kws) => (lambda (p) (name->string (cdr p) who))]
                   [else (symbol->string (kind-name k))]))
  (compile-fields (kind-fields k) kws who #:extra '(#:id)
                  #:init (hasheq 'id id 'kind (symbol->string (kind-name k))
                                 'status (symbol->string (kind-status k)))))

(define (default-option fam)
  (define k (find-kind fam (family-default-kind fam)))
  (compile-fields (kind-fields k) '() (format "(~a ~a)" (family-name fam) (kind-name k))
                  #:init (hasheq 'id (symbol->string (kind-name k))
                                 'kind (symbol->string (kind-name k))
                                 'status (symbol->string (kind-status k)))))

;; --- codebase ---------------------------------------------------------------------------

(define (compile-codebase datum)
  (define-values (subs kws) (split-kwargs (cdr datum) "(codebase)"))
  (define base (compile-fields (rec-fields 'codebase) kws "(codebase)"))
  ;; position of each sub-clause in (cdr datum), so an error points at the sub-clause itself
  (define positions
    (let loop ([l (cdr datum)] [i 0] [acc '()])
      (cond [(null? l) (reverse acc)]
            [(keyword? (car l)) (loop (if (pair? (cdr l)) (cddr l) '()) (+ i 2) acc)]
            [else (loop (cdr l) (add1 i) (cons i acc))])))
  (define top (current-clause))
  (define cmds '())
  (define docs '())
  (define paths #f)
  (for ([s (in-list subs)] [pos (in-list positions)])
    (parameterize ([current-clause (if (exact-integer? top) (cons top pos) top)])
      (unless (and (pair? s) (symbol? (car s)))
        (fail "(codebase): unexpected ~s; expected (cmd …), (paths …) or (conditional-doc …)" s))
      (case (car s)
        [(cmd)
         (unless (and (>= (length s) 3) (symbol? (cadr s)) (string? (caddr s)))
           (fail "(cmd …): expected (cmd NAME \"shell command\" …), got ~s" s))
         (define who (format "(cmd ~a)" (cadr s)))
         (define-values (pos k) (split-kwargs (cdddr s) who))
         (unless (null? pos) (fail "~a: unexpected ~s" who (car pos)))
         (when (findf (lambda (c) (equal? (hash-ref c 'name) (symbol->string (cadr s)))) cmds)
           (fail "~a: declared twice" who))
         (set! cmds (cons (compile-fields (rec-fields 'cmd) (cons (cons '#:run (caddr s)) k) who
                                          #:init (hasheq 'name (symbol->string (cadr s))))
                          cmds))]
        [(paths)
         (when paths (fail "(codebase): (paths …) given twice"))
         (define-values (pos k) (split-kwargs (cdr s) "(paths)"))
         (unless (null? pos) (fail "(paths): unexpected ~s" (car pos)))
         (set! paths (compile-fields (rec-fields 'paths) k "(paths)"))]
        [(conditional-doc)
         (unless (and (= (length s) 3) (string? (cadr s)) (string? (caddr s)))
           (fail "(conditional-doc …): expected (conditional-doc \"path\" \"when …\"), got ~s" s))
         (set! docs (cons (hasheq 'path (cadr s) 'when (caddr s)) docs))]
        [(include) (fail include-unexpanded)]
        [else (fail "(codebase): unknown sub-clause ~a; expected cmd, paths, conditional-doc or include" (car s))])))
  (when (null? cmds)
    (fail "(codebase): declare at least one (cmd NAME \"…\"), typically install, test, lint, build"))
  (hash-set* base
             'commands (reverse cmds)
             'paths (or paths (compile-fields (rec-fields 'paths) '() "(paths)"))
             'conditionalDocs (reverse docs)))

(define include-unexpanded
  "(include \"FILE\") is resolved by `#lang al` when the file is compiled; compile agentic.rkt itself (racket agentic.rkt or al compile), not its datum")

;; --- named records: command, hop, expert ------------------------------------------------

(define (compile-named record-name datum)
  (unless (and (>= (length datum) 2) (symbol? (cadr datum)))
    (fail "(~a …): expected (~a NAME #:field value …)" record-name record-name))
  (define who (format "(~a ~a)" record-name (cadr datum)))
  (define-values (pos kws) (split-kwargs (cddr datum) who))
  (unless (null? pos) (fail "~a: unexpected ~s; fields are given as #:keyword value" who (car pos)))
  (compile-fields (rec-fields record-name) kws who
                  #:init (hasheq 'name (symbol->string (cadr datum)))))

;; --- adw --------------------------------------------------------------------------------

(define step-kinds "run, check, script, review, ship, compose, fan-out")

(define (compile-fan-out s adw-name named)
  (define f (named 'step-fan-out 'fan-out #f))
  (define who (format "(adw ~a) step (fan-out ~a)" adw-name (hash-ref f 'name)))
  (define n (hash-ref f 'count))
  (unless (<= 2 n 8)
    (fail "~a: #:count must be between 2 and 8 (got ~a)" who n))
  (unless (<= 0 (hash-ref f 'maxParallel) n)
    (fail "~a: #:max-parallel must be between 0 (= #:count) and #:count ~a (got ~a)"
          who n (hash-ref f 'maxParallel)))
  f)

(define (compile-script s adw-name named)
  (define f (named 'step-script 'script #f))
  (define who (format "(adw ~a) step (script ~a)" adw-name (hash-ref f 'name)))
  (for ([code (in-list (hash-ref f 'waitingExit))])
    (unless (<= 1 code 255)
      (fail "~a: #:waiting-exit codes must be between 1 and 255 (got ~a)" who code)))
  (define journal (hash-ref f 'journal))
  (unless (string=? journal "")
    (when (or (regexp-match? #rx"^/" journal) (member ".." (string-split journal "/")))
      (fail "~a: #:journal must be a path relative to the run's worktree without `..` or a leading `/` (got ~s)"
            who journal))
    (for ([m (in-list (regexp-match* #rx"{[^}]*}" journal))])
      (unless (string=? m "{adw_id}")
        (fail "~a: #:journal: unknown placeholder ~a; the only one is {adw_id}" who m))))
  f)

(define (compile-step s adw-name)
  (unless (and (pair? s) (symbol? (car s)))
    (fail "(adw ~a): bad step ~s; steps are ~a" adw-name s step-kinds))
  (define (named record kind default-name)
    (define rest (cdr s))
    (define has-name? (and (pair? rest) (symbol? (car rest))))
    (define name (cond [has-name? (symbol->string (car rest))]
                       [default-name default-name]
                       [else (fail "(adw ~a): (~a NAME …) needs a step name" adw-name kind)]))
    (define who (format "(adw ~a) step (~a ~a)" adw-name kind name))
    (define-values (pos kws) (split-kwargs (if has-name? (cdr rest) rest) who))
    (unless (null? pos) (fail "~a: unexpected ~s" who (car pos)))
    (compile-fields (rec-fields record) kws who
                    #:init (hasheq 'kind (symbol->string kind) 'name name)))
  (case (car s)
    [(run) (named 'step-run 'run #f)]
    [(check) (named 'step-check 'check #f)]
    [(script) (compile-script s adw-name named)]
    [(review) (named 'step-review 'review "review")]
    [(ship) (named 'step-ship 'ship "ship")]
    [(compose)
     (unless (and (= (length s) 2) (symbol? (cadr s)))
       (fail "(adw ~a): expected (compose ADW-NAME), got ~s" adw-name s))
     (note-ref! 'adw (symbol->string (cadr s)) (format "(adw ~a) (compose ~a)" adw-name (cadr s)))
     (hasheq 'kind "compose" 'adw (symbol->string (cadr s)))]
    [(fan-out) (compile-fan-out s adw-name named)]
    [else (fail "(adw ~a): unknown step ~a; steps are ~a" adw-name (car s) step-kinds)]))

(define (compile-adw datum firsts)
  (unless (and (>= (length datum) 2) (symbol? (cadr datum)))
    (fail "(adw …): expected (adw NAME #:field value … (steps STEP …))"))
  (define name (cadr datum))
  (define who (format "(adw ~a)" name))
  (define-values (pos kws) (split-kwargs (cddr datum) who))
  (unless (and (= (length pos) 1) (pair? (car pos)) (eq? (caar pos) 'steps))
    (fail "~a: expected exactly one (steps STEP …) form" who))
  (when (null? (cdar pos)) (fail "~a: (steps) is empty" who))
  ;; PITER defaults: first declared option of each family.
  (define (with-default kws kw v) (if (assq kw kws) kws (append kws (list (cons kw v)))))
  (define kws*
    (for/fold ([k kws]) ([d (in-list firsts)])
      (with-default k (car d) (cdr d))))
  (hash-set (compile-fields (rec-fields 'adw) kws* who
                            #:init (hasheq 'name (symbol->string name)))
            'steps (for/list ([s (in-list (cdar pos))]) (compile-step s name))))

(define (expand-compose adws)
  (define by-name (for/hash ([a (in-list adws)]) (values (hash-ref a 'name) a)))
  (define (steps-of name seen)
    (when (member name seen)
      (fail "(compose ~a): composition cycle through ~a" name (names (reverse (cons name seen)))))
    (define a (hash-ref by-name name #f))
    (unless a (fail "(compose ~a): no such adw; adws are: ~a" name (names (hash-keys by-name))))
    (append*
     (for/list ([s (in-list (hash-ref a 'steps))])
       (if (equal? (hash-ref s 'kind) "compose")
           (steps-of (hash-ref s 'adw) (cons name seen))
           (list s)))))
  (for/list ([a (in-list adws)])
    (hash-set a 'steps (steps-of (hash-ref a 'name) '()))))

;; --- in-repo placement --------------------------------------------------------------------

;; Path components of a relative path, without "" and "." parts.
(define (components p) (filter (lambda (x) (not (member x '("" ".")))) (string-split p "/")))

(define (check-in-repo dir root)
  (define parts (components dir))
  (when (or (null? parts) (regexp-match? #rx"^/" dir) (member ".." parts))
    (fail "(layer in-repo): #:dir must be a relative directory inside the repository without `..` (got ~s), e.g. \"agentic\"" dir))
  (define expected (string-join (for/list ([_ (in-list parts)]) "..") "/"))
  (define up (components root))
  (unless (and (not (regexp-match? #rx"^/" root)) (= (length up) (length parts)) (andmap (lambda (x) (equal? x "..")) up))
    (fail "(layer in-repo #:dir ~s): (codebase #:root …) must lead from the layer directory back to the repository root, i.e. ~s (got ~s)"
          dir expected root)))

;; --- delivery: presets, stages, cycles ----------------------------------------------------

(define preset-names "trunk, sprint, gitflow, release-train, phase-gate")

(define (head-of c) (let ([d (cdr c)]) (and (pair? d) (symbol? (car d)) (car d))))
(define (stage-name-of c) (let ([d (cdr c)]) (and (eq? (head-of c) 'stage) (pair? (cdr d)) (cadr d))))

;; The indexed clause list with a (delivery PRESET …) clause replaced by the clauses it stands for
;; (each carrying the delivery clause's index), a declared (stage NAME …) of the same name moved into
;; its preset stage's place. Returns (values clauses delivery-record-or-#f origins), where origins
;; maps each preset-made clause datum (eq?) to "preset NAME".
(define (expand-delivery clauses)
  (define deliveries (filter (lambda (c) (eq? (head-of c) 'delivery)) clauses))
  (define origins (make-hasheq))
  (cond
    [(null? deliveries) (values clauses #f origins)]
    [else
     (when (pair? (cdr deliveries))
       (parameterize ([current-clause (car (cadr deliveries))])
         (fail "(delivery …) is declared twice; declare one preset and add or replace stages with (stage …)")))
     (define dc (car deliveries))
     (define-values (expanded record)
       (parameterize ([current-clause (car dc)])
         (define d (cdr dc))
         (unless (and (pair? (cdr d)) (symbol? (cadr d)))
           (fail "(delivery …): expected (delivery PRESET #:param value …); presets are: ~a" preset-names))
         (define p (find-preset 'delivery (cadr d)))
         (unless p (fail "unknown delivery preset ~a; presets are: ~a" (cadr d) preset-names))
         (define who (format "(delivery ~a)" (cadr d)))
         (define-values (pos kws) (split-kwargs (cddr d) who))
         (unless (null? pos) (fail "~a: unexpected ~s; parameters are given as #:keyword value" who (car pos)))
         (define params (compile-fields (preset-fields p) kws who))
         (define exp (preset-expansion p params))
         (values exp (hasheq 'preset (symbol->string (cadr d)) 'params params
                             'expansion (for/list ([x (in-list exp)]) (format "~s" x))))))
     (define origin (format "preset ~a" (hash-ref record 'preset)))
     (when (findf (lambda (x) (eq? (car x) 'cycle)) expanded)
       (define declared-cycle (findf (lambda (c) (eq? (head-of c) 'cycle)) clauses))
       (when declared-cycle
         (parameterize ([current-clause (car declared-cycle)])
           (fail "(cycle …): (delivery ~a) already declares a cycle; remove this (cycle …) or declare the stages yourself"
                 (hash-ref record 'preset)))))
     (define declared-stages
       (for/list ([c (in-list clauses)] #:when (symbol? (stage-name-of c))) c))
     (define moved '())
     (define replacement
       (for/list ([x (in-list expanded)])
         (define own (and (eq? (car x) 'stage)
                          (findf (lambda (c) (eq? (stage-name-of c) (cadr x))) declared-stages)))
         (cond [own (set! moved (cons own moved)) own]
               [else (hash-set! origins x origin) (cons (car dc) x)])))
     (values (append* (for/list ([c (in-list clauses)])
                        (cond [(eq? c dc) replacement]
                              [(memq c moved) '()]
                              [else (list c)])))
             record origins)]))

(define cycle-placeholders '("{year}" "{week}" "{n}" "{date}" "{version}"))
(define (placeholders-in s) (regexp-match* #rx"{[^}]*}" s))

;; A branch name git accepts once its placeholders are filled (git check-ref-format, the common rules).
(define (valid-branch-pattern? s)
  (define b (regexp-replace* #rx"{[^}]*}" s "x1"))
  (not (or (string=? b "")
           (regexp-match? #rx"[.][.]|//|@[{]|[ ~^:?*\\[\\\\]" b)
           (regexp-match? #rx"^[/.]|[/.]$" b)
           (regexp-match? #rx"[.]lock$" b)
           (regexp-match? #rx"/[.]" b)
           (regexp-match? #rx"[\u0000-\u001f\u007f]" b))))

(define (stage-next s)
  (define into (hash-ref s 'into))
  (if (string=? into "") (hash-ref s 'from) into))

;; --- whole layer ------------------------------------------------------------------------

;; --- dashboard ----------------------------------------------------------------------------

(define (loopback-address? s)
  (or (member (string-downcase s) '("localhost" "localhost.localdomain" "::1" "[::1]"))
      (regexp-match? #px"^127\\.[0-9]{1,3}\\.[0-9]{1,3}\\.[0-9]{1,3}$" s)))

;; --- notifiers ----------------------------------------------------------------------------

;; #f when `s` is an environment variable name in capitals; else what it looks like, in words.
;; The value itself is never put in an error message: it may be the private thing.
(define (private-looking s)
  (cond [(regexp-match? #px"^[A-Z][A-Z0-9_]*$" s) #f]
        [(regexp-match? #rx"://" s) "looks like a URL"]
        [(regexp-match? #rx"@" s) "looks like an e-mail address"]
        [(regexp-match? #px"^[A-Za-z0-9-]+([.][A-Za-z0-9-]+)*[.]?(:[0-9]+)?$" s) "looks like a host name"]
        [else "is not a variable name (capitals, digits and _ only, starting with a letter)"]))

(define (compile-layer-datum datum)
  (parameterize ([pending-refs (box '())] [current-clause #f])
    (unless (and (pair? datum) (eq? (car datum) 'agentic-layer))
      (fail "expected (agentic-layer #:name \"…\" CLAUSE …)"))
    ;; walk the raw list so clause indices match the syntax object in main.rkt
    (define raw (cdr datum))
    (define-values (clauses0 kws)
      (let loop ([l raw] [i 0] [cl '()] [kws '()])
        (cond [(null? l) (values (reverse cl) (reverse kws))]
              [(keyword? (car l))
               (when (null? (cdr l)) (fail "agentic-layer: keyword ~a has no value" (car l)))
               (loop (cddr l) (+ i 2) cl (cons (cons (car l) (cadr l)) kws))]
              [else (loop (cdr l) (add1 i) (cons (cons i (car l)) cl) kws)])))
    (define-values (clauses delivery stage-origins) (expand-delivery clauses0))
    (define top
      (compile-fields (list (field 'name 'string '#:required "Layer name (usually the repository name).")
                            (field 'description 'string "" "One line.")
                            (field 'llm-txt 'string "LLM.txt" "Descriptor file the layer was derived from."))
                      kws "agentic-layer"))
    (unless (regexp-match? #px"^[a-z0-9][a-z0-9._-]*$" (hash-ref top 'name))
      (fail "agentic-layer #:name must be lowercase letters, digits, . _ - (got ~s)" (hash-ref top 'name)))

    (define options (make-hasheq))   ; family-name → list of option hashes (reverse order)
    (define clause-of (make-hash))   ; (cons kind-of-thing name) → clause index
    (define codebase #f)
    (define commands '())
    (define hops '())
    (define experts '())
    (define adw-clauses '())
    (define layer-clause #f)
    (define stages '())
    (define clause-heads
      (append (map family-name (all-families)) '(codebase command hop expert adw stage delivery include)))

    (for ([c (in-list clauses)])
      (parameterize ([current-clause (car c)])
        (define d (cdr c))
        (unless (and (pair? d) (symbol? (car d)))
          (fail "unexpected ~s; clauses are: ~a" d (names clause-heads)))
        (define fam (find-family (car d)))
        (cond
          [fam
           (define o (compile-option fam d))
           (define existing (hash-ref options (family-name fam) '()))
           (when (findf (lambda (x) (equal? (hash-ref x 'id) (hash-ref o 'id))) existing)
             (fail "(~a ~a): id ~a already used; add #:id to tell them apart"
                   (family-name fam) (hash-ref o 'kind) (hash-ref o 'id)))
           (when (and (memq (family-name fam) singleton-families) (pair? existing))
             (fail "(~a …): only one ~a may be declared" (family-name fam) (family-name fam)))
           (when (eq? (family-name fam) 'layer) (set! layer-clause (car c)))
           (when (eq? (family-name fam) 'dashboard) (hash-set! clause-of 'dashboard-clause (car c)))
           (when (eq? (family-name fam) 'budget) (hash-set! clause-of 'budget-clause (car c)))
           (when (eq? (family-name fam) 'cycle) (hash-set! clause-of 'cycle-clause (car c)))
           (when (eq? (family-name fam) 'agent) (hash-set! clause-of (cons 'agent (hash-ref o 'id)) (car c)))
           (when (eq? (family-name fam) 'notify) (hash-set! clause-of (cons 'notify (hash-ref o 'id)) (car c)))
           (hash-set! options (family-name fam) (cons o existing))]
          [(eq? (car d) 'codebase)
           (when codebase (fail "(codebase …) given twice"))
           (set! codebase (compile-codebase d))]
          [(memq (car d) '(command hop))
           (define r (compile-named (car d) d))
           (when (findf (lambda (x) (equal? (hash-ref x 'name) (hash-ref r 'name)))
                        (append commands hops))
             (fail "(~a ~a): name already used by another command or hop" (car d) (hash-ref r 'name)))
           (when (equal? (hash-ref r 'modelSet) "inherit")
             (fail "(~a ~a): #:model-set inherit is only for run steps; name a tier, e.g. base or heavy" (car d) (hash-ref r 'name)))
           (when (and (eq? (car d) 'command)
                      (equal? (hash-ref r 'template) "custom")
                      (string=? (hash-ref r 'file) ""))
             (fail "(command ~a): #:template custom needs #:file" (hash-ref r 'name)))
           (if (eq? (car d) 'command) (set! commands (cons r commands)) (set! hops (cons r hops)))]
          [(eq? (car d) 'expert) (set! experts (cons (compile-named 'expert d) experts))]
          [(eq? (car d) 'stage)
           (define st (hash-set (compile-named 'stage d) 'origin (hash-ref stage-origins d "declared")))
           (when (findf (lambda (x) (equal? (hash-ref x 'name) (hash-ref st 'name))) stages)
             (fail "(stage ~a): declared twice" (hash-ref st 'name)))
           (hash-set! clause-of (cons 'stage (hash-ref st 'name)) (car c))
           (set! stages (cons st stages))]
          [(eq? (car d) 'adw) (set! adw-clauses (cons c adw-clauses))]
          [(eq? (car d) 'include) (fail include-unexpanded)]
          [else (fail "unknown clause (~a …); clauses are: ~a" (car d) (names clause-heads))])))

    (unless codebase (fail "missing (codebase #:root \"…\" #:languages (…) (cmd …) …)"))
    (when (null? adw-clauses) (fail "declare at least one (adw NAME … (steps …))"))

    ;; family defaults
    (for ([fam (in-list (all-families))])
      (when (and (family-default-kind fam) (null? (hash-ref options (family-name fam) '())))
        (hash-set! options (family-name fam) (list (default-option fam)))))
    (define (opts fname) (reverse (hash-ref options fname '())))
    (define (first-id fname) (string->symbol (hash-ref (car (opts fname)) 'id)))

    (define firsts
      (list (cons '#:input (list (first-id 'prompt-input)))
            (cons '#:trigger (list (first-id 'trigger)))
            (cons '#:environment (list (first-id 'environment)))
            (cons '#:review (first-id 'review))
            (cons '#:agent (first-id 'agent))))

    (define adws-raw
      (for/list ([c (in-list (reverse adw-clauses))])
        (parameterize ([current-clause (car c)])
          (define a (compile-adw (cdr c) firsts))
          (when (hash-ref clause-of (hash-ref a 'name) #f)
            (fail "(adw ~a): declared twice" (hash-ref a 'name)))
          (hash-set! clause-of (hash-ref a 'name) (car c))
          a)))
    (define adws (expand-compose adws-raw))

    ;; ---- reference resolution -----------------------------------------------------------
    (define (ids-of target)
      (case target
        [(command) (map (lambda (c) (hash-ref c 'name)) (reverse commands))]
        [(command-or-hop) (map (lambda (c) (hash-ref c 'name)) (append (reverse commands) (reverse hops)))]
        [(cmd) (map (lambda (c) (hash-ref c 'name)) (hash-ref codebase 'commands))]
        [(adw) (map (lambda (a) (hash-ref a 'name)) adws)]
        [(stage) (map (lambda (st) (hash-ref st 'name)) (reverse stages))]
        [else (map (lambda (o) (hash-ref o 'id)) (opts target))]))
    (for ([r (in-list (reverse (unbox (pending-refs))))])
      (define-values (target id who clause) (apply values r))
      (define known (ids-of target))
      ;; a stage named while none is declared is reported by the delivery rules below, in their words
      (unless (or (member id known) (and (eq? target 'stage) (null? known)))
        (parameterize ([current-clause clause])
          (fail "~a: `~a` is not a declared ~a; declared: ~a" who id
                (case target [(cmd) "codebase cmd"] [(command-or-hop) "command or hop"] [else target])
                (if (null? known) "(none)" (names known))))))

    ;; ---- semantic rules -----------------------------------------------------------------
    (define (opt fname id) (findf (lambda (o) (equal? (hash-ref o 'id) id)) (opts fname)))
    (let ([placement (car (opts 'layer))])
      (when (equal? (hash-ref placement 'kind) "in-repo")
        (parameterize ([current-clause layer-clause])
          (check-in-repo (hash-ref placement 'dir) (hash-ref codebase 'root)))))
    (let ([d (car (opts 'dashboard))])
      (unless (equal? (hash-ref d 'kind) "none")
        (parameterize ([current-clause (hash-ref clause-of 'dashboard-clause #f)])
          (define who (format "(dashboard ~a)" (hash-ref d 'kind)))
          (define port (hash-ref d 'port))
          (unless (<= 1 port 65535) (fail "~a: #:port must be 1–65535 (got ~a)" who port))
          (unless (>= (hash-ref d 'pollMs) 100)
            (fail "~a: #:poll-ms must be at least 100 (got ~a)" who (hash-ref d 'pollMs)))
          (define bind (hash-ref d 'bind))
          (when (and (not (loopback-address? bind)) (string=? (string-trim (hash-ref d 'tokenEnv)) ""))
            (fail "~a: #:bind ~s is not a loopback address, so anyone on that network could watch the agents' work; add #:token-env \"VARIABLE\" naming the environment variable that holds the access token (or bind to \"127.0.0.1\")"
                  who bind))
          (unless (regexp-match? #px"^([A-Za-z_][A-Za-z0-9_]*)?$" (hash-ref d 'tokenEnv))
            (fail "~a: #:token-env must be an environment variable name (got ~s)" who (hash-ref d 'tokenEnv))))))
    (let ([b (opts 'budget)])
      (when (pair? b)
        (parameterize ([current-clause (hash-ref clause-of 'budget-clause #f)])
          (for ([k '(runUsd stepUsd dayUsd)] [kw '("#:run-usd" "#:step-usd" "#:day-usd")])
            (define v (hash-ref (car b) k))
            (when (and (real? v) (not (> v 0)))
              (fail "(budget limits): ~a must be greater than 0 (got ~a); leave it out for no limit" kw v)))
          (define pct (hash-ref (car b) 'limitStopPercent))
          (unless (<= 0 pct 100)
            (fail "(budget limits): #:limit-stop-percent must be between 0 and 100 (got ~a); 0 or leaving it out turns the threshold off" pct))
          (when (and (equal? (hash-ref (car b) 'onLimit) "wait") (= pct 0))
            (fail "(budget limits): #:on-limit wait needs #:limit-stop-percent above 0 (there is no threshold to wait at)"))
          (unless (>= (hash-ref (car b) 'limitWaitMinutes) 1)
            (fail "(budget limits): #:limit-wait-minutes must be at least 1 (got ~a)" (hash-ref (car b) 'limitWaitMinutes))))))
    (for ([n (in-list (opts 'notify))] #:unless (equal? (hash-ref n 'kind) "none"))
      (parameterize ([current-clause (hash-ref clause-of (cons 'notify (hash-ref n 'id)) #f)])
        (define who (format "(notify ~a)" (hash-ref n 'kind)))
        (when (null? (hash-ref n 'events))
          (fail "~a: #:events is empty; list at least one of needs-human, failed, passed, shipped, or remove the notifier" who))
        (unless (<= 1 (hash-ref n 'timeout) 60)
          (fail "~a: #:timeout must be 1–60 seconds (got ~a)" who (hash-ref n 'timeout)))
        (when (equal? (hash-ref n 'kind) "email")
          (for ([k '(toEnv fromEnv smtpUrlEnv)] [kw '("#:to-env" "#:from-env" "#:smtp-url-env")])
            (define looks (private-looking (hash-ref n k)))
            (when looks
              (fail "~a: ~a takes the NAME of an environment variable in capitals, e.g. \"AL_NOTIFY_TO\", but the value given ~a. agentic.rkt is committed, so it must hold no private data: put the value itself in local.env in the layer directory (never committed) or in the environment, and write only the variable's name here"
                    who kw looks))))))
    (for ([ag (in-list (opts 'agent))] #:when (hash-has-key? ag 'models))
      (parameterize ([current-clause (hash-ref clause-of (cons 'agent (hash-ref ag 'id)) #f)])
        (define who (format "(agent ~a)" (hash-ref ag 'kind)))
        (for ([m (in-list (hash-ref ag 'models))])
          (define t (hash-ref m 'tier))
          (when (equal? t "inherit")
            (fail "~a #:models: `inherit` is not a tier name (on a run step it means \"the command's tier\")" who))
          (define e (hash-ref m 'effort))
          (unless (equal? e "default")
            (case (hash-ref ag 'kind)
              [("claude-code") (void)]
              [("codex") (when (equal? e "max")
                           (fail "~a #:models: tier `~a` has #:effort max, which codex does not offer; use low, medium, high or xhigh" who t))]
              [else (fail "~a #:models: tier `~a` has #:effort ~a, but a ~a agent has no way to pass it; remove #:effort" who t e (hash-ref ag 'kind))])))))
    (define forge (car (opts 'forge)))
    (define no-forge? (equal? (hash-ref forge 'kind) "none"))
    (define voice (let ([v (opts 'voice)]) (and (pair? v) (car v))))
    (define adw-by-name (for/hash ([x (in-list adws)]) (values (hash-ref x 'name) x)))
    (define (env-kinds-of x)
      (map (lambda (id) (hash-ref (opt 'environment id) 'kind)) (hash-ref x 'environment)))
    (define (steps-of-kind x k)
      (filter (lambda (s) (equal? (hash-ref s 'kind) k)) (hash-ref x 'steps)))
    ;; First fan-out cycle reachable from `start` (path is newest-first), as a list of names, or #f.
    (define (fan-out-cycle start path)
      (for/or ([s (in-list (steps-of-kind (hash-ref adw-by-name start) "fan-out"))])
        (define t (hash-ref s 'adw))
        (if (member t path) (reverse (cons t path)) (fan-out-cycle t (cons t path)))))
    (define (check-fan-outs a who steps review-i ship-i env-kinds)
      (for ([s (in-list steps)] [i (in-naturals)] #:when (equal? (hash-ref s 'kind) "fan-out"))
        (define parent (hash-ref a 'name))
        (define child-name (hash-ref s 'adw))
        (define fwho (format "~a: fan-out ~a" who (hash-ref s 'name)))
        (unless (member "worktree" env-kinds)
          (fail "~a needs a `worktree` environment in the parent's stack: the winner's commits are merged into the run's worktree" fwho))
        (when (equal? child-name parent)
          (fail "~a: #:adw must name another ADW, not `~a` itself; declare a separate child ADW (plan → build → check → commit)"
                fwho parent))
        (define cycle (fan-out-cycle child-name (list child-name parent)))
        (when cycle
          (fail "~a: cycle ~a through fan-out/compose; a child ADW must not fan out back into its parent"
                fwho (string-join cycle " → ")))
        (define child (hash-ref adw-by-name child-name))
        (unless (null? (steps-of-kind child "ship"))
          (fail "~a: child ADW `~a` contains a (ship) step; candidates must not ship — remove (review)/(ship) from `~a` and let the parent review and ship the winner"
                fwho child-name child-name))
        (unless (null? (steps-of-kind child "fan-out"))
          (fail "~a: child ADW `~a` contains a (fan-out) step; fan-out does not nest — use a child ADW without (fan-out)"
                fwho child-name))
        (unless (member "worktree" (env-kinds-of child))
          (fail "~a: child ADW `~a` needs a `worktree` environment so each candidate commits on its own branch; add it to `~a`'s #:environment"
                fwho child-name child-name))
        (when (or (and review-i (< review-i i)) (and ship-i (< ship-i i)))
          (fail "~a must come before the ADW's (review) and (ship) steps: they judge and ship the merged winner"
                fwho))))

    (define (command-tier name)
      (define c (findf (lambda (x) (equal? (hash-ref x 'name) name)) (append commands hops)))
      (if c (hash-ref c 'modelSet) "base"))
    (define (hop? name) (findf (lambda (x) (equal? (hash-ref x 'name) name)) hops))
    (define (agent-tiers agent)
      (and (hash-has-key? agent 'models) (map (lambda (m) (hash-ref m 'tier)) (hash-ref agent 'models))))
    ;; every tier a workflow can use must be declared on the agent that runs it
    (define (check-tiers who ag rv steps)
      (define (use agent-id tier why)
        (define agent (opt 'agent agent-id))
        (define tiers (agent-tiers agent))
        (when (and tiers (not (member tier tiers)))
          (fail "~a: ~a uses model tier `~a`, but agent `~a` declares the tiers: ~a; declare it, e.g. (agent ~a #:models ((~a \"MODEL-ID\")) …), or use one of those"
                who why tier agent-id (names tiers) (hash-ref agent 'kind) tier)))
      (define adw-agent (hash-ref ag 'id))
      (for ([st (in-list steps)])
        (define name (hash-ref st 'name))
        (case (hash-ref st 'kind)
          [("run")
           (define step-tier (hash-ref st 'modelSet))
           (define agent-id (if (string=? (hash-ref st 'agent) "") adw-agent (hash-ref st 'agent)))
           (if (equal? step-tier "inherit")
               (use agent-id (command-tier (hash-ref st 'command))
                    (format "step `~a` (the #:model-set of ~a `~a`)" name
                            (if (hop? (hash-ref st 'command)) "hop" "command") (hash-ref st 'command)))
               (use agent-id step-tier (format "step `~a` (its #:model-set)" name)))]
          [("check")
           (unless (string=? (hash-ref st 'resolve) "")
             (use adw-agent (command-tier (hash-ref st 'resolve))
                  (format "check `~a` (the #:model-set of its resolve command `~a`)" name (hash-ref st 'resolve))))]
          [("review")
           (when (member (hash-ref rv 'kind) '("agent" "auto"))
             (use (if (string=? (hash-ref rv 'agent "") "") adw-agent (hash-ref rv 'agent))
                  (command-tier (hash-ref rv 'command))
                  (format "review `~a` (the #:model-set of review command `~a`)" (hash-ref rv 'id) (hash-ref rv 'command)))
             (for ([k '(patch implement)])
               (unless (string=? (hash-ref st k) "")
                 (use adw-agent (command-tier (hash-ref st k))
                      (format "step `~a` (the #:model-set of its #:~a command `~a`)" name k (hash-ref st k))))))]
          [else (void)])))

    ;; ---- delivery: stages and the cycle ----------------------------------------------------
    (define stage-list (reverse stages))
    (define (stage-by-name n) (findf (lambda (st) (equal? (hash-ref st 'name) n)) stage-list))
    (define (in-stage name thunk)
      (parameterize ([current-clause (hash-ref clause-of (cons 'stage name) #f)]) (thunk)))
    (define cyc (let ([c (opts 'cycle)]) (and (pair? c) (not (equal? (hash-ref (car c) 'kind) "none")) (car c))))
    (define cycle-stage (and cyc (hash-ref cyc 'stage)))
    (define final-stage
      (and (pair? stage-list)
           (let ()
             ;; every stage reaches the final stage by following #:into (default #:from), without a cycle
             (for ([st (in-list stage-list)])
               (let walk ([cur st] [path (list (hash-ref st 'name))])
                 (define next (stage-next cur))
                 (unless (string=? next "")
                   (when (member next path)
                     (in-stage (hash-ref st 'name)
                               (lambda () (fail "stages ~a form a cycle; following #:into (default #:from) must end at the one final stage (no #:from, no #:into)"
                                                (string-join (reverse (cons next path)) " → ")))))
                   (walk (stage-by-name next) (cons next path)))))
             (define finals (filter (lambda (st) (string=? (stage-next st) "")) stage-list))
             (when (> (length finals) 1)
               (in-stage (hash-ref (cadr finals) 'name)
                         (lambda () (fail "two final stages: ~a; exactly one stage has neither #:from nor #:into (the one that holds (codebase #:default-branch …)); give the others #:from"
                                          (names (map (lambda (st) (hash-ref st 'name)) finals))))))
             (car finals))))
    (when cyc
      (parameterize ([current-clause (hash-ref clause-of 'cycle-clause #f)])
        (define who (format "(cycle ~a)" (hash-ref cyc 'kind)))
        (when (null? stage-list)
          (fail "~a: declare the stage it repeats: (stage ~a #:branch \"…/{cycle}\" #:from …), or use a (delivery …) preset" who cycle-stage))
        (when (eq? (stage-by-name cycle-stage) final-stage)
          (fail "~a: #:stage ~a is the final stage, which never repeats; name the stage that is cut anew each cycle" who cycle-stage))
        (define b (hash-ref (stage-by-name cycle-stage) 'branch))
        (define release? (equal? (hash-ref cyc 'kind) "release"))
        (unless (or (regexp-match? #rx"{cycle}" b) (and release? (regexp-match? #rx"{version}" b)))
          (fail "~a: the #:branch ~s of stage ~a must contain {cycle}~a, so each cycle gets its own branch"
                who b cycle-stage (if release? " or {version}" "")))
        (when (and (not release?) (not (<= 1 (hash-ref cyc 'lengthDays) 366)))
          (fail "~a: #:length-days must be 1–366 (got ~a)" who (hash-ref cyc 'lengthDays)))
        (define n (hash-ref cyc 'name))
        (for ([ph (in-list (placeholders-in n))])
          (unless (member ph cycle-placeholders)
            (fail "~a: #:name ~s: unknown placeholder ~a; placeholders are ~a" who n ph (string-join cycle-placeholders " "))))
        (when (and release? (not (regexp-match? #rx"{version}" n)))
          (fail "~a: #:name ~s must contain {version}: a release cycle is named after the version it is started with" who n))))
    (when final-stage
      (define default-branch (hash-ref codebase 'defaultBranch))
      (in-stage (hash-ref final-stage 'name)
                (lambda ()
                  (define b (hash-ref final-stage 'branch))
                  (unless (null? (placeholders-in b))
                    (fail "(stage ~a): the final stage's #:branch ~s has a placeholder; it is one fixed branch, (codebase #:default-branch …)"
                          (hash-ref final-stage 'name) b))
                  (unless (equal? b default-branch)
                    (fail "(stage ~a): the final stage's #:branch ~s must equal (codebase #:default-branch …) ~s"
                          (hash-ref final-stage 'name) b default-branch))))
      (for ([st (in-list stage-list)])
        (define name (hash-ref st 'name))
        (in-stage name
                  (lambda ()
                    (define b (hash-ref st 'branch))
                    (define who (format "(stage ~a)" name))
                    (for ([ph (in-list (placeholders-in b))])
                      (unless (member ph delivery-placeholders)
                        (fail "~a: #:branch ~s: unknown placeholder ~a; placeholders are ~a" who b ph (string-join delivery-placeholders " ")))
                      (when (and (member ph '("{cycle}" "{version}")) (not (equal? name cycle-stage)))
                        (fail "~a: #:branch ~s: ~a is filled by the cycle, so it is only on the stage of (cycle …) #:stage; ~a"
                              who b ph (if cycle-stage (format "that is `~a`" cycle-stage) "declare (cycle sprint|release #:stage …) or use {date}"))))
                    (unless (valid-branch-pattern? b)
                      (fail "~a: #:branch ~s is not a valid git branch name (no spaces, `..`, `//`, ~~ ^ : ? * [ \\, leading or trailing / or ., `.lock` at the end, or a component starting with .)" who b))
                    (define twin (findf (lambda (o) (and (not (eq? o st)) (equal? (hash-ref o 'branch) b))) stage-list))
                    (when twin
                      (fail "~a: branch ~s is used by two stages, ~a and ~a; a stage is one branch" who b (hash-ref twin 'name) name))
                    (when (and (equal? (hash-ref st 'entry) "fast-forward") (not (equal? (hash-ref forge 'kind) "none")))
                      (fail "~a: #:entry fast-forward is not possible with (forge ~a …): a pull request is merged by merge, squash or rebase; use one of those"
                            who (hash-ref forge 'kind)))))))
    (define all-four '("chore" "bug" "feature" "patch"))
    ;; (class . stage) for every class the ADW can produce
    (define (stage-routes a)
      (define to (hash-ref a 'deliverTo ""))
      (for/list ([k (in-list (if (null? (hash-ref a 'classes)) all-four (hash-ref a 'classes)))])
        (cons k (if (string=? to "")
                    (or (findf (lambda (st) (member k (hash-ref st 'accepts))) stage-list) final-stage)
                    (stage-by-name to)))))

    (for ([a (in-list adws)])
      (parameterize ([current-clause (hash-ref clause-of (hash-ref a 'name))])
        (define who (format "(adw ~a)" (hash-ref a 'name)))
        (define steps (hash-ref a 'steps))
        (define step-names (map (lambda (s) (hash-ref s 'name)) steps))
        (define dup (check-duplicates step-names))
        (when dup (fail "~a: step name `~a` used twice (after compose expansion)" who dup))
        (define kinds (map (lambda (s) (hash-ref s 'kind)) steps))
        (define (index-of-kind k) (index-of kinds k))
        (define review-i (index-of-kind "review"))
        (define ship-i (index-of-kind "ship"))
        (when (and ship-i (or (not review-i) (> review-i ship-i)))
          (fail "~a: (ship) must come after a (review) step" who))
        (define rv (opt 'review (hash-ref a 'review)))
        (define deliver-to (hash-ref a 'deliverTo))
        (when (and (null? stage-list) (not (string=? deliver-to "")))
          (fail "~a: #:deliver-to needs declared stages: add (stage …) clauses or a (delivery PRESET …) clause, or remove #:deliver-to ~a"
                who deliver-to))
        (when (pair? stage-list)
          (define routes (stage-routes a))
          (when (and ship-i (equal? (hash-ref rv 'kind) "auto") (member (hash-ref rv 'ship) '("merge" "deploy")))
            (for ([r (in-list routes)])
              (unless (hash-ref (cdr r) 'agentsMayMerge)
                (define open (findf (lambda (st) (hash-ref st 'agentsMayMerge)) stage-list))
                (fail "~a: review `~a` ships without a human, but class ~a is delivered to stage `~a` (branch ~a), where agents may not merge; deliver to a stage with #:agents-may-merge #t~a or use a human or agent review"
                      who (hash-ref rv 'id) (car r) (hash-ref (cdr r) 'name) (hash-ref (cdr r) 'branch)
                      (if open (format " (e.g. #:deliver-to ~a)" (hash-ref open 'name)) "")))))
          (when (and ship-i (not (string=? deliver-to "")))
            (for ([r (in-list routes)])
              (unless (member (car r) (hash-ref (cdr r) 'accepts))
                (fail "~a: #:deliver-to ~a, but stage `~a` does not accept class ~a (its #:accepts is (~a)); add the class to the stage's #:accepts or narrow the ADW's #:classes"
                      who deliver-to deliver-to (car r) (string-join (hash-ref (cdr r) 'accepts) " "))))))
        (when (equal? (hash-ref rv 'kind) "auto")
          (define shipping? (member (hash-ref rv 'ship) '("merge" "deploy")))
          (when shipping?
            (define check-i (index-of-kind "check"))
            (unless (and check-i review-i (< check-i review-i))
              (fail "~a: review `~a` ships without a human, so the ADW needs a (check …) step before its (review) step"
                    who (hash-ref rv 'id)))
            (when (null? (hash-ref rv 'requireChecks))
              (fail "~a: review `~a` ships without a human; #:require-checks must list at least one codebase cmd"
                    who (hash-ref rv 'id))))
          (when (and (equal? (hash-ref rv 'ship) "deploy") (string=? (hash-ref rv 'deployCmd) ""))
            (if (null? stage-list)
                (fail "~a: review `~a` has #:ship deploy but no #:deploy-cmd" who (hash-ref rv 'id))
                (for ([r (in-list (stage-routes a))])
                  (when (string=? (hash-ref (cdr r) 'deployCmd) "")
                    (fail "~a: review `~a` has #:ship deploy but no #:deploy-cmd, and stage `~a` it delivers to has none either; add #:deploy-cmd to the review or to the stage"
                          who (hash-ref rv 'id) (hash-ref (cdr r) 'name)))))))
        (for ([id (in-list (hash-ref a 'input))])
          (define o (opt 'prompt-input id))
          (when (and no-forge? (equal? (hash-ref o 'kind) "issue"))
            (fail "~a: prompt-input `~a` reads forge issues, but no (forge github|forgejo …) is declared" who id))
          (when (and (not voice) (equal? (hash-ref o 'kind) "voice"))
            (fail "~a: prompt-input `~a` needs a (voice kokoro …) declaration" who id)))
        (for ([id (in-list (hash-ref a 'trigger))])
          (define o (opt 'trigger id))
          (define k (hash-ref o 'kind))
          (when (and no-forge?
                     (or (equal? k "forge-actions")
                         (and (equal? k "webhook")
                              (ormap (lambda (e) (regexp-match? #rx"^issue-" e)) (hash-ref o 'events)))))
            (fail "~a: trigger `~a` listens to forge events, but no (forge github|forgejo …) is declared (use #:events (payload) for a forge-less webhook)" who id))
          (when (and (not voice) (equal? k "voice"))
            (fail "~a: trigger `~a` needs a (voice kokoro …) declaration" who id)))
        (define env-kinds
          (map (lambda (id) (hash-ref (opt 'environment id) 'kind)) (hash-ref a 'environment)))
        (when (null? env-kinds) (fail "~a: #:environment is empty" who))
        (define dup-env (check-duplicates env-kinds))
        (when dup-env (fail "~a: environment stack has two `~a` options" who dup-env))
        (when (and (member "host" env-kinds) (member "worktree" env-kinds))
          (fail "~a: `host` and `worktree` environments cannot be combined" who))
        (when (and (not (member "worktree" env-kinds))
                   (ormap (lambda (k) (member k '("check" "ship" "script"))) kinds))
          (fail "~a: check, script and ship steps need a `worktree` environment in the stack so the application checkout is never touched" who))
        (for ([st (in-list steps)] #:when (equal? (hash-ref st 'kind) "check"))
          (define in (hash-ref st 'in ""))
          (unless (string=? in "")
            (define k (hash-ref (opt 'environment in) 'kind))
            (unless (member k '("podman" "nix-devshell"))
              (fail "~a: check `~a` #:in `~a` is a `~a` environment; a check runs in the run's worktree inside a podman or nix-devshell environment"
                    who (hash-ref st 'name) in k)))
          (when (< (hash-ref st 'maxSkipped -1) -1)
            (fail "~a: check `~a` #:max-skipped must be -1 (no limit) or more, got ~a"
                  who (hash-ref st 'name) (hash-ref st 'maxSkipped))))
        (check-fan-outs a who steps review-i ship-i env-kinds)
        (when (and (equal? (hash-ref a 'changes) "layer")
                   (not (member (hash-ref rv 'kind) '("human" "agent"))))
          (fail "~a: #:changes layer needs a review option of kind human or agent (got `~a`, kind ~a): a change to how agents work always passes a person; there is no zero-touch mode for layer changes"
                who (hash-ref rv 'id) (hash-ref rv 'kind)))
        (define ag (opt 'agent (hash-ref a 'agent)))
        (define podman
          (for/first ([id (in-list (hash-ref a 'environment))]
                      #:when (equal? (hash-ref (opt 'environment id) 'kind) "podman"))
            (opt 'environment id)))
        ;; the ADW's agent and every step-level agent (#:agent on a run step) obey the same rule
        (define step-agent-ids
          (for/list ([st (in-list steps)]
                     #:when (and (equal? (hash-ref st 'kind) "run")
                                 (not (string=? (hash-ref st 'agent "") ""))))
            (hash-ref st 'agent)))
        (define reviewer-ids
          (if (and (member (hash-ref rv 'kind) '("agent" "auto")) review-i
                   (not (string=? (hash-ref rv 'agent "") "")))
              (list (hash-ref rv 'agent))
              '()))
        (for ([agent-id (in-list (remove-duplicates (append (list (hash-ref ag 'id)) step-agent-ids reviewer-ids)))])
          (define used (opt 'agent agent-id))
          (when (and (equal? (hash-ref used 'kind) "claude-code")
                     (equal? (hash-ref used 'permissionMode) "bypassPermissions")
                     (not (and podman (hash-ref podman 'agentInside))))
            (fail "~a: agent `~a` uses bypassPermissions, which is only accepted when the agent itself runs inside a podman environment: add (environment podman #:agent-inside #t …) to the ADW's #:environment" who agent-id)))
        (check-tiers who ag rv steps)
        (when (and (equal? (hash-ref rv 'kind) "auto")
                   (member (hash-ref rv 'ship) '("merge" "deploy"))
                   (not podman))
          (fail "~a: review `~a` ships without a human, so the ADW's #:environment must include a podman option (declare (environment podman …) and list its id)" who (hash-ref rv 'id)))))

    (define (single fname) (let ([l (opts fname)]) (if (null? l) 'null (car l))))
    (define ir-out
      (hash-set* top
               'schemaVersion "agentic-layer-v1"
               'codebase codebase
               'layer (single 'layer)
               'forge (single 'forge)
               'storage (single 'storage)
               'voice (single 'voice)
               'concurrency (single 'concurrency)
               'dashboard (single 'dashboard)
               'budget (single 'budget)
               'notifiers (opts 'notify)
               'agents (opts 'agent)
               'promptInputs (opts 'prompt-input)
               'triggers (opts 'trigger)
               'environments (opts 'environment)
               'reviews (opts 'review)
               'artefacts (opts 'artefact)
               'services (opts 'service)
               'commands (reverse commands)
               'hops (reverse hops)
               'experts (reverse experts)
               'adws adws))
    ;; no stage: exactly the IR of before (no stages, cycle or delivery key, no deliverTo)
    (if (null? stage-list)
        (hash-set ir-out 'adws (for/list ([a (in-list adws)]) (hash-remove a 'deliverTo)))
        (hash-set* ir-out
                   'stages stage-list
                   'cycle (single 'cycle)
                   'delivery (or delivery 'null)))))
