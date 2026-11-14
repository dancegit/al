#lang racket/base
;; Deterministic JSON: object keys sorted, 2-space indent. Same input → same bytes.
(require json racket/list)
(provide json-pretty)

(define (json-pretty v)
  (define out (open-output-string))
  (let loop ([v v] [ind 0])
    (define pad (make-string (+ ind 2) #\space))
    (define (items xs emit)
      (for ([x (in-list xs)] [i (in-naturals)])
        (write-string pad out)
        (emit x)
        (write-string (if (< i (sub1 (length xs))) ",\n" "\n") out)))
    (cond
      [(hash? v)
       (define keys (sort (hash-keys v) symbol<?))
       (cond [(null? keys) (write-string "{}" out)]
             [else (write-string "{\n" out)
                   (items keys (lambda (k)
                                 (write-json (symbol->string k) out)
                                 (write-string ": " out)
                                 (loop (hash-ref v k) (+ ind 2))))
                   (write-string (make-string ind #\space) out)
                   (write-string "}" out)])]
      [(null? v) (write-string "[]" out)]
      [(list? v)
       (write-string "[\n" out)
       (items v (lambda (x) (loop x (+ ind 2))))
       (write-string (make-string ind #\space) out)
       (write-string "]" out)]
      [else (write-json v out)]))
  (get-output-string out))
