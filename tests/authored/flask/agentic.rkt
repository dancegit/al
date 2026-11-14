#lang al
;; Agentic layer for Flask, authored from LLM.txt.
;; PITER: tasklist + cli prompt, cron + manual trigger, worktree + nix dev shell,
;; human review (sdlc) and zero-touch auto review for chores and bugs (zte), Claude Code.
(agentic-layer #:name "flask" #:description "Agentic layer for the Flask WSGI web framework library" #:llm-txt "LLM.txt"
  (codebase #:root "../../testbeds/flask" #:languages (python) #:default-branch "main"
            #:description "Flask is a Python WSGI web application framework distributed as the Flask package (requires Python >=3.11). It is used as a library and through its flask command line entry point, is built on Werkzeug, Jinja, Click, ItsDangerous, MarkupSafe and Blinker, is managed with uv and tox, and ships to PyPI."
            #:readme "README.md"
    (cmd lint "uv run --locked --no-default-groups --group pre-commit pre-commit run --show-diff-on-failure --color=always --all-files"
         #:description "Every pre-commit hook passes on all files: ruff lint rules B, E, F, I, UP, W; ruff format; uv.lock matches pyproject.toml; codespell; whitespace and end-of-file hooks. Hooks rewrite files in place and exit non-zero when they changed something.")
    (cmd typecheck "uv run --locked --no-default-groups --group dev tox run -e typing"
         #:description "mypy in strict mode reports no errors for src and tests/type_check.")
    (cmd test "TOX_ENV=py3.12 uv run --locked --no-default-groups --group dev tox run -- --junitxml=.tox/reports/junit.xml"
         #:description "The pytest suite in tests passes on Python 3.12 against the locked dependency versions, with every warning treated as an error; writes a JUnit XML report.")
    (cmd build "uv build"
         #:description "The sdist and wheel build into dist/.")
    (cmd docs "uv run --locked --no-default-groups --group dev tox run -e docs"
         #:description "The Sphinx documentation builds to docs/_build/dirhtml with warnings treated as errors.")
    (paths #:source ("src/flask") #:tests ("tests") #:docs ("docs")
           #:protected (".github" ".pre-commit-config.yaml" ".readthedocs.yaml" "uv.lock" "LICENSE.txt" "src/flask/sessions.py"))
    (conditional-doc "src/flask/sansio/README.md" "When changing any file under src/flask/sansio")
    (conditional-doc "docs/web-security.rst" "When changing session, cookie, JSON or response header handling")
    (conditional-doc "docs/extensiondev.rst" "When changing an API that extensions hook into (Flask.extensions, init_app patterns, signals, teardown callbacks)")
    (conditional-doc "docs/async-await.rst" "When changing async view support or ensure_sync")
    (conditional-doc "docs/cli.rst" "When changing src/flask/cli.py")
    (conditional-doc "docs/testing.rst" "When changing src/flask/testing.py")
    (conditional-doc "docs/contributing.rst" "When preparing a pull request"))

  (prompt-input tasklist)
  (prompt-input cli)
  (trigger cron)
  (trigger manual)

  (environment worktree)
  (environment nix-devshell #:packages ("python312" "uv" "git")
               #:nixpkgs "path:/nix/store/243rp6ap5vd4zhgh24fqhvd8m1k48yyi-nixpkgs/nixpkgs")

  (artefact log)
  (artefact junit #:glob ".tox/reports/junit.xml")

  (review human)
  (review auto #:id zero-touch #:artefacts (log junit) #:ship merge
          #:require-checks (lint typecheck test) #:allowed-classes (chore bug)
          #:block-protected-paths #t)

  (command prime #:template prime)
  (command classify #:template classify #:output json)
  (command plan-chore #:template plan-chore #:model-set heavy #:output path #:validation (lint typecheck test)
           #:focus ("Dependencies are declared in pyproject.toml and locked in uv.lock; uv.lock is regenerated with uv, never edited by hand, and is a protected path."
                    "When files under docs change, the plan also lists the docs cmd as a validation step."
                    "A code change adds an entry to CHANGES.rst under the topmost (unreleased) version, as a '-   ' bullet ending with an :issue:`<number>` or :pr:`<number>` reference."))
  (command plan-bug #:template plan-bug #:model-set heavy #:output path #:validation (lint typecheck test)
           #:focus ("The regression test goes into the existing tests/test_<area>.py module of the source file being fixed (src/flask/blueprints.py -> tests/test_blueprints.py, src/flask/json/tag.py -> tests/test_json_tag.py); a regression with no matching module goes into tests/test_regression.py."
                    "The regression test must fail without the fix."
                    "Add an entry to CHANGES.rst under the topmost (unreleased) version, as a '-   ' bullet ending with an :issue:`<number>` or :pr:`<number>` reference."
                    "Code under src/flask/sansio does no IO and does not use the Flask globals."))
  (command plan-feature #:template plan-feature #:model-set heavy #:output path #:validation (lint typecheck test)
           #:focus ("New tests go into the existing tests/test_<area>.py module of the source file being changed, as functions named test_<behaviour>; shared fixtures live in tests/conftest.py."
                    "A feature adds a CHANGES.rst entry under the topmost (unreleased) version, '.. versionadded:: <version>' or '.. versionchanged:: <version>' entries in the docstrings of the changed code, and documentation under docs."
                    "Code under src passes mypy with strict = true; every warning raised during the tests fails the suite."
                    "Code under src/flask/sansio does no IO and does not use the Flask globals."))
  (command plan-patch #:template plan-patch #:model-set heavy #:output path #:validation (lint typecheck test))
  (command resolve-test #:template resolve-test #:model-set heavy
           #:focus ("The lint cmd's hooks rewrite files in place and exit non-zero when they changed something: keep the rewritten files and fix only what the hooks still report."
                    "Every warning raised during the tests fails the suite (filterwarnings = error): fix the cause of the warning, do not add a filter."
                    "Imports are one name per 'from ... import ...' line; maximum line length is 88."))
  (command review #:template review #:model-set heavy #:output json
           #:focus ("A code change must come with tests in tests/test_<area>.py that fail without the change; the junit report from the test cmd must list them as passed."
                    "A code change must add a CHANGES.rst entry under the topmost (unreleased) version ending with an :issue: or :pr: reference."
                    "A change to documented behaviour must add a '.. versionchanged::' or '.. versionadded::' docstring entry and update the relevant file under docs."
                    "A feature is user-visible as a public API signature of the flask package, an HTTP response produced by a view under the test client, or flask CLI output; the junit report listing the new test cases as passed is its proof."))
  (command document #:template document
           #:focus ("Documentation is reStructuredText under docs; a change to documented behaviour also adds a '.. versionchanged:: <version>' or '.. versionadded:: <version>' entry in the docstring of the changed code."))
  (command commit #:template commit
           #:focus ("A commit message is one lower-case imperative summary line without a type prefix, for example: add `versionchanged` for ctx signature change (#6176)."))
  (command pull-request #:template pull-request
           #:focus ("The description links its issue with 'fixes #<issue number>'."))
  (hop run-plan #:template run-plan #:takes plan)
  (hop meta-command #:template meta-command #:produces command)
  (hop meta-adw #:template meta-adw #:produces adw)

  (expert sansio #:domain "The sans-IO application, blueprint and scaffold base classes, which must not do IO and must not use the Flask globals"
          #:paths ("src/flask/sansio"))

  (adw plan-build #:description "Classify, plan by work class and implement the plan"
       #:input (tasklist cli) #:trigger (cron manual)
       #:environment (worktree nix-devshell) #:review human #:agent claude-code
    (steps
      (run classify #:command classify #:args ("$prompt"))
      (run plan-chore #:command plan-chore #:args ("$prompt") #:when class-chore)
      (run plan-bug #:command plan-bug #:args ("$prompt") #:when class-bug)
      (run plan-feature #:command plan-feature #:args ("$prompt") #:when class-feature)
      (run build #:command run-plan #:args ("$plan"))))

  (adw sdlc #:description "Plan, build, lint, typecheck, test, document, commit; a human reviews the result"
       #:input (tasklist cli) #:trigger (cron manual)
       #:environment (worktree nix-devshell) #:review human #:agent claude-code
    (steps
      (compose plan-build)
      (check lint #:cmd lint #:resolve resolve-test #:collect (log))
      (check typecheck #:cmd typecheck #:resolve resolve-test #:collect (log))
      (check test #:cmd test #:resolve resolve-test #:collect (log junit))
      (run document #:command document #:args ("$plan"))
      (run commit #:command commit)
      (review #:patch plan-patch #:implement run-plan)
      (ship)))

  (adw zte #:description "Zero-touch: merges chores and bugs when lint, typecheck and test all pass"
       #:input (tasklist cli) #:trigger (cron manual)
       #:environment (worktree nix-devshell) #:review zero-touch #:agent claude-code #:classes (chore bug)
    (steps
      (compose plan-build)
      (check lint #:cmd lint #:resolve resolve-test #:collect (log))
      (check typecheck #:cmd typecheck #:resolve resolve-test #:collect (log))
      (check test #:cmd test #:resolve resolve-test #:collect (log junit))
      (run commit #:command commit)
      (review #:patch plan-patch #:implement run-plan)
      (ship))))
