;; a cmd whose shell command is not a string
(cmd lint "ruff check")
(cmd typecheck "mypy" #:timeout "long")
