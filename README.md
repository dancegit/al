# al — generate a codebase-specific agentic layer

`LLM.txt` (what the codebase is) → `agentic.rkt` (`#lang al`, authored by an LLM, checked by a compiler)
→ `agentic-layer.json` (IR, JSON Schema) → a generated layer directory **outside** the application:
command templates, HOPs, experts, ADWs (uv scripts), environments (Nix flake, Home Manager module,
podman image), triggers, forge integration, review and ship policy.

Design and the mapping to the Tactical Agentic Coding lessons: [`docs/DESIGN.md`](docs/DESIGN.md).
The whole language: [`docs/AL-DSL.llms.txt`](docs/AL-DSL.llms.txt) (generated).

## Use it on a codebase

```bash
tools/al init ~/code/myapp                       # creates ~/code/myapp.al with a draft LLM.txt + the DSL reference
# 1. complete the descriptor      → run meta/commands/scout-codebase.md  with  $1=~/code/myapp  $2=~/code/myapp.al/LLM.txt
# 2. author the layer             → run meta/commands/author-layer.md    with  $1=LLM.txt $2=agentic.rkt $3="PITER preferences in plain words"
tools/al gen ~/code/myapp.al                     # compile + generate (errors are source-located; meta/commands/repair-layer.md)
tools/al check ~/code/myapp.al                   # layer complete, app tree untouched

cd ~/code/myapp.al
uv run adws/adw_plan-build.py "Add request-id logging" --dry-run     # mock agent, no LLM
uv run adws/adw_sdlc.py "Add request-id logging"                     # real agent
uv run adws/triggers/trigger_cron.py                                  # poll tasks.md / issues
uv run adws/triggers/trigger_voice.py                                 # talk to the codebase
```

`racket` must see the language: `export PLTCOLLECTS="$PWD/racket:"` (the `al` tool sets it itself).

## Develop

```bash
raco test racket/al/tests                         # compiler
(cd kit/runtime && uv run --with pytest pytest -q)   # runtime
tests/acceptance.sh                               # five public testbeds end to end (see below)
racket racket/al/cli.rkt schema    > schemas/agentic-layer-v1.schema.json   # after editing vocab.rkt
racket racket/al/cli.rkt reference > docs/AL-DSL.llms.txt
```

Testbeds for the acceptance suite (git-ignored): clone `pallets/flask`, `BurntSushi/ripgrep`,
`gin-gonic/gin`, `expressjs/express`, `jqlang/jq` into `testbeds/`, `tools/al init testbeds/X --layer layers/X`,
then author each `layers/X/agentic.rkt` with the meta commands.

Adding a PITER alternative: one `define-kind` in `racket/al/private/vocab.rkt`, its runtime support in
`kit/runtime`, regenerate schema + reference.
