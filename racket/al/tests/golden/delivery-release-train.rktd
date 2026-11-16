(cycle sprint #:stage train #:length-days 14 #:name "{date}")
(stage train #:branch "train/{cycle}" #:from main #:accepts (feature bug chore patch) #:entry merge)
(stage main #:branch "main" #:accepts () #:agents-may-merge #f)
