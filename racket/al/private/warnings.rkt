#lang racket/base

;; Warnings about declarations that compile but have no effect (or not the one meant). They never
;; change the IR and never fail a compile: `racket agentic.rkt` prints them on stderr as
;; `warning: …`, and `al compile`, `al gen` and `al check` pass them on.
;;
;; layer-warnings : IR (hasheq, as compile-layer-datum returns it) -> (listof string)

(require racket/list racket/string)

(provide layer-warnings)

(define (field h key [default '()])
  (if (hash? h) (hash-ref h key default) default))

(define (podman-decl option)
  (format "(environment podman #:id ~a)" (field option 'id "")))

;; python312 -> python, nodejs_20 -> nodejs, python3.12 -> python
(define (normalise name)
  (let loop ([s (string-downcase name)])
    (define next (regexp-replace #rx"[-_.]?[0-9]+$" s ""))
    (if (or (equal? next s) (equal? next "")) s (loop next))))

(define aliases
  (hash "nodejs" '("nodejs" "node") "node" '("node" "nodejs")
        "python" '("python" "python3") "python3" '("python3" "python")))

;; what a toolchain feature brings, by the names a dev shell would list
(define feature-packages
  (hash "browser" '("chromium") "video" '("ffmpeg") "build-essentials" '("gcc" "gnumake" "pkg-config")
        "docker-cli" '("docker") "podman-cli" '("podman")))

;; `toolchain` is the (toolchain nix …) the podman option uses, or #f: then the image is built
;; from the toolchain's packages and features (and the #:setup lines), not from a base image.
(define (provided? package podman [toolchain #f])
  (define names (hash-ref aliases (normalise package) (list (normalise package))))
  ;; the base image counts too: docker.io/library/python:3.12-slim provides python
  (define provided
    (map string-downcase
         (if toolchain
             (append (field toolchain 'packages)
                     (append* (map (lambda (f) (hash-ref feature-packages f '())) (field toolchain 'features)))
                     (field podman 'setup))
             (append (list (field podman 'baseImage "")) (field podman 'packages) (field podman 'setup)))))
  (for*/or ([name (in-list names)] [line (in-list provided)])
    (string-contains? line name)))

;; every non-empty #:in of a check step
(define (check-ins adw)
  (for/list ([st (in-list (field adw 'steps))]
             #:when (equal? (field st 'kind "") "check")
             #:unless (equal? (field st 'in "") ""))
    st))

(define (unused-environments ir)
  (define used (append* (map (lambda (adw) (append (field adw 'environment)
                                                   (map (lambda (st) (field st 'in "")) (check-ins adw))))
                             (field ir 'adws))))
  (for/list ([option (in-list (field ir 'environments))]
             #:unless (equal? (field option 'kind "") "home-manager")
             #:unless (member (field option 'id "") used))
    (format "environment `~a` is used by no workflow" (field option 'id ""))))

(define (credentials-outside ir)
  (for/list ([option (in-list (field ir 'environments))]
             #:when (equal? (field option 'kind "") "podman")
             #:when (pair? (field option 'credentials))
             #:unless (eq? (field option 'agentInside #t) #t))
    (format "#:credentials on ~a has no effect without #:agent-inside #t" (podman-decl option))))

(define (scope-command ir)
  (for/list ([option (in-list (field ir 'environments))]
             #:when (equal? (field option 'kind "") "podman")
             #:when (equal? (field option 'scope "run") "command")
             #:when (or (pair? (field option 'publish)) (pair? (field option 'services))))
    (define what (string-join (append (if (pair? (field option 'publish)) '("#:publish") '())
                                      (if (pair? (field option 'services)) '("#:services") '()))
                              " and "))
    (format "~a on ~a with #:scope command: each command gets a fresh container, so a server one command starts is gone before the next command or a host-side tool can reach it; use #:scope run"
            what (podman-decl option))))

(define (nix-not-in-container ir)
  (define by-id (for/hash ([option (in-list (field ir 'environments))]) (values (field option 'id "") option)))
  (define stacks
    (for/fold ([acc '()]) ([adw (in-list (field ir 'adws))])
      (define options (filter values (map (lambda (id) (hash-ref by-id id #f)) (field adw 'environment))))
      (define nix (findf (lambda (o) (equal? (field o 'kind "") "nix-devshell")) options))
      (define podman (findf (lambda (o) (equal? (field o 'kind "") "podman")) options))
      (cond [(not (and nix podman)) acc]
            ;; an image from the repository's own Containerfile or dev container: al cannot know what it provides
            [(or (non-empty-string? (field podman 'containerfile "")) (non-empty-string? (field podman 'devcontainer "")))
             acc]
            [(assoc (cons nix podman) acc)
             => (lambda (entry) (cons (cons (car entry) (append (cdr entry) (list (field adw 'name ""))))
                                      (remove entry acc)))]
            [else (cons (cons (cons nix podman) (list (field adw 'name ""))) acc)])))
  (for*/list ([entry (in-list (reverse stacks))]
              [nix (in-value (car (car entry)))]
              [podman (in-value (cdr (car entry)))]
              [toolchain (in-value (findf (lambda (t) (equal? (field t 'id "") (field podman 'toolchain "")))
                                          (field ir 'toolchains)))]
              [missing (in-value (filter (lambda (p) (not (provided? p podman toolchain))) (field nix 'packages)))]
              #:when (pair? missing))
    (format "workflow ~a runs its commands in ~a, which does not provide what (environment nix-devshell #:id ~a) declares: ~a"
            (string-join (cdr entry) ", ") (podman-decl podman) (field nix 'id "")
            (string-join missing ", "))))

(define (check-outside-sandbox ir)
  (define by-id (for/hash ([option (in-list (field ir 'environments))]) (values (field option 'id "") option)))
  (define reviews (for/hash ([option (in-list (field ir 'reviews))]) (values (field option 'id "") option)))
  (for*/list ([adw (in-list (field ir 'adws))]
              [rv (in-value (hash-ref reviews (field adw 'review "") #f))]
              #:when (and rv (equal? (field rv 'kind "") "auto")
                          (member (field rv 'ship "") '("merge" "deploy")))
              [st (in-list (check-ins adw))]
              [option (in-value (hash-ref by-id (field st 'in "") #f))]
              #:unless (and option (equal? (field option 'kind "") "podman")))
    (format "workflow ~a ships without a person, but check ~a runs #:in ~a (a ~a environment), outside its podman sandbox"
            (field adw 'name "") (field st 'name "") (field st 'in "") (field option 'kind ""))))

(define (require-affected-ships ir)
  (for/list ([rv (in-list (field ir 'reviews))]
             #:when (equal? (field rv 'kind "") "auto")
             #:when (equal? (field rv 'require "all") "affected")
             #:when (member (field rv 'ship "") '("merge" "deploy")))
    (format "review `~a` has #:require affected and #:ship ~a: a required check no changed path touches is not run before the change ships without a person; a wrong #:covers then ships unproven code"
            (field rv 'id "") (field rv 'ship ""))))

(define (layer-warnings ir)
  (append (unused-environments ir) (credentials-outside ir) (scope-command ir) (nix-not-in-container ir)
          (check-outside-sandbox ir) (require-affected-ships ir)))
