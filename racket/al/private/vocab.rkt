#lang racket/base

;; The agentic-layer vocabulary, declared with the meta-DSL (meta.rkt).
;; Adding a PITER alternative = adding one define-kind here. The compiler, the JSON Schema
;; and the LLM reference pick it up automatically. Runtime support lives in kit/runtime.

(require racket/list "meta.rkt")
(provide kit-command-templates kit-hop-templates artefact-types singleton-families semantic-rules)

;; ---------------------------------------------------------------------------------------
;; Singletons
;; ---------------------------------------------------------------------------------------

(define-family layer #:key "layer" #:letter "" #:default sibling
  "Where the generated agentic layer lives. It is never part of the application tree.")
(define-kind layer sibling
  "A directory next to the application checkout (default: <app>.al)."
  [dir string "" "Layer directory; empty = `<codebase root>.al` beside the app."]
  [git boolean #t "Initialise the layer as its own git repository."])
(define-kind layer central
  "A directory under a central layers home, e.g. ~/agentic-layers/<name>."
  [home string "~/agentic-layers" "Parent directory of all layers."]
  [git boolean #t "Initialise the layer as its own git repository."])
(define-kind layer separate-repo
  "The layer is its own remote repository; the app is referenced by URL only."
  [remote string #:required "Git remote URL of the layer repository."]
  [dir string "" "Local checkout directory; empty = `<name>.al`."])

(define-family agent #:key "agents" #:letter "" #:default claude-code
  "The coding agent a step runs on. ADWs name one; the runtime shells out to it headlessly.")
(define-kind agent claude-code
  "Claude Code in programmable mode (`claude -p`), stream-json output."
  [model-base string "claude-sonnet-5-5" "Model for the `base` model set (cheap steps: classify, commit, branch name)."]
  [model-heavy string "claude-opus-5-5" "Model for the `heavy` model set (plan, implement, review)."]
  [permission-mode (enum acceptEdits bypassPermissions plan default) acceptEdits
   "Headless permission mode. bypassPermissions only inside a podman environment."]
  [safe-mode boolean #t "Run with --safe-mode so user CLAUDE.md, hooks, plugins and skills are not loaded."]
  [allowed-tools (list-of string) ("Read" "Glob" "Grep" "Edit" "Write" "Bash")
   "Tools the headless agent may use without asking (passed as --allowedTools). Narrow it for read-only workflows."]
  [max-turns integer 60 "Turn cap per step."]
  [timeout integer 1800 "Wall-clock seconds per step."])
(define-kind agent codex
  "OpenAI Codex CLI (`codex exec`)."
  [model-base string "gpt-5.4-mini" "Model for the base set."]
  [model-heavy string "gpt-5.4" "Model for the heavy set."]
  [sandbox (enum read-only workspace-write danger-full-access) workspace-write "Codex sandbox mode."]
  [timeout integer 1800 "Wall-clock seconds per step."])
(define-kind agent custom
  "Any CLI agent. The command receives the rendered prompt on stdin."
  [command string #:required "Shell command; {model} and {cwd} are substituted."]
  [model-base string "" "Model for the base set."]
  [model-heavy string "" "Model for the heavy set."]
  [timeout integer 1800 "Wall-clock seconds per step."])
(define-kind agent mock
  "Deterministic stand-in used by tests and dry runs: no LLM, replays scripted responses."
  [script-dir string "mock" "Directory (in the layer) holding <step>.json scripted responses and optional <step>.patch files."]
  [timeout integer 60 "Wall-clock seconds per step."])

(define-family forge #:key "forge" #:letter "" #:default none
  "The code forge used for issues, PRs and CI integration.")
(define-kind forge none
  "No forge: branches stay local, review results are written to the layer."
  [remote string "" "Optional git remote to push to."])
(define-kind forge github
  "GitHub via the `gh` CLI."
  [repo string #:required "owner/name."]
  [host string "github.com" "GitHub host (Enterprise supported)."]
  [token-env string "GITHUB_TOKEN" "Environment variable holding the token (falls back to gh auth)."]
  [bot-marker string "[ADW-BOT]" "Prefix on every comment the layer posts, so triggers ignore their own comments."])
(define-kind forge forgejo
  "Forgejo / Gitea via the REST API (v1)."
  [host string #:required "Base URL, e.g. https://forgejo.example.org."]
  [repo string #:required "owner/name."]
  [token-env string "FORGEJO_TOKEN" "Environment variable holding the API token."]
  [bot-marker string "[ADW-BOT]" "Prefix on every comment the layer posts."])
(define-kind forge gitlab #:status planned
  "GitLab via `glab`."
  [repo string #:required "group/name."]
  [host string "gitlab.com" "GitLab host."]
  [token-env string "GITLAB_TOKEN" "Environment variable holding the token."])

(define-family storage #:key "storage" #:letter "" #:default local
  "Where test/review artefacts (logs, reports, screenshots, video) are kept. Local first.")
(define-kind storage local
  "Artefacts stay on disk inside the layer."
  [dir string "artefacts" "Directory relative to the layer."])
(define-kind storage s3
  "Any S3-compatible bucket (AWS S3, MinIO, Cloudflare R2, Garage). A local copy is always kept too."
  [bucket string #:required "Bucket name."]
  [prefix string "adw" "Key prefix."]
  [endpoint string "" "Endpoint URL for non-AWS stores; empty = AWS."]
  [region string "us-east-1" "Region."]
  [public-base-url string "" "If set, artefact links in reviews use this base URL."]
  [access-key-env string "AWS_ACCESS_KEY_ID" "Environment variable with the access key."]
  [secret-key-env string "AWS_SECRET_ACCESS_KEY" "Environment variable with the secret key."]
  [local-dir string "artefacts" "Local mirror directory relative to the layer."])

(define-family concurrency #:key "concurrency" #:letter "" #:default slots
  "How far the layer scales out. Every run has its own worktree; these limits are shared by all triggers and manual runs.")
(define-kind concurrency slots
  "File-lock slot pools under agents/.slots: a run, an agent call and a check each hold one slot of their pool."
  [max-runs integer 4 "ADW runs in flight at once (each in its own worktree)."]
  [max-agents integer 2 "Agent (LLM) calls in flight at once across all runs."]
  [max-checks integer 2 "Codebase check commands in flight at once across all runs (they are the CPU-heavy part)."]
  [wait-seconds integer 3600 "How long a run waits for a free slot before failing."])

(define-family voice #:key "voice" #:letter "" #:default #f
  "Local speech stack used by the voice prompt input and trigger, and for spoken summaries.")
(define-kind voice kokoro
  "Kokoro TTS (ONNX, local, English) + faster-whisper speech recognition, both as uv scripts."
  [tts-voice string "af_heart" "Kokoro voice id."]
  [tts-lang string "en-us" "Kokoro language code."]
  [tts-speed integer 100 "Speech speed in percent."]
  [stt-model string "base.en" "faster-whisper model size."]
  [stt-device (enum auto cpu cuda) auto "Inference device for recognition."]
  [record-seconds integer 8 "Push-to-talk recording length when no silence detection is available."]
  [speak-summaries boolean #t "Speak the run summary when an ADW finishes."])

;; ---------------------------------------------------------------------------------------
;; P — Prompt input
;; ---------------------------------------------------------------------------------------

(define-family prompt-input #:key "promptInputs" #:letter "P" #:default cli
  "PITER · Prompt input: where the high-level prompt for a run comes from.")
(define-kind prompt-input cli
  "The prompt is passed on the command line (or stdin) of the ADW script."
  [allow-stdin boolean #t "Read the prompt from stdin when no argument is given."])
(define-kind prompt-input issue
  "A forge issue (title + body). Requires a forge."
  [labels (list-of string) () "Only issues carrying all of these labels are picked up; empty = any."]
  [keyword string "adw" "Body/comment keyword prefix that selects a workflow, e.g. `adw: sdlc`."]
  [classify boolean #t "Run the classify command to map the issue to chore/bug/feature."])
(define-kind prompt-input tasklist
  "A plain-text task list (tasks.md) in the layer: one heading per worktree, one checkbox line per task."
  [path string "tasks.md" "Task file relative to the layer."]
  [open-marker string "[ ]" "Marker of a task that is ready to be picked up."]
  [running-marker string "[~]" "Marker written when an ADW claims the task (followed by the adw id)."]
  [done-marker string "[x]" "Marker written on success (followed by adw id and commit hash)."]
  [failed-marker string "[!]" "Marker written on failure (followed by adw id and reason)."]
  [blocked-marker string "[>]" "Marker of a task that waits for the tasks above it in the same section."]
  [tag-syntax (enum braces hash) braces "Tags select workflow/model: `{sdlc, heavy}` or `#sdlc #heavy`."])
(define-kind prompt-input spec-file
  "A hand-written spec/plan file dropped into a directory of the layer; planning is skipped."
  [dir string "specs/inbox" "Directory relative to the layer that is watched for new specs."]
  [pattern string "*.md" "Glob of files treated as specs."])
(define-kind prompt-input voice
  "Speak the prompt: local recording → speech recognition → text. Requires a `voice` declaration."
  [confirm boolean #t "Read the recognised prompt back and wait for a spoken/typed yes before running."]
  [route (enum task question summary auto) auto
   "What speech becomes: a new tasklist entry, a codebase question, a status summary, or classified automatically."]
  [append-to-tasklist boolean #t "When routed as task, append to the tasklist instead of running immediately."])
(define-kind prompt-input webhook-payload
  "A JSON payload posted to the webhook trigger by any external system."
  [prompt-field string "prompt" "Dotted path of the prompt text in the payload."]
  [workflow-field string "workflow" "Dotted path of the workflow (ADW) name in the payload."])
(define-kind prompt-input notion #:status planned
  "A Notion database row (status Not started → In progress → Done)."
  [database-id-env string "NOTION_DATABASE_ID" "Environment variable with the database id."]
  [status-filter (list-of string) ("Not started") "Statuses that are picked up."])
(define-kind prompt-input chat #:status planned
  "A chat message (Telegram / Slack / Matrix)."
  [platform (enum telegram slack matrix) telegram "Chat platform."]
  [token-env string "CHAT_TOKEN" "Environment variable with the bot token."])

;; ---------------------------------------------------------------------------------------
;; T — Trigger
;; ---------------------------------------------------------------------------------------

(define-family trigger #:key "triggers" #:letter "T" #:default manual
  "PITER · Trigger: the event that starts a run.")
(define-kind trigger manual
  "Run the ADW script by hand: `adws/adw_<name>.py <prompt-or-ref>`."
  [confirm boolean #f "Ask for confirmation before starting."])
(define-kind trigger cron
  "A polling loop (uv script, optionally a systemd user timer) that checks the prompt inputs."
  [interval-seconds integer 20 "Polling interval."]
  [max-concurrent integer 3 "Maximum ADW runs in flight."]
  [systemd boolean #f "Also generate a systemd user service + timer unit."])
(define-kind trigger webhook
  "A small HTTP server receiving forge webhooks or generic JSON payloads."
  [port integer 8001 "Listen port."]
  [path string "/webhook" "Endpoint path."]
  [bind string "127.0.0.1" "Bind address; put a tunnel/reverse proxy in front for remote forges."]
  [secret-env string "ADW_WEBHOOK_SECRET" "Environment variable with the HMAC secret."]
  [events (list-of (enum issue-opened issue-comment issue-labeled payload)) (issue-opened issue-comment)
   "Which events start a run."])
(define-kind trigger forge-actions
  "A CI workflow file for the forge (GitHub Actions / Forgejo Actions) that runs the ADW on a runner."
  [events (list-of (enum issue-opened issue-comment issue-labeled schedule workflow-dispatch))
   (issue-opened issue-comment workflow-dispatch) "Workflow events."]
  [schedule string "" "Cron expression when `schedule` is among the events."]
  [runner string "self-hosted" "runs-on label. A self-hosted runner keeps the agent on hardware you control."]
  [label string "adw" "Issue label that must be present for issue events."])
(define-kind trigger file-watch
  "Watch a file or directory in the layer (tasklist, spec inbox) and start when it changes."
  [debounce-seconds integer 3 "Quiet period before a change is acted on."])
(define-kind trigger voice
  "Push-to-talk loop: press Enter, speak, the recognised text is routed by the voice prompt input."
  [hotkey string "enter" "Key that starts recording in the terminal loop."])

;; ---------------------------------------------------------------------------------------
;; E — Environment
;; ---------------------------------------------------------------------------------------

(define-family environment #:key "environments" #:letter "E" #:default worktree
  "PITER · Environment: where the agent works. An ADW lists one or more; they stack in order (worktree → nix → podman).")
(define-kind environment host
  "The application checkout itself, no isolation. Only for read-only workflows."
  [read-only boolean #t "Refuse steps that are expected to write."])
(define-kind environment worktree
  "A dedicated git worktree per run under the layer (trees/<adw-id>), so the app's working tree is never touched."
  [dir string "trees" "Worktree parent directory relative to the layer."]
  [port-base integer 9100 "First port of the per-run port block."]
  [ports-per-run integer 2 "Ports reserved per run (exported as ADW_PORT_0..n)."]
  [copy-files (list-of string) () "Untracked files copied from the app checkout into the worktree (e.g. .env)."]
  [keep (enum never on-failure always) on-failure "When the worktree is kept after the run."]
  [sparse (list-of string) () "Sparse-checkout these directories only (cone mode); empty = full checkout. For large monorepos."]
  [gc-days integer 7 "Kept worktrees of finished runs older than this are removed when a new run starts; 0 = never."]
  [delete-merged-branches boolean #t "When a run's worktree is removed, delete its branch if it holds nothing that is not already on the default or al/ship branch."])
(define-kind environment nix-devshell
  "A Nix flake dev shell generated in the layer; every codebase command and the agent run inside `nix develop`."
  [packages (list-of string) #:required "nixpkgs attribute names providing the toolchain (e.g. python312 uv)."]
  [nixpkgs string "github:NixOS/nixpkgs/nixos-unstable" "nixpkgs flake reference."]
  [shell-hook string "" "Extra shell hook."]
  [pure boolean #f "Use `nix develop --ignore-environment` (keeps HOME, PATH is nix-only)."])
(define-kind environment home-manager
  "A Home Manager module generated in the layer that installs the toolchain and the layer's helper commands for a user."
  [packages (list-of string) #:required "nixpkgs attribute names."]
  [module-name string "agentic-layer" "Option namespace: programs.<module-name>.enable."]
  [state-version string "25.05" "home.stateVersion used by the standalone example configuration."])
(define-kind environment podman
  "A light rootless podman container; the worktree is bind-mounted, nothing else of the host is visible."
  [base-image string "docker.io/library/debian:stable-slim" "Base image."]
  [packages (list-of string) () "Distribution packages installed in the image (apt names for Debian bases)."]
  [setup (list-of string) () "Extra RUN lines for the Containerfile."]
  [network (enum none host slirp) slirp "Network mode: `slirp` = podman's default rootless user-mode network, `none` = fully offline checks, `host` = host network."]
  [memory string "4g" "Memory limit."]
  [cpus string "2" "CPU limit."]
  [agent-inside boolean #f "Run the agent CLI inside the container too, so the agent itself is sandboxed (the image must provide the agent CLI). Otherwise only codebase commands run inside."]
  [scope (enum run command) run "`run` = one container per ADW run, every step execs into it (installed dependencies persist for the run); `command` = a fresh container per command."]
  [caches (list-of string) () "Named cache volumes shared across runs, mounted at /caches/NAME. Known names also set the tool's cache variable: uv, pip, cargo, go, npm, pre-commit."]
  [credentials (list-of (enum claude codex)) () "Agent credentials made available inside the container (read-only auth file + API key variable). Only meaningful with #:agent-inside #t."])

;; ---------------------------------------------------------------------------------------
;; R — Review
;; ---------------------------------------------------------------------------------------

(define-family review #:key "reviews" #:letter "R" #:default human
  "PITER · Review: how the result is judged and what happens after.")
(define-kind review human
  "Open a pull request and stop. A human reads it (presence = 2: prompt and review)."
  [reviewers (list-of string) () "Forge users requested as reviewers."]
  [draft boolean #f "Open the PR as draft."])
(define-kind review agent
  "An agent reviews the diff against the spec, with artefacts as proof, and posts a structured verdict. A human still merges."
  [command (ref command) review "Review command."]
  [artefacts (list-of (ref artefact)) () "Artefacts attached as proof."]
  [reviewers integer 1 "Independent review agents; all must pass ('double agent check')."]
  [max-retries integer 3 "Patch-and-re-review attempts on blockers before stopping for a human."])
(define-kind review auto
  "Zero-touch: agent review + deterministic gates; if everything passes the change ships without a human."
  [command (ref command) review "Review command."]
  [artefacts (list-of (ref artefact)) () "Artefacts collected, stored and given to the reviewer as proof."]
  [reviewers integer 1 "Independent review agents; all must pass."]
  [max-retries integer 3 "Patch-and-re-review attempts on blockers."]
  [ship (enum pr merge deploy) merge "On pass: open PR only, merge to the default branch, or merge and run the deploy command."]
  [deploy-cmd (optional-ref cmd) "" "Codebase command run after merge when ship = deploy."]
  [require-checks (list-of (ref cmd)) #:required "Codebase commands that must exit 0 in the final state (the deterministic gate)."]
  [allowed-classes (list-of (enum chore bug feature patch)) (chore bug)
   "Work classes allowed to ship untouched; others fall back to a human PR."]
  [max-diff-lines integer 400 "Larger diffs fall back to a human PR."]
  [block-protected-paths boolean #t "Any change under codebase protected paths falls back to a human PR."])

;; ---------------------------------------------------------------------------------------
;; Artefacts
;; ---------------------------------------------------------------------------------------

(define-family artefact #:key "artefacts" #:letter "" #:default #f
  "Proof a check or review produces. Collected from the worktree after the producing step and put in storage.")
(define-kind artefact log
  "Captured stdout/stderr of codebase commands (always available)."
  [tail-lines integer 200 "Lines of each log shown to the reviewer."])
(define-kind artefact junit
  "JUnit XML test report."
  [glob string #:required "Glob relative to the worktree."])
(define-kind artefact coverage
  "Coverage report (lcov, cobertura xml, or text)."
  [glob string #:required "Glob relative to the worktree."])
(define-kind artefact image
  "Screenshots or rendered images; the reviewer reads every one."
  [glob string #:required "Glob relative to the worktree."]
  [max-files integer 12 "Upper bound handed to the reviewer."])
(define-kind artefact video
  "Screen/session recordings; keyframes are extracted with ffmpeg for the reviewer."
  [glob string #:required "Glob relative to the worktree."]
  [keyframes integer 6 "Evenly spaced frames extracted per video."])
(define-kind artefact audio
  "Audio files (e.g. generated speech)."
  [glob string #:required "Glob relative to the worktree."])
(define-kind artefact trace
  "Tool traces such as Playwright trace.zip."
  [glob string #:required "Glob relative to the worktree."])
(define-kind artefact html
  "HTML reports."
  [glob string #:required "Glob relative to the worktree."])
(define-kind artefact file
  "Any other file worth keeping."
  [glob string #:required "Glob relative to the worktree."])

;; ---------------------------------------------------------------------------------------
;; Records (forms with their own surface syntax; see compiler.rkt)
;; ---------------------------------------------------------------------------------------

(define-record codebase
  "The application the layer wraps. Everything codebase-specific that templates need lives here."
  [root string #:required "Path of the application checkout, relative to the agentic.rkt file or absolute."]
  [languages (list-of symbol) #:required "Languages, most important first."]
  [default-branch string "main" "Branch that ships."]
  [description string "" "One-paragraph description used by prime/plan templates."]
  [package-manager string "" "How dependencies are added (e.g. `uv add`, `cargo add`)."]
  [readme string "README.md" "Entry document agents read first."])

(define-record cmd
  "A named shell command of the codebase: (cmd NAME \"shell\" …). Check steps and validation sections refer to these names."
  [run string #:required "Shell command, run from the worktree root."]
  [timeout integer 900 "Seconds."]
  [description string "" "What the command proves."])

(define-record paths
  "Where things are: (paths #:source (…) #:tests (…) …)."
  [source (list-of string) () "Source directories."]
  [tests (list-of string) () "Test directories."]
  [docs (list-of string) () "Documentation directories."]
  [protected (list-of string) () "Paths agents must not change without a human (CI config, release, security, migrations…)."])

(define-record conditional-doc
  "(conditional-doc \"path\" \"condition\" …): documentation an agent must read when the condition holds."
  [path string #:required "Document path relative to the codebase root."]
  [when string #:required "Condition in plain language."])

(define-record command
  "(command NAME #:template T …): instantiate a kit template as a command of this layer."
  [template (enum prime install start plan-feature plan-bug plan-chore plan-patch implement build
                  test resolve-test e2e-test review document commit pull-request classify
                  branch-name summarize question self-improve track-kpis custom)
            #:required "Kit template to instantiate."]
  [description string "" "One-line purpose."]
  [file string "" "For template custom: path of a hand-written template relative to the layer."]
  [model-set (enum base heavy) base "Model set the step runs on."]
  [output (enum text json path) text "What the command must return: free text, JSON per its schema, or a file path."]
  [focus (list-of string) () "Codebase-specific instructions injected into the template's Instructions section."]
  [relevant (list-of string) () "Codebase paths listed under Relevant Files; empty = codebase source+tests paths."]
  [validation (list-of (ref cmd)) () "Codebase commands listed as Validation Commands."]
  [fabric (list-of symbol) () "Fabric patterns vendored into the layer and applied by this command."])

(define-record hop
  "(hop NAME #:template T …): a higher-order prompt — it takes another prompt, plan or command as its argument."
  [template (enum run-plan meta-command meta-adw meta-expert plan-build-improve) #:required "Kit HOP template."]
  [description string "" "One-line purpose."]
  [takes (enum prompt plan command spec) prompt "What the argument is."]
  [produces (enum changes command adw expert plan) changes "What comes out."]
  [model-set (enum base heavy) heavy "Model set."])

(define-record expert
  "(expert NAME …): an agent expert — an expertise file the agent maintains itself (act → learn → reuse)."
  [domain string #:required "What the expert knows, in one sentence."]
  [paths (list-of string) #:required "Codebase paths the expertise is validated against."]
  [max-lines integer 1000 "Line cap of the expertise file."])

(define-record adw
  "(adw NAME … (steps STEP …)): an AI Developer Workflow. The four PITER choices default to the first declared option of each family."
  [description string "" "One-line purpose."]
  [input (list-of (ref prompt-input)) #:first "Prompt inputs this ADW accepts."]
  [trigger (list-of (ref trigger)) #:first "Triggers that may start it."]
  [environment (list-of (ref environment)) #:first "Environment stack, outermost first."]
  [review (ref review) #:first "Review option."]
  [agent (ref agent) #:first "Agent the steps run on."]
  [classes (list-of (enum chore bug feature patch)) () "Work classes routed to this ADW by classification; empty = any."])

(define-record step-run
  "(run NAME #:command C …): one agent, one prompt, one purpose."
  [command (ref command-or-hop) #:required "Command or HOP to execute."]
  [args (list-of string) () "Arguments; $prompt, $plan, $adw_id, $class, $branch and $out.<step> are substituted."]
  [model-set (enum inherit base heavy) inherit "Override the command's model set."]
  [retries integer 0 "Re-run on failure."]
  [when (enum always class-feature class-bug class-chore class-patch) always "Run only for this work class."])

(define-record step-check
  "(check NAME #:cmd C …): a deterministic feedback loop. Runs a codebase command; on failure an agent resolves and the command re-runs."
  [cmd (ref cmd) #:required "Codebase command."]
  [resolve (optional-ref command) "" "Command given the failing output; empty = fail the run immediately."]
  [retries integer 3 "Resolve attempts."]
  [collect (list-of (ref artefact)) () "Artefacts collected after the command."])

(define-record step-review
  "(review [NAME] …): run the ADW's review option here."
  [patch (optional-ref command) "" "Command that plans a patch for a blocker; implemented via the ADW's implement command."]
  [implement (optional-ref command-or-hop) "" "Command/HOP that implements the patch plan."])

(define-record step-ship
  "(ship [NAME]): apply the review option's ship policy (PR / merge / deploy). Must come after a review step."
  [message string "" "Extra text for the PR/merge message."])

;; ---------------------------------------------------------------------------------------
;; Derived lists and the semantic rules the compiler enforces (also printed in the reference)
;; ---------------------------------------------------------------------------------------

(define (enum-values record-name wanted)
  (define f (findf (lambda (f) (eq? (field-name f) wanted))
                   (record-fields (find-record record-name))))
  (cdr (field-type f)))

;; Templates shipped in kit/templates/commands and kit/templates/hops.
(define kit-command-templates (enum-values 'command 'template))
(define kit-hop-templates (enum-values 'hop 'template))
(define artefact-types (map kind-name (family-kinds (find-family 'artefact))))

;; Families that hold exactly one option (emitted as an object, not an array).
(define singleton-families '(layer forge storage voice concurrency))

(define semantic-rules
  '("Exactly one (codebase …) clause and at least one (adw …) clause."
    "Every name is declared before it is used anywhere: (ref X) fields must name a declared option id, command, hop, cmd, artefact or adw."
    "Option ids default to the kind name; give #:id when you declare two options of the same kind."
    "Command and hop names share one namespace; step names are unique within an ADW (after compose expansion)."
    "A (ship) step must come after a (review) step in the same ADW."
    "An ADW whose review option is `auto` with #:ship merge or deploy must contain at least one (check …) step before its (review) step, and #:require-checks must not be empty: zero-touch needs a closed loop."
    "#:ship deploy needs #:deploy-cmd."
    "prompt-input `issue`, trigger `forge-actions`, and webhook issue events need a forge other than `none`."
    "prompt-input `voice` and trigger `voice` need a (voice …) declaration."
    "An ADW's environment stack has at most one option per kind; `host` cannot be combined with `worktree`; an ADW with run steps that write needs `worktree`."
    "claude-code #:permission-mode bypassPermissions is only accepted for ADWs whose environment stack contains a podman option with #:agent-inside #t (the agent itself must be sandboxed)."
    "An ADW whose review option is `auto` with #:ship merge or deploy must have a podman option in its environment stack: nothing ships untouched from an unsandboxed environment."
    "(compose ADW) inlines another ADW's steps; cycles are rejected."
    "Kinds marked planned compile but are refused by the generator."))
