#lang al
;; A layer that uses most of the vocabulary: several prompt inputs and triggers,
;; a stacked environment, artefacts on S3, and a zero-touch (auto review → merge) ADW
;; next to a human-reviewed one, model tiers, a second vendor as reviewer, spending limits, and a
;; script step that runs one of the team's own scripts, and notifications when a run needs a person.
(agentic-layer #:name "webshop" #:description "Agentic layer for the webshop monorepo"
  (codebase #:root "../webshop" #:languages (typescript python) #:default-branch "main"
            #:description "Vue storefront + FastAPI backend" #:package-manager "bun add / uv add"
    (cmd install "bun install && (cd api && uv sync)" #:timeout 1200)
    (cmd lint "bun run lint && (cd api && uv run ruff check)")
    (cmd test "bun run test -- --reporter=junit --outputFile=reports/unit.xml && (cd api && uv run pytest -q)")
    (cmd e2e "bun run e2e" #:timeout 1800 #:description "Playwright; writes e2e-results/")
    (cmd deploy "./scripts/deploy.sh staging")
    (cmd changelog "./scripts/changelog.sh" #:description "the team's own script: appends the change to CHANGELOG.md")
    (paths #:source ("web/src" "api/app") #:tests ("web/tests" "api/tests") #:docs ("docs")
           #:protected (".github" "api/migrations" "scripts/deploy.sh"))
    (conditional-doc "docs/payments.md" "When touching anything under api/app/payments"))

  (layer sibling)
  (forge forgejo #:host "https://forgejo.example.org" #:repo "acme/webshop")
  (storage s3 #:bucket "webshop-adw" #:endpoint "https://s3.example.org")
  (voice kokoro)
  (agent claude-code #:models ((fast "claude-haiku-5-5") (heavy "claude-fable-5-2" #:effort high)))
  (agent codex #:id second-opinion)
  (budget limits #:run-usd 5 #:step-usd 2 #:day-usd 50)
  (notify email #:events (needs-human failed shipped))   ; addresses and the server: variables in local.env, never here
  (notify command #:id desktop #:run "notify-send \"$AL_NOTIFY_SUBJECT\"" #:events (needs-human))

  (prompt-input issue #:labels ("adw"))
  (prompt-input tasklist)
  (prompt-input voice #:route auto)
  (trigger webhook #:port 8001)
  (trigger cron #:interval-seconds 15 #:systemd #t)
  (trigger voice)
  (trigger manual)

  (environment worktree #:copy-files (".env"))
  (environment nix-devshell #:packages ("bun" "nodejs_22" "python312" "uv" "ffmpeg"))
  (environment podman #:id sandbox #:packages ("git" "curl") #:network slirp #:caches ("npm" "uv"))

  (artefact log)
  (artefact junit #:glob "reports/*.xml")
  (artefact image #:id screenshots #:glob "e2e-results/**/*.png")
  (artefact video #:id recordings #:glob "e2e-results/**/*.webm" #:keyframes 6)

  (review human)
  (concurrency slots #:max-runs 4 #:max-agents 2)
  (dashboard local #:port 8765 #:also ("../storefront.al") #:systemd #t)
  (review auto #:id zero-touch #:artefacts (log junit screenshots recordings) #:agent second-opinion
          #:ship deploy #:deploy-cmd deploy #:require-checks (lint test e2e)
          #:allowed-classes (chore bug) #:max-diff-lines 300)

  (command prime #:template prime)
  (command classify #:template classify #:model-set fast #:output json)
  (command chore #:template plan-chore #:model-set heavy #:output path #:validation (lint test))
  (command bug #:template plan-bug #:model-set heavy #:output path #:validation (lint test e2e))
  (command feature #:template plan-feature #:model-set heavy #:output path #:validation (lint test e2e)
           #:fabric (create_prd)
           #:focus ("New endpoints need a pytest module under api/tests mirroring the source path."))
  (command resolve-test #:template resolve-test #:model-set heavy)
  (command review #:template review #:model-set heavy #:output json #:fabric (review_code))
  (command patch #:template plan-patch #:model-set heavy #:output path)
  (command document #:template document)
  (command commit #:template commit #:model-set fast #:fabric (create_git_diff_commit))
  (command pull-request #:template pull-request #:fabric (write_pull-request))
  (hop implement #:template run-plan #:takes plan)
  (hop new-command #:template meta-command #:produces command)
  (hop new-adw #:template meta-adw #:produces adw)

  (expert payments #:domain "Payment provider integration and reconciliation" #:paths ("api/app/payments"))

  (adw plan-build #:description "Plan and implement, nothing else" #:input (issue tasklist voice)
    (steps
      (run classify #:command classify #:args ("$prompt"))
      (run plan-chore #:command chore #:args ("$prompt") #:when class-chore)
      (run plan-bug #:command bug #:args ("$prompt") #:when class-bug)
      (run plan-feature #:command feature #:args ("$prompt") #:when class-feature)
      (run build #:command implement #:args ("$plan"))))

  (adw sdlc #:description "Plan, build, test, review; a human merges"
       #:input (issue tasklist) #:trigger (webhook cron manual)
       #:environment (worktree nix-devshell) #:review human
    (steps
      (compose plan-build)
      (check lint #:cmd lint #:resolve resolve-test)
      (check test #:cmd test #:resolve resolve-test #:collect (junit))
      (run document #:command document #:args ("$plan"))
      (script changelog #:cmd changelog #:args ("$prompt"))
      (run commit #:command commit)
      (review)
      (ship)))

  (adw zte #:description "Zero-touch: ships chores and bugs when every gate passes"
       #:input (issue tasklist) #:trigger (webhook cron)
       #:environment (worktree nix-devshell sandbox) #:review zero-touch #:classes (chore bug)
    (steps
      (compose plan-build)
      (check lint #:cmd lint #:resolve resolve-test)
      (check test #:cmd test #:resolve resolve-test #:collect (junit))
      (check e2e #:cmd e2e #:resolve resolve-test #:retries 2 #:collect (screenshots recordings))
      (run commit #:command commit)
      (review #:patch patch #:implement implement)
      (ship)))

  (adw candidate #:description "One candidate of a race: plan, build, test, commit; never ships"
       #:input (tasklist) #:trigger (manual) #:environment (worktree nix-devshell) #:review human
    (steps
      (compose plan-build)
      (check test #:cmd test #:resolve resolve-test)
      (run commit #:command commit)))

  (adw race #:description "Three candidates race (two at a time); a human reviews the best one"
       #:input (tasklist) #:trigger (manual) #:environment (worktree nix-devshell) #:review human
    (steps
      (fan-out race #:adw candidate #:count 3 #:pick best #:max-parallel 2)
      (check lint #:cmd lint #:resolve resolve-test)
      (check test #:cmd test #:resolve resolve-test #:collect (junit))
      (review)
      (ship))))
