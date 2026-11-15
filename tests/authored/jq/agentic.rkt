#lang al
;; Agentic layer for jq, authored from LLM.txt.
(agentic-layer #:name "jq" #:description "Agentic layer for the jq command-line JSON processor and libjq" #:llm-txt "LLM.txt"
  (codebase #:root "../../testbeds/jq" #:languages (c shell python) #:default-branch "master"
            #:description "jq is a command-line JSON processor and the C library libjq, written in portable C with no runtime dependencies. It is built with GNU autotools (autoconf, automake, libtool) and optionally links the vendored Oniguruma regex library (git submodule). The manual and website are generated from YAML with Python."
            #:readme "README.md"
    (cmd install "autoreconf -i && ./configure --disable-docs --with-oniguruma=yes --enable-static"
         #:description "The build system is generated and configured against the system Oniguruma (libonig-dev); the vendor/oniguruma submodule is not fetched, because a run's container mounts the repository's .git read-only and git submodule update cannot register it; must run once in a fresh checkout before build or test")
    (cmd build "make -j\"$(nproc)\""
         #:description "libjq and the jq binary compile and link with -Wextra -Wall")
    (cmd test "make check VERBOSE=yes"
         #:description "Every driver in TESTS of Makefile.am passes against the built ./jq; writes test-suite.log and tests/*.log")
    (cmd docs "(cd docs && pipenv sync && pipenv run python3 build_website.py)"
         #:description "The website renders from docs/content into docs/output/")
    (paths #:source ("src" "Makefile.am" "configure.ac" "config/m4" "m4" "scripts")
           #:tests ("tests")
           #:docs ("docs" "README.md" "NEWS.md")
           #:protected (".github" "Dockerfile" "COPYING" "SECURITY.md" "KEYS" "sig" "scripts/version" "jq.spec"
                        "vendor" ".gitmodules" "src/lexer.c" "src/lexer.h" "src/parser.c" "src/parser.h"
                        "tests/man.test" "tests/manonig.test" "jq.1.prebuilt" "docs/Pipfile.lock" "docs/public/CNAME"))
    (conditional-doc "docs/README.md" "When changing anything under docs/, or regenerating jq.1.prebuilt, tests/man.test or tests/manonig.test")
    (conditional-doc "docs/content/manual/dev/manual.yml" "When adding or changing a builtin, operator or command-line option")
    (conditional-doc "docs/manual_schema.yml" "When editing any manual.yml")
    (conditional-doc "tests/setup" "When adding or changing a test driver script under tests/")
    (conditional-doc ".github/workflows/ci.yml" "When changing configure options or cross-compilation behaviour")
    (conditional-doc "NEWS.md" "When a change is user-visible and belongs in release notes")
    (conditional-doc "SECURITY.md" "When the task concerns a security vulnerability"))

  (forge none)
  (storage local)
  (agent claude-code)

  (prompt-input cli)
  (prompt-input spec-file)
  (trigger manual)

  (environment worktree)
  (environment podman #:packages ("build-essential" "autoconf" "automake" "libtool" "git" "libonig-dev") #:network slirp)

  (artefact log)
  (artefact file #:id test-suite-log #:glob "test-suite.log")
  (artefact file #:id test-logs #:glob "tests/*.log")

  (review auto #:artefacts (log test-suite-log test-logs) #:ship pr #:require-checks (build test)
          #:allowed-classes (chore bug feature patch))

  (command prime #:template prime)
  (command classify #:template classify #:output json)
  (command plan-chore #:template plan-chore #:model-set heavy #:output path #:validation (build test)
           #:focus ("A new C source or header file must be added to LIBJQ_SRC or LIBJQ_INCS in Makefile.am."
                    "A new test driver script must be added to TESTS in Makefile.am, and every new test data file to EXTRA_DIST in Makefile.am."
                    "Never hand-edit src/lexer.c, src/lexer.h, src/parser.c or src/parser.h; change src/lexer.l or src/parser.y instead."))
  (command plan-bug #:template plan-bug #:model-set heavy #:output path #:validation (build test)
           #:focus ("Add a regression test: language or builtin behaviour in tests/jq.test, regex behaviour in tests/onig.test, command-line or process behaviour in tests/shtest."
                    "A test in tests/jq.test is three lines: jq program, input, expected output; blank lines and lines starting with # are ignored."
                    "Never hand-edit src/lexer.c, src/lexer.h, src/parser.c or src/parser.h; change src/lexer.l or src/parser.y instead."
                    "Building and running the tests must leave no change in tracked files."))
  (command plan-feature #:template plan-feature #:model-set heavy #:output path #:validation (build test)
           #:focus ("Builtins that can be written in jq go in src/builtin.jq; C builtins go in src/builtin.c."
                    "Add tests in tests/jq.test (three lines: program, input, expected output), tests/onig.test for regex, or tests/shtest for command-line options."
                    "Document every user-visible builtin or option in docs/content/manual/dev/manual.yml with examples entries; they are tests."
                    "After changing docs/content/manual/dev/manual.yml, tests/man.test, tests/manonig.test and jq.1.prebuilt must be regenerated; they are protected paths, so say so in the plan."
                    "A new C source or header file must be added to LIBJQ_SRC or LIBJQ_INCS in Makefile.am."
                    "Public libjq symbols start with jq_ or jv_."))
  (command plan-patch #:template plan-patch #:model-set heavy #:output path #:validation (build test))
  (command resolve-test #:template resolve-test #:model-set heavy
           #:focus ("C code must compile with -Wextra -Wall -Wno-unused-parameter -Wno-unused-function -Woverlength-strings -Wstrict-prototypes."
                    "Failing test output is in test-suite.log and tests/*.log."
                    "Never fix a failure by editing tests/man.test or tests/manonig.test; they are generated from docs/content/manual/dev/manual.yml."))
  (command review #:template review #:model-set heavy #:output json
           #:focus ("A user-visible change is jq CLI output (stdout, stderr, exit code) or the public API in src/jq.h and src/jv.h; require test-suite.log and tests/*.log showing the new test passing."
                    "A bug fix must add a regression test to tests/jq.test, tests/onig.test or tests/shtest."
                    "A new builtin or option must have an entry with examples in docs/content/manual/dev/manual.yml."
                    "Reject hand edits to src/lexer.c, src/lexer.h, src/parser.c and src/parser.h."
                    "New test drivers appear in TESTS and new test data files in EXTRA_DIST in Makefile.am."))
  (command document #:template document
           #:focus ("User-visible builtins and options are documented in docs/content/manual/dev/manual.yml, never in docs/content/manual/v*/manual.yml."))
  (command commit #:template commit
           #:focus ("Text files use LF line endings."
                    "Do not commit build output; building and testing must leave no change in tracked files."))
  (command pull-request #:template pull-request)
  (hop implement #:template run-plan #:takes plan)
  (hop new-command #:template meta-command #:produces command)
  (hop new-adw #:template meta-adw #:produces adw)

  (adw plan-build #:description "Classify, plan by class and implement the plan"
       #:input (cli spec-file) #:trigger (manual) #:environment (worktree podman) #:review auto #:agent claude-code
    (steps
      (run classify #:command classify #:args ("$prompt"))
      (run plan-chore #:command plan-chore #:args ("$prompt") #:when class-chore)
      (run plan-bug #:command plan-bug #:args ("$prompt") #:when class-bug)
      (run plan-feature #:command plan-feature #:args ("$prompt") #:when class-feature)
      (run plan-patch #:command plan-patch #:args ("$prompt") #:when class-patch)
      (run implement #:command implement #:args ("$plan"))))

  (adw sdlc #:description "Plan, build, test, document, commit, agent review with deterministic gates; opens a PR only"
       #:input (cli spec-file) #:trigger (manual) #:environment (worktree podman) #:review auto #:agent claude-code
    (steps
      (compose plan-build)
      (check install #:cmd install)
      (check build #:cmd build #:resolve resolve-test #:collect (log))
      (check test #:cmd test #:resolve resolve-test #:collect (log test-suite-log test-logs))
      (run document #:command document #:args ("$plan"))
      (run commit #:command commit)
      (review #:patch plan-patch #:implement implement)
      (ship))))
