#lang info

(define collection "al")
(define pkg-desc "#lang al — declarative agentic-layer DSL emitting agentic-layer-v1 JSON")
(define version "0.1.0")
(define deps '("base"))
(define build-deps '("rackunit-lib"))
(define compile-omit-paths '("tests/fixtures"))
;; tests/fixtures holds files that are invalid on purpose (they test the compiler's errors);
;; `raco test racket/al/tests` must not try to run them as tests.
(define test-omit-paths '("tests/fixtures"))
