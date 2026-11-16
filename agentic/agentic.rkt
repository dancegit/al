#lang al
;; The agentic layer of the al repository itself ("al for al").
;; Work comes from the command line or a task file; a run is started by hand or by a poller;
;; agents work in a private git worktree; the repository's own test suites are the checks;
;; a human reviews a branch and PR.md. No forge, no zero-touch.
(agentic-layer #:name "al"
               #:description "Agentic layer for al, the generator of agentic layers"
               #:llm-txt "LLM.txt"
  (codebase #:root ".." #:languages (python racket javascript shell) #:default-branch "main"
            #:description "al generates an agentic layer (prompts, workflows, sandbox, triggers, a dashboard) for any git repository. An LLM writes LLM.txt and agentic.rkt in a small Racket language; a compiler checks the definition and a generator produces the layer from templates. The command is tools/al (Python, run with uv); the runtime and the dashboard are Python with the standard library only."
            #:readme "README.md"
            #:package-manager "inline script metadata in the script that needs it, or uv run --with for a test command; there is no manifest or lock file"
    (cmd test-compiler "PLTCOLLECTS=\"$PWD/racket:\" raco test racket/al/tests/compiler-test.rkt"
         #:description "The language rules: the compiler accepts the examples and rejects each invalid definition with the documented error")
    (cmd test-runtime "(cd kit/runtime && env -u AL_APP_ROOT uv run --with pytest pytest -q)"
         #:description "The runtime unit tests pass, including the shipping gate, the sandbox and attempts to escape the container")
    (cmd test-tools "env -u AL_APP_ROOT uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools -q"
         #:description "The al command itself works: eject, in-repo layers, al improve, dashboard generation")
    (cmd test-dashboard "(cd kit/dashboard && uv run --with pytest pytest -q tests)"
         #:description "The dashboard's model and HTTP server work: unit and HTTP tests, no browser")
    ;; Declared because the repository documents it; not used as a workflow check: one browser test
    ;; is not deterministic (see LLM.txt, Unknowns).
    (cmd test-dashboard-browser "(cd kit/dashboard && uv run --with pytest --with playwright pytest -q tests)"
         #:description "The dashboard tests plus the real-browser tests of the page in the host's Chrome")
    (cmd test-integration "env -u AL_APP_ROOT uv run --with pytest --with pyyaml --with jsonschema pytest tests/integration -q"
         #:timeout 3600
         #:description "The default tier of full runs on the five pinned example repositories passes with the mock agent, and generated prompts equal the golden snapshots")
    (paths #:source ("racket/al" "tools" "kit/runtime" "kit/templates" "kit/schemas" "kit/dashboard" "kit/self" "meta")
           #:tests ("racket/al/tests" "kit/runtime/tests" "kit/dashboard/tests" "tests/tools" "tests/integration")
           #:docs ("docs" "README.md" "tests/README.md" "kit/dashboard/README.md" "meta/README.md")
           ;; The language core (racket/al/private) and the two files generated from it are NOT
           ;; protected: every workflow here ends with a person's review, and agents must be able
           ;; to change the vocabulary and regenerate. The shipping gate and the sandbox stay protected.
           #:protected ("kit/runtime/adw_modules/review.py"
                        "kit/runtime/adw_modules/ship.py"
                        "kit/runtime/adw_modules/gitsafe.py"
                        "kit/runtime/adw_modules/envs.py"
                        "docs/demo"
                        "tests/authored"
                        "tests/integration/testbeds.toml"))
    (conditional-doc "docs/DESIGN.md" "When changing the language, the compiler or how the generator maps a definition to files")
    (conditional-doc "tests/README.md" "When adding or changing a test under tests/integration, or anything al verify does")
    (conditional-doc "kit/dashboard/README.md" "When changing anything under kit/dashboard")
    (conditional-doc "docs/AL-DSL.llms.txt" "When writing or changing any agentic.rkt or a test that contains one")
    (conditional-doc "meta/README.md" "When changing a prompt under meta/commands")
    (conditional-doc "docs/GUIDE.md" "When changing the behaviour or output of a tools/al subcommand, because the guide quotes it"))

  (layer in-repo #:dir "agentic")
  (forge none)
  ;; Up to five runs at once, and five agent calls so they do not queue on each other. Checks stay at
  ;; two at a time: they run this repository's test suites, which are the CPU-heavy part.
  (concurrency slots #:max-runs 5 #:max-agents 5 #:max-checks 2)
  (agent claude-code #:model-base "claude-opus-5-5" #:model-heavy "claude-opus-5-5")

  (prompt-input cli)
  (prompt-input tasklist)
  (trigger manual)
  (trigger cron)

  (environment worktree)
  ;; The sandbox of the zero-touch workflow: the agent and every check run inside this container, which
  ;; sees only the run's worktree. It carries what this repository's suites need: Racket, uv and git.
  (environment podman #:id sandbox #:base-image "docker.io/library/python:3.12-slim"
               #:packages ("git" "curl" "ca-certificates" "libatomic1" "racket")
               #:setup ("pip install --no-cache-dir uv"
                        "HOME=/opt/claude bash -c 'curl -fsSL https://claude.ai/install.sh | bash' && chmod -R a+rX /opt/claude && ln -s /opt/claude/.local/bin/claude /usr/local/bin/claude")
               #:memory "8g" #:cpus "4" #:caches ("uv") #:agent-inside #t #:credentials (claude))

  (artefact log)

  (review human)
  (review auto #:id two-reviewers #:artefacts (log) #:reviewers 2 #:max-retries 2 #:ship pr
          #:require-checks (test-compiler test-runtime) #:allowed-classes (chore bug feature))
  ;; Zero-touch: two reviewer agents and every suite must pass, then the change is merged without a person.
  ;; A change under a protected path, or a very large one, still goes to a person.
  (review auto #:id zero-touch #:artefacts (log) #:reviewers 2 #:max-retries 2 #:ship merge
          #:require-checks (test-compiler test-runtime test-tools test-dashboard test-integration)
          #:allowed-classes (chore bug feature) #:max-diff-lines 4000)
  (dashboard local #:port 8770)   ; 8765 is often taken by other tools

  (command prime #:template prime)
  (command classify #:template classify #:output json)
  (command plan-chore #:template plan-chore #:model-set heavy #:output path
           #:validation (test-compiler test-runtime test-tools test-dashboard)
           #:focus ("A new test for tools/al or tools/explain.py goes in tests/tools/test_<topic>.py; for kit/runtime/adw_modules/X.py in kit/runtime/tests/test_X.py; for the dashboard in kit/dashboard/tests; for the compiler in racket/al/tests."
                    "Tests must not need the network and must not call a real LLM: agent steps in tests use the mock agent or --dry-run."
                    "Tests work in pytest's temporary directory and leave no branch, worktree or container behind; nothing is written into the repository checkout."
                    "No personal paths in committed files: no home directory, user name or machine-specific absolute path."
                    "Never mention a private repository, a real employer or a real customer; examples use public repositories or synthetic data."))
  (command plan-bug #:template plan-bug #:model-set heavy #:output path
           #:validation (test-compiler test-runtime test-tools test-dashboard test-integration)
           #:focus ("The regression test goes in the test directory of the area fixed (tests/tools, kit/runtime/tests, kit/dashboard/tests, racket/al/tests) and must fail before the fix."
                    "docs/AL-DSL.llms.txt and schemas/agentic-layer-v1.schema.json are generated from racket/al/private/vocab.rkt: never edit them by hand; a fix that needs the vocabulary or compiler changed is stated for a human instead of made."
                    "Tests must not need the network unless marked `network`, and must not call a real LLM."
                    "No personal paths and no private repository, employer or customer names in committed files."))
  (command plan-feature #:template plan-feature #:model-set heavy #:output path
           #:validation (test-compiler test-runtime test-tools test-dashboard test-integration)
           #:focus ("A feature is user-visible as tools/al output, a generated file or a dashboard screen; the plan names which, and the test that shows it."
                    "A change to a template under kit/templates changes generated prompts: update the snapshots with --update-golden on the integration suite and include the snapshot diff in the same change."
                    "The vocabulary (racket/al/private/vocab.rkt) is the single source of the schema and the language reference; both are regenerated, never hand-edited, and a vocabulary change is handed to a human."
                    "kit/runtime and kit/dashboard use the Python standard library only; a script that needs a third-party package declares it in its inline `# /// script` header."
                    "A test that needs podman, nix or a browser carries that marker and skips when the tool is missing; it never downloads one."
                    "No personal paths and no private repository, employer or customer names in committed files."))
  (command plan-patch #:template plan-patch #:model-set heavy #:output path
           #:validation (test-compiler test-runtime test-tools test-dashboard))
  (command resolve-test #:template resolve-test #:model-set heavy
           #:focus ("Fix the code or the new test, never weaken or delete an existing test to make a command pass."
                    "A failure in tests/integration golden snapshots after a template change is resolved with --update-golden and a reviewed snapshot diff, not by editing snapshots by hand."))
  (command review #:template review #:model-set heavy #:output json
           #:focus ("Proof is the log of the test command for the area touched; a change without a test that would fail without it is a blocker for bug and feature work."
                    "Any personal path, user name, private repository, employer or customer name in the diff is a blocker."
                    "A hand edit of docs/AL-DSL.llms.txt, schemas/agentic-layer-v1.schema.json or any file under docs/demo is a blocker."))
  (command document #:template document
           #:focus ("Prose in README.md and docs/ is plain and factual: short sentences, no marketing adjectives, numbers only from real runs."
                    "docs/GUIDE.md quotes tools/al output: when a subcommand's output changes, the quoted text changes with it."))
  (command commit #:template commit
           #:focus ("One lower-case summary line that names the area first (e.g. `dashboard: …`, `runtime: …`, `tools: …`); no trailer lines."))
  (command pull-request #:template pull-request)
  (hop implement #:template run-plan #:takes plan)
  (hop meta-command #:template meta-command #:produces command)
  (hop meta-adw #:template meta-adw #:produces adw)

  (expert language #:domain "The #lang al language: vocabulary, compiler rules, schema and reference generation, compiler tests"
          #:paths ("racket/al"))
  (expert runtime #:domain "The workflow runtime: steps, environments, review and shipping gate, triggers, task list, events"
          #:paths ("kit/runtime"))
  (expert dashboard #:domain "The live read-only dashboard: run model, events, HTTP server, page, transcript normalisation"
          #:paths ("kit/dashboard"))

  (adw plan-build #:description "Classify the request, plan it by class, implement the plan; nothing else"
       #:input (cli tasklist) #:trigger (manual cron) #:environment (worktree) #:review human
    (steps
      (run classify #:command classify #:args ("$prompt"))
      (run plan-chore #:command plan-chore #:args ("$prompt") #:when class-chore)
      (run plan-bug #:command plan-bug #:args ("$prompt") #:when class-bug)
      (run plan-feature #:command plan-feature #:args ("$prompt") #:when class-feature)
      (run build #:command implement #:args ("$plan"))))

  (adw sdlc #:description "Plan, build, run the repository's test suites, document, commit; a human reviews the branch and PR.md"
       #:input (cli tasklist) #:trigger (manual cron) #:environment (worktree) #:review human
    (steps
      (compose plan-build)
      (check test-compiler #:cmd test-compiler #:resolve resolve-test #:collect (log))
      (check test-runtime #:cmd test-runtime #:resolve resolve-test #:collect (log))
      (check test-tools #:cmd test-tools #:resolve resolve-test #:collect (log))
      (check test-dashboard #:cmd test-dashboard #:resolve resolve-test #:collect (log))
      (check test-integration #:cmd test-integration #:resolve resolve-test #:collect (log))
      (run document #:command document #:args ("$plan"))
      (run commit #:command commit)
      (review)
      (ship)))

  (adw reviewed #:description "Everything sdlc does, then two independent reviewer agents must both approve before a human gets the branch and PR.md; never merges"
       #:input (cli tasklist) #:trigger (manual cron) #:environment (worktree) #:review two-reviewers
    (steps
      (compose plan-build)
      (check test-compiler #:cmd test-compiler #:resolve resolve-test #:collect (log))
      (check test-runtime #:cmd test-runtime #:resolve resolve-test #:collect (log))
      (check test-tools #:cmd test-tools #:resolve resolve-test #:collect (log))
      (check test-dashboard #:cmd test-dashboard #:resolve resolve-test #:collect (log))
      (check test-integration #:cmd test-integration #:resolve resolve-test #:collect (log))
      (run document #:command document #:args ("$plan"))
      (run commit #:command commit)
      (review #:patch plan-patch #:implement implement)
      (ship)))

  (adw zte #:description "Zero-touch: plan, build and test inside a container; two reviewer agents and every suite must pass; then merge without a person"
       #:input (cli tasklist) #:trigger (manual cron) #:environment (worktree sandbox) #:review zero-touch
    (steps
      (compose plan-build)
      (check test-compiler #:cmd test-compiler #:resolve resolve-test #:collect (log))
      (check test-runtime #:cmd test-runtime #:resolve resolve-test #:collect (log))
      (check test-tools #:cmd test-tools #:resolve resolve-test #:collect (log))
      (check test-dashboard #:cmd test-dashboard #:resolve resolve-test #:collect (log))
      (check test-integration #:cmd test-integration #:resolve resolve-test #:collect (log))
      (run document #:command document #:args ("$plan"))
      (run commit #:command commit)
      (review #:patch plan-patch #:implement implement)
      (ship))))
