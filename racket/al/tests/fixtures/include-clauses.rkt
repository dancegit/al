;; included at the top level by include-main.rkt
(command plan #:template plan-chore)
(adw a
  (steps (run plan #:command plan) (check lint #:cmd lint)))
