(cycle sprint #:stage sprint #:length-days 14 #:name "{year}-s{week}")
(stage sprint #:branch "sprint/{cycle}" #:from main #:accepts (feature bug chore patch) #:entry merge)
(stage main #:branch "main" #:accepts () #:agents-may-merge #f)
