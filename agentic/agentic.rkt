#lang al
;; The agentic layer of the al repository itself ("al for al").
;; Work comes from the command line or a task file; a run is started by hand or by a poller;
;; agents work in a private git worktree; the repository's own test suites are the checks;
;; a human reviews a branch and PR.md. Runs land on the branch integration; a person promotes a batch
;; of them into main after GitHub's CI passed on that exact commit.
(agentic-layer #:name "al"
               #:description "Agentic layer for al, the generator of agentic layers"
               #:llm-txt "LLM.txt"
  (codebase #:root ".." #:languages (python racket javascript shell) #:default-branch "main"
            #:description "al generates an agentic layer (prompts, workflows, sandbox, triggers, a dashboard) for any git repository. An LLM writes LLM.txt and agentic.rkt in a small Racket language; a compiler checks the definition and a generator produces the layer from templates. The command is tools/al (Python, run with uv); the runtime and the dashboard are Python with the standard library only."
            #:readme "README.md"
            #:package-manager "inline script metadata in the script that needs it, or uv run --with for a test command; there is no manifest or lock file"
    (cmd test-compiler "PLTCOLLECTS=\"$PWD/racket:\" raco test racket/al/tests/compiler-test.rkt"
         #:description "The language rules: the compiler accepts the examples and rejects each invalid definition with the documented error"
         #:covers ("racket" "schemas" "docs/AL-DSL.llms.txt" "kit/runtime/tests/full.layer.json") #:always #f)
    (cmd test-runtime "(cd kit/runtime && env -u AL_APP_ROOT uv run --with pytest pytest -q)"
         #:description "The runtime unit tests pass, including the shipping gate, the sandbox and attempts to escape the container"
         #:covers ("kit/runtime" "racket" "schemas") #:always #f)
    ;; The sandbox's escape, isolation and nested-engine suites need a real podman, which the run
    ;; container does not have and the nested engine cannot host (no freezer, cgroup limits,
    ;; /dev/net/tun or subuid range): they run on this machine #:in host-engine (run 35ac29a6).
;; The zero-touch gate requires the check, so a change to the runtime cannot ship on podman skips.
    (cmd test-podman "(cd kit/runtime && env -u AL_APP_ROOT -u PYTHONPATH uv run --with pytest pytest -q -rs tests/test_sandbox_podman.py tests/test_isolation_podman.py tests/test_nested_engine.py tests/test_nested_engine_podman.py)"
         #:timeout 3600
         #:description "The sandbox's escape, isolation and nested-engine tests against this machine's real rootless podman"
         #:covers ("kit/runtime" "racket" "schemas") #:always #f)
    (cmd test-tools "env -u AL_APP_ROOT uv run --with pytest --with pytest-xdist --with pyyaml --with jsonschema pytest tests/tools -q -n 4"
         #:description "The al command itself works: eject, in-repo layers, al improve, dashboard generation"
         #:timeout 3600
         #:covers ("tools" "tests/tools" "kit" "racket" "schemas" "meta") #:always #f)
    (cmd test-docs "if [ -f tests/tools/test_readme.py ]; then env -u AL_APP_ROOT uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools/test_readme.py tests/tools/test_generated_references.py -q; else echo 'this branch started before the README check existed: nothing to run'; fi"
         #:description "Links, images and commands shown in the README and the documents resolve, and the generated references are current"
         #:covers ("README.md" "docs" "kit/dashboard/README.md" "meta/README.md" "tests/README.md" "tests/tools/test_readme.py") #:always #f)
    (cmd test-dashboard "(cd kit/dashboard && uv run --with pytest pytest -q tests)"
         #:description "The dashboard's model and HTTP server work: unit and HTTP tests, no browser")
    ;; Declared because the repository documents it; not used as a workflow check: one browser test
    ;; is not deterministic (see LLM.txt, Unknowns).
    (cmd test-dashboard-browser "(cd kit/dashboard && uv run --with pytest --with playwright pytest -q tests)"
         #:description "The dashboard tests plus the real-browser tests of the page in the host's Chrome")
    ;; The zero-touch workflow's dashboard check: the same tests with the browser tests, in the sandbox's
    ;; Chromium, saving the screenshots the tests take as proof for the reviewers.
    (cmd test-dashboard-proof "(cd kit/dashboard && rm -rf shots && AL_DASHBOARD_SHOTS=shots uv run --with pytest --with playwright pytest -q tests)"
         #:timeout 2400
         #:description "The dashboard's unit, HTTP and real-browser tests, with screenshots of the page written to kit/dashboard/shots"
         #:covers ("kit/dashboard" "kit/runtime") #:always #f)
    (cmd test-integration "env -u AL_APP_ROOT uv run --with pytest --with pyyaml --with jsonschema pytest tests/integration -q"
         #:timeout 3600
         #:description "The default tier of full runs on the five pinned example repositories passes with the mock agent, and generated prompts equal the golden snapshots"
         #:covers ("tools" "kit" "racket" "schemas" "meta" "tests/integration") #:always #f)
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
  ;; GitHub hosts the repository and its CI. Runs and promotions still merge here, then push the branch they moved (#:pull-requests #f).
  (forge github #:repo "dancegit/al" #:pull-requests #f)
  ;; Up to ten runs at once, and ten agent calls so they do not queue on each other. Checks stay at
  ;; three at a time: they run this repository's test suites, which are the CPU-heavy part.
  (concurrency slots #:max-runs 10 #:max-agents 10 #:max-checks 3)
  (agent claude-code #:model-base "claude-opus-5-5" #:model-heavy "claude-opus-5-5")
  ;; What the stand-in may do alone in this repository. land and promote stay out on purpose: it proposes
  ;; them and a person decides (a promotion is what moves main). The dashboard may narrow this per machine.
  (stand-in auto
    #:may (resume restart answer integrate settle retry-job)
    #:never ()
    #:max-actions-per-run 2 #:max-per-hour 12
    #:escalate-to (dashboard))

  (prompt-input cli)
  (prompt-input tasklist)
  (trigger manual)
  (trigger cron)

  (environment worktree)
  ;; The sandbox of the zero-touch workflow: the agent and every check run inside this container, which
  ;; sees only the run's worktree. It carries what this repository's suites need: Racket, uv and git.
  (environment podman #:id sandbox #:base-image "docker.io/library/python:3.12-slim"
               #:packages ("git" "curl" "ca-certificates" "libatomic1" "racket" "chromium" "fonts-liberation" "fonts-dejavu-core" "ffmpeg" "espeak-ng" "libsndfile1")
               #:setup ("pip install --no-cache-dir uv"
                        "HOME=/opt/claude bash -c 'curl -fsSL https://claude.ai/install.sh | bash' && chmod -R a+rX /opt/claude && ln -s /opt/claude/.local/bin/claude /usr/local/bin/claude")
               #:memory "8g" #:cpus "4" #:caches ("uv") #:agent-inside #t #:credentials (claude))
  ;; This machine, for the podman suites only (cmd test-podman). Not #:pure: the host's rootless
  ;; podman stays on PATH. Approved by Per for zero-touch runs (2026-11-21, run 35ac29a6).
  (environment nix-devshell #:id host-engine #:packages ("uv" "git"))

  (artefact log)
  (artefact image #:id screenshots #:glob "kit/dashboard/shots/*.png")
  (artefact image #:id run-shots #:glob "kit/dashboard/shots/run-*.png" #:max-files 24)
  (artefact image #:id showcase-stills #:glob "docs/showcase/*.png")
  (artefact video #:id showcase-clips #:glob "docs/showcase/*.mp4")
  ;; Proof of the voice helper: written by its validation commands into a git-ignored folder of the
  ;; run's worktree. Without these, reviewers got only the first twelve dashboard screenshots.
  (artefact image #:id voice-shots #:glob "kit/dashboard/shots-proof/voice-*.png" #:max-files 24)
  (artefact video #:id voice-clips #:glob "kit/dashboard/shots-proof/voice-*.webm")
  (artefact audio #:id voice-audio #:glob "kit/dashboard/shots-proof/voice/roundtrip-*.wav")
  (artefact file #:id voice-report #:glob "kit/dashboard/shots-proof/voice/*.json")
  (artefact file #:id voice-logs #:glob "kit/dashboard/shots-proof/voice-*.log")

  (review human)
  (review auto #:id two-reviewers #:artefacts (log) #:reviewers 2 #:max-retries 2 #:ship pr
          #:require-checks (test-compiler test-runtime) #:allowed-classes (chore bug feature))
  ;; Zero-touch: two reviewer agents and every suite must pass, then the change is merged without a person.
  ;; A change under a protected path, or a very large one, still goes to a person.
  (review auto #:id zero-touch #:artefacts (log screenshots run-shots showcase-stills showcase-clips voice-shots voice-clips voice-audio voice-report voice-logs) #:reviewers 2 #:max-retries 2 #:ship merge
          #:require-checks (test-docs test-compiler test-runtime test-podman test-tools test-dashboard-proof test-integration) #:require affected
          #:allowed-classes (chore bug feature) #:max-diff-lines 4000)
  (dashboard local #:port 8770)   ; 8765 is often taken by other tools
  ;; Every run lands on integration; main changes only by a promotion a person approves.
  (stage integration #:branch "integration" #:from main #:accepts (chore bug feature patch) #:entry merge)
  ;; The full suites are main's checks: the promotion runs them on the integration head before it is offered.
  ;; They sit on main, not on integration, because a stage's checks are added to every run delivering there.
  (stage main #:branch "main" #:accepts () #:agents-may-merge #f
         #:checks (test-docs test-compiler test-runtime test-tools test-dashboard test-integration))
  ;; Fast-forward: main becomes exactly the integration commit whose CI passed on GitHub. No version, no tag, no deploy.
  (promotion integration -> main #:approval human #:approvers 1 #:requires (checks acceptance forge-ci) #:method fast-forward)
  ;; Per run: the checks a change affects, as before (no cmd has a #:targeted form). Per batch: the full
  ;; suites on the integration head, after 5 landed changes or 24 hours.
  (testing targeted #:on-stage full #:batch ((every 5) (after-hours 24)))

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
       #:input (cli tasklist) #:trigger (manual cron) #:environment (worktree) #:review human #:deliver-to integration
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
       #:input (cli tasklist) #:trigger (manual cron) #:environment (worktree) #:review two-reviewers #:deliver-to integration
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
       #:input (cli tasklist) #:trigger (manual cron) #:environment (worktree sandbox) #:review zero-touch #:deliver-to integration
    (steps
      (compose plan-build)
      (check test-docs #:cmd test-docs #:resolve resolve-test #:collect (log) #:when-affected #t)
      (check test-compiler #:cmd test-compiler #:resolve resolve-test #:collect (log) #:when-affected #t)
      (check test-runtime #:cmd test-runtime #:resolve resolve-test #:collect (log) #:when-affected #t)
      (check test-podman #:cmd test-podman #:in host-engine #:resolve resolve-test #:collect (log) #:when-affected #t
             #:forbid-skip-reason "podman is not installed")
      (check test-tools #:cmd test-tools #:resolve resolve-test #:collect (log) #:when-affected #t)
      (check test-dashboard #:cmd test-dashboard-proof #:resolve resolve-test #:collect (log screenshots showcase-stills showcase-clips voice-shots voice-clips voice-audio voice-report voice-logs) #:when-affected #t)
      (check test-integration #:cmd test-integration #:resolve resolve-test #:collect (log) #:when-affected #t)
      (run document #:command document #:args ("$plan"))
      (run commit #:command commit)
      (review #:patch plan-patch #:implement implement)
      (ship))))
