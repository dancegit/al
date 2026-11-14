#lang al
;; Agentic layer for express, authored from LLM.txt.
(agentic-layer #:name "express"
               #:description "Agentic layer for the express Node.js web framework library"
               #:llm-txt "LLM.txt"
  (codebase #:root "../../testbeds/express" #:languages (javascript) #:default-branch "master"
            #:description "express is a web framework for Node.js, published to the npm registry as the package express and consumed through require('express'). CommonJS JavaScript with no build step: index.js re-exports lib/express.js. Tested with mocha and supertest, linted with eslint 8, published to npm by a GitHub Actions workflow when a GitHub release is published."
            #:package-manager "npm install" #:readme "Readme.md"
    (cmd install "npm install"
         #:description "dependencies and devDependencies resolve and install into node_modules")
    (cmd lint "npm run lint"
         #:description "eslint reports no violation of .eslintrc.yml in any JavaScript file of the repository")
    (cmd test "npm run test-ci"
         #:description "the whole mocha suite (test/ and test/acceptance/) passes with global-leak checking, under nyc, writing coverage/lcov.info and a text coverage table to stdout")
    (cmd test-fast "npm test"
         #:description "the whole mocha suite (test/ and test/acceptance/) passes with global-leak checking, without coverage")
    (paths #:source ("lib" "index.js") #:tests ("test")
           #:protected (".github" "LICENSE" ".npmrc"))
    (conditional-doc "examples/README.md" "When adding, renaming or removing a directory under examples/")
    (conditional-doc "History.md" "When a change alters behaviour that an earlier release entry describes"))

  (voice kokoro)

  (prompt-input tasklist)
  (prompt-input voice #:route auto #:append-to-tasklist #t)

  (trigger voice)
  (trigger file-watch)
  (trigger manual)

  (environment worktree)
  (environment home-manager #:packages ("nodejs_22"))
  (environment host)

  (artefact log)
  (artefact coverage #:glob "coverage/lcov.info")

  (review agent #:artefacts (log))

  (command prime #:template prime)
  (command classify #:template classify #:output json)
  (command plan-chore #:template plan-chore #:model-set heavy #:output path #:validation (lint test)
           #:focus ("No lock file is committed: never add package-lock.json, npm-shrinkwrap.json or yarn.lock."
                    "A test-only addition goes in test/<object>.<member>.js (unit) or test/acceptance/<example>.js (acceptance test for examples/<example>)."))
  (command plan-bug #:template plan-bug #:model-set heavy #:output path #:validation (lint test)
           #:focus ("The regression test goes in the existing test/<object>.<member>.js for the affected member (test/res.send.js for res.send in lib/response.js); create that file if absent."
                    "Tests are mocha describe/it blocks issuing HTTP requests with supertest; the outer describe names the object (res), the inner one the member (.send())."
                    "Code must run on every Node.js version from 18 and must not create global variables: mocha runs with --check-leaks."))
  (command plan-feature #:template plan-feature #:model-set heavy #:output path #:validation (lint test)
           #:focus ("A new member of lib/response.js, lib/request.js or lib/application.js gets its tests in test/res.<member>.js, test/req.<member>.js or test/app.<member>.js."
                    "State the user-visible change as a public API signature or the HTTP response (status, headers, body) an application produces."
                    "The global Buffer is forbidden; take Buffer from the node:buffer module."
                    "Code must run on every Node.js version from 18 and must not create global variables: mocha runs with --check-leaks."))
  (command plan-patch #:template plan-patch #:model-set heavy #:output path #:validation (lint test))
  (command resolve-test #:template resolve-test #:model-set heavy
           #:focus ("JavaScript is indented with 2 spaces, files end with a newline, no trailing whitespace, comparisons use === except == null, no unused variables."))
  (command review #:template review #:model-set heavy #:output json
           #:focus ("The proof is the mocha spec output in the test log: the names of the tests covering the change and the pass count must be present."
                    "A bug fix or feature without a test in test/<object>.<member>.js for the changed member is a blocker."
                    "A change under .github, LICENSE or .npmrc is a blocker for a human."))
  (command document #:template document)
  (command commit #:template commit)
  (command pull-request #:template pull-request)
  (command question #:template question
           #:description "Answer a question about the codebase without changing it")
  (hop implement #:template run-plan #:takes plan)
  (hop meta-command #:template meta-command #:produces command)
  (hop meta-adw #:template meta-adw #:produces adw)

  (adw plan-build #:description "Classify, plan by class and implement the plan"
       #:input (tasklist voice) #:trigger (voice file-watch manual)
       #:environment (worktree home-manager) #:review agent
    (steps
      (run classify #:command classify #:args ("$prompt"))
      (run plan-chore #:command plan-chore #:args ("$prompt") #:when class-chore)
      (run plan-bug #:command plan-bug #:args ("$prompt") #:when class-bug)
      (run plan-feature #:command plan-feature #:args ("$prompt") #:when class-feature)
      (run build #:command implement #:args ("$plan"))))

  (adw sdlc #:description "Plan, build, lint, test, document, commit, agent review; a human merges"
       #:input (tasklist voice) #:trigger (voice file-watch manual)
       #:environment (worktree home-manager) #:review agent
    (steps
      (compose plan-build)
      (check lint #:cmd lint #:resolve resolve-test)
      (check test #:cmd test #:resolve resolve-test #:collect (log coverage))
      (run document #:command document #:args ("$plan"))
      (run commit #:command commit)
      (review #:patch plan-patch #:implement implement)
      (ship)))

  (adw ask #:description "Read-only: answer a spoken question about the codebase"
       #:input (voice) #:trigger (voice manual)
       #:environment (host) #:review agent
    (steps
      (run answer #:command question #:args ("$prompt")))))
