#lang al
;; Agentic layer for gin-gonic/gin, authored from LLM.txt.
;; PITER: GitHub issues -> forge CI workflow or manual -> git worktree -> human pull request. Agent: Codex.
(agentic-layer #:name "gin" #:description "Agentic layer for the Gin HTTP web framework (Go library)"
               #:llm-txt "LLM.txt"
  (codebase #:root "../../testbeds/gin" #:languages (go) #:default-branch "master"
            #:description "Gin is an HTTP web framework written in Go, imported by other Go programs as the module github.com/gin-gonic/gin. Root package gin (router, context, middleware) plus sub-packages binding, render, codec/json, ginS and internal. Shipped by pushing a git tag, which runs GoReleaser in GitHub Actions."
            #:readme "README.md"
    (cmd lint "make vet"
         #:description "go vet reports nothing for all packages except /examples/")
    (cmd format "make fmt"
         #:description "rewrites every *.go file with gofmt -s -w; proves nothing, it changes files")
    (cmd test "make test"
         #:description "tests of packages gin, ginS, binding, render pass with go test -v -covermode=count; writes coverage.out")
    (paths #:source ("." "binding" "render" "codec/json" "internal/bytesconv" "internal/fs" "ginS")
           #:tests ("." "binding" "render" "internal/bytesconv" "internal/fs" "ginS" "testdata")
           #:docs ("docs")
           #:protected (".github" ".goreleaser.yaml" ".golangci.yml" "codecov.yml" "LICENSE" "auth.go"
                        "testdata/certificate" "testdata/protoexample/test.pb.go"))
    (conditional-doc "docs/doc.md" "When adding or changing a feature (it must be documented there), or when working with the build tags jsoniter, go_json, sonic, nomsgpack")
    (conditional-doc "CONTRIBUTING.md" "When preparing a pull request")
    (conditional-doc ".github/PULL_REQUEST_TEMPLATE.md" "When writing a pull request description")
    (conditional-doc "ginS/README.md" "When changing anything under ginS/")
    (conditional-doc "BENCHMARKS.md" "When changing router performance in tree.go")
    (conditional-doc "CHANGELOG.md" "When checking in which release a feature or fix shipped"))

  (forge github #:repo "gin-gonic/gin")
  (agent codex)

  (prompt-input issue)
  (trigger forge-actions #:runner "self-hosted")
  (trigger manual)

  (environment worktree)
  ;; gin's own tests bind fixed ports (:8443 among others); a container per run gives every run its
  ;; own network namespace, so they cannot collide with the host or with each other.
  (environment podman #:base-image "docker.io/library/golang:1.26-bookworm" #:caches ("go"))

  (artefact log)
  (artefact coverage #:glob "coverage.out")

  (review human)

  (command prime #:template prime)
  (command classify #:template classify #:output json)
  (command plan-chore #:template plan-chore #:model-set heavy #:output path #:validation (lint test)
           #:focus ("A code change adds or modifies tests in <name>_test.go in the same directory as the changed <name>.go."
                    "Use any, not interface{}."))
  (command plan-bug #:template plan-bug #:model-set heavy #:output path #:validation (lint test)
           #:focus ("The regression test goes in the <name>_test.go file beside the fixed <name>.go, using github.com/stretchr/testify assertions."
                    "The fix must compile and pass under the CI build tag sets: none, -tags nomsgpack, -tags sonic with --ldflags=-checklinkname=0, -tags go_json, and -race."
                    "Use any, not interface{}."))
  (command plan-feature #:template plan-feature #:model-set heavy #:output path #:validation (lint test)
           #:focus ("A feature is a new or changed exported function, method, type or option of package gin, binding or render; plan its tests in <name>_test.go beside the source, using github.com/stretchr/testify assertions."
                    "Plan a section for the feature in docs/doc.md, not in README.md."
                    "The change must compile and pass under the CI build tag sets: none, -tags nomsgpack, -tags sonic with --ldflags=-checklinkname=0, -tags go_json, and -race."
                    "Use any, not interface{}."))
  (command plan-patch #:template plan-patch #:model-set heavy #:output path #:validation (lint test)
           #:focus ("A code change adds or modifies tests in <name>_test.go in the same directory as the changed <name>.go."))
  (command resolve-test #:template resolve-test #:model-set heavy
           #:focus ("make test runs only packages gin, ginS, binding and render and writes coverage.out; do not edit files under testdata/certificate or testdata/protoexample/test.pb.go to make a test pass."))
  (command review #:template review #:model-set heavy #:output json
           #:focus ("Every code change has an added or modified test in the <name>_test.go beside the changed file."
                    "A feature is proven by make test output and coverage.out, and has a section in docs/doc.md; coverage targets are 99% for the project and 99% for the patch."
                    "Tests use github.com/stretchr/testify assertions; code uses any, not interface{}."))
  (command document #:template document
           #:focus ("Document a new feature in docs/doc.md, not in README.md."))
  (command commit #:template commit
           #:focus ("Commit subject has the form <type>(<optional scope>): <subject> with type one of feat, fix, chore, refactor, build, ci, docs."
                    "A pull request has at most two commits; squash otherwise."))
  (command pull-request #:template pull-request
           #:focus ("Open the pull request against the master branch."
                    "The description follows the checklist in .github/PULL_REQUEST_TEMPLATE.md."
                    "A pull request has at most two commits."))
  (hop run-plan #:template run-plan #:takes plan)
  (hop meta-command #:template meta-command #:produces command)
  (hop meta-adw #:template meta-adw #:produces adw)

  (expert binding #:domain "Request binding and validation for JSON, XML, form, query, URI, header, YAML, TOML, protobuf, msgpack and BSON"
          #:paths ("binding"))
  (expert render #:domain "Response renderers for JSON, XML, HTML, YAML, TOML, protobuf, msgpack, BSON, PDF, redirect, text, data and reader"
          #:paths ("render"))

  (adw plan-build #:description "Classify the issue, plan by class, implement the plan"
       #:input (issue) #:trigger (forge-actions manual) #:environment (worktree) #:review human #:agent codex
    (steps
      (run classify #:command classify #:args ("$prompt"))
      (run plan-chore #:command plan-chore #:args ("$prompt") #:when class-chore)
      (run plan-bug #:command plan-bug #:args ("$prompt") #:when class-bug)
      (run plan-feature #:command plan-feature #:args ("$prompt") #:when class-feature)
      (run plan-patch #:command plan-patch #:args ("$prompt") #:when class-patch)
      (run build #:command run-plan #:args ("$plan"))))

  (adw sdlc #:description "Plan, build, vet, test, document, commit; a human reviews the pull request"
       #:input (issue) #:trigger (forge-actions manual) #:environment (worktree podman) #:review human #:agent codex
    (steps
      (compose plan-build)
      (check lint #:cmd lint #:resolve resolve-test)
      (check test #:cmd test #:resolve resolve-test #:collect (coverage log))
      (run document #:command document #:args ("$plan"))
      (run commit #:command commit)
      (review)
      (ship))))
