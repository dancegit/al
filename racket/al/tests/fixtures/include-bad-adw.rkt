;; the second clause uses a tier the agent does not declare
(command build #:template build)
(adw a
  (steps (run plan #:command plan)))
