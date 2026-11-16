(stage build #:branch "phase/build" #:from main #:into verify #:accepts (feature bug chore patch))
(stage verify #:branch "phase/verify" #:from main #:into accept #:accepts () #:agents-may-merge #f)
(stage accept #:branch "phase/accept" #:from main #:into main #:accepts () #:agents-may-merge #f)
(stage main #:branch "main" #:accepts () #:agents-may-merge #f)
