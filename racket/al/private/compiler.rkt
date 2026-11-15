#lang racket/base

;; (agentic-layer …) datum  →  agentic-layer-v1 IR (a jsexpr of immutable hasheqs).
;;
;; Shape checking is entirely driven by the registry in vocab.rkt. On top of that the
;; compiler enforces the semantic rules (vocab.rkt `semantic-rules`): these are what make
;; an LLM-authored layer coherent rather than merely well-formed.

(require racket/list racket/string "meta.rkt" "vocab.rkt")

(provide compile-layer-datum (struct-out exn:al))

;; clause-index: index into (cdr datum) of the clause being compiled, or #f.
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

(define (coerce type v who)
  (define (bad what) (fail "~a: expected ~a, got ~s" who what v))
  (cond
    [(eq? type 'string) (if (string? v) v (bad "a string in double quotes"))]
    [(eq? type 'integer) (if (exact-integer? v) v (bad "an integer"))]
    [(eq? type 'boolean) (if (boolean? v) v (bad "#t or #f"))]
    [(eq? type 'symbol) (name->string v who)]
    [(and (pair? type) (eq? (car type) 'enum))
     (define s (cond [(symbol? v) v] [(string? v) (string->symbol v)] [else #f]))
     (if (and s (memq s (cdr type)))
         (symbol->string s)
         (bad (format "one of: ~a" (names (cdr type)))))]
    [(and (pair? type) (eq? (car type) 'list-of))
     (if (list? v)
         (for/list ([x (in-list v)]) (coerce (cadr type) x who))
         (bad "a parenthesised list, e.g. (a b)"))]
    [(and (pair? type) (memq (car type) '(ref optional-ref)))
     (define s (name->string v who))
     (unless (and (eq? (car type) 'optional-ref) (string=? s ""))
       (note-ref! (cadr type) s who))
     s]
    [else (error 'al "vocabulary bug: unknown field type ~s" type)]))

(define (compile-fields fields kws who #:init [init (hasheq)] #:extra [extra '()])
  (for ([p (in-list kws)])
    (unless (or (memq (car p) extra)
                (findf (lambda (f) (eq? (field-keyword f) (car p))) fields))
      (fail "~a: unknown field ~a; fields are: ~a" who (car p)
            (string-join (map (lambda (f) (format "~a" (field-keyword f))) fields) " "))))
  (for/fold ([h init]) ([f (in-list fields)])
    (define p (assq (field-keyword f) kws))
    (define fwho (format "~a ~a" who (field-keyword f)))
    (hash-set h (field-json-key f)
              (cond [p (coerce (field-type f) (cdr p) fwho)]
                    [(field-required? f)
                     (fail "~a: missing required field ~a — ~a" who (field-keyword f) (field-doc f))]
                    [else (coerce (field-type f) (field-default f) fwho)]))))

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
  (define cmds '())
  (define docs '())
  (define paths #f)
  (for ([s (in-list subs)])
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
      [else (fail "(codebase): unknown sub-clause ~a; expected cmd, paths or conditional-doc" (car s))]))
  (when (null? cmds)
    (fail "(codebase): declare at least one (cmd NAME \"…\"), typically install, test, lint, build"))
  (hash-set* base
             'commands (reverse cmds)
             'paths (or paths (compile-fields (rec-fields 'paths) '() "(paths)"))
             'conditionalDocs (reverse docs)))

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

(define step-kinds "run, check, review, ship, compose, fan-out")

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

;; --- whole layer ------------------------------------------------------------------------

(define (compile-layer-datum datum)
  (parameterize ([pending-refs (box '())] [current-clause #f])
    (unless (and (pair? datum) (eq? (car datum) 'agentic-layer))
      (fail "expected (agentic-layer #:name \"…\" CLAUSE …)"))
    ;; walk the raw list so clause indices match the syntax object in main.rkt
    (define raw (cdr datum))
    (define-values (clauses kws)
      (let loop ([l raw] [i 0] [cl '()] [kws '()])
        (cond [(null? l) (values (reverse cl) (reverse kws))]
              [(keyword? (car l))
               (when (null? (cdr l)) (fail "agentic-layer: keyword ~a has no value" (car l)))
               (loop (cddr l) (+ i 2) cl (cons (cons (car l) (cadr l)) kws))]
              [else (loop (cdr l) (add1 i) (cons (cons i (car l)) cl) kws)])))
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
    (define clause-heads
      (append (map family-name (all-families)) '(codebase command hop expert adw)))

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
           (hash-set! options (family-name fam) (cons o existing))]
          [(eq? (car d) 'codebase)
           (when codebase (fail "(codebase …) given twice"))
           (set! codebase (compile-codebase d))]
          [(memq (car d) '(command hop))
           (define r (compile-named (car d) d))
           (when (findf (lambda (x) (equal? (hash-ref x 'name) (hash-ref r 'name)))
                        (append commands hops))
             (fail "(~a ~a): name already used by another command or hop" (car d) (hash-ref r 'name)))
           (when (and (eq? (car d) 'command)
                      (equal? (hash-ref r 'template) "custom")
                      (string=? (hash-ref r 'file) ""))
             (fail "(command ~a): #:template custom needs #:file" (hash-ref r 'name)))
           (if (eq? (car d) 'command) (set! commands (cons r commands)) (set! hops (cons r hops)))]
          [(eq? (car d) 'expert) (set! experts (cons (compile-named 'expert d) experts))]
          [(eq? (car d) 'adw) (set! adw-clauses (cons c adw-clauses))]
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
        [else (map (lambda (o) (hash-ref o 'id)) (opts target))]))
    (for ([r (in-list (reverse (unbox (pending-refs))))])
      (define-values (target id who clause) (apply values r))
      (define known (ids-of target))
      (unless (member id known)
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
            (fail "~a: review `~a` has #:ship deploy but no #:deploy-cmd" who (hash-ref rv 'id))))
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
                   (ormap (lambda (k) (member k '("check" "ship"))) kinds))
          (fail "~a: check/ship steps need a `worktree` environment in the stack so the application checkout is never touched" who))
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
        (for ([agent-id (in-list (remove-duplicates (cons (hash-ref ag 'id) step-agent-ids)))])
          (define used (opt 'agent agent-id))
          (when (and (equal? (hash-ref used 'kind) "claude-code")
                     (equal? (hash-ref used 'permissionMode) "bypassPermissions")
                     (not (and podman (hash-ref podman 'agentInside))))
            (fail "~a: agent `~a` uses bypassPermissions, which is only accepted when the agent itself runs inside a podman environment: add (environment podman #:agent-inside #t …) to the ADW's #:environment" who agent-id)))
        (when (and (equal? (hash-ref rv 'kind) "auto")
                   (member (hash-ref rv 'ship) '("merge" "deploy"))
                   (not podman))
          (fail "~a: review `~a` ships without a human, so the ADW's #:environment must include a podman option (declare (environment podman …) and list its id)" who (hash-ref rv 'id)))))

    (define (single fname) (let ([l (opts fname)]) (if (null? l) 'null (car l))))
    (hash-set* top
               'schemaVersion "agentic-layer-v1"
               'codebase codebase
               'layer (single 'layer)
               'forge (single 'forge)
               'storage (single 'storage)
               'voice (single 'voice)
               'concurrency (single 'concurrency)
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
               'adws adws)))
