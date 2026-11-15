# al — generate a codebase-specific agentic layer

`LLM.txt` (what the codebase is) → `agentic.rkt` (`#lang al`, authored by an LLM, checked by a compiler)
→ `agentic-layer.json` (IR, JSON Schema) → a generated layer directory **outside** the application:
command templates, HOPs, experts, ADWs (uv scripts), environments (Nix flake, Home Manager module,
podman image), triggers, forge integration, review and ship policy.

Design and the mapping to the Tactical Agentic Coding lessons: [`docs/DESIGN.md`](docs/DESIGN.md).
The whole language: [`docs/AL-DSL.llms.txt`](docs/AL-DSL.llms.txt) (generated).

## Demo

**[Watch the 3½-minute tour (MP4, 3 MB)](docs/demo/tour.mp4)** — every scene runs real commands; waits are compressed.
Re-record with `tools/demo/record.py tools/demo/scenes.toml` (captured output is in `docs/demo/logs/`).

From a codebase to its agentic layer (gin, Go):

![scout, LLM-authored DSL, generate, check](docs/demo/01-layer.gif)

The compiler is the feedback loop:

![source-located errors with alternatives](docs/demo/02-compiler.gif)

Zero-touch on flask with Claude Opus 5.5 — replay of a real run (3 min 17 s, $0.71, one attempt): plan, build,
flask's own lint/mypy/test suite in a container, review, gate, ship:

![zero-touch run](docs/demo/05-zero-touch-opus.gif)

More: [five codebases](docs/demo/03-five-codebases.gif) · [tasklist + cron](docs/demo/04-tasklist.gif) ·
[scaling: worktree + container per run](docs/demo/06-scaling.gif) · [live Forgejo](docs/demo/07-forgejo.gif) ·
[local voice](docs/demo/08-voice.gif) · [tests](docs/demo/09-tests.gif)

## Use it on a codebase

```bash
tools/al init ~/code/myapp                       # creates ~/code/myapp.al with a draft LLM.txt + the DSL reference
# 1. complete the descriptor      → run meta/commands/scout-codebase.md  with  $1=~/code/myapp  $2=~/code/myapp.al/LLM.txt
# 2. author the layer             → run meta/commands/author-layer.md    with  $1=LLM.txt $2=agentic.rkt $3="PITER preferences in plain words"
tools/al gen ~/code/myapp.al                     # compile + generate (errors are source-located; meta/commands/repair-layer.md)
tools/al verify ~/code/myapp.al                  # run every declared codebase command in its environment: is LLM.txt true?
tools/al check ~/code/myapp.al                   # layer complete, app tree untouched

cd ~/code/myapp.al
uv run adws/adw_plan-build.py "Add request-id logging" --dry-run     # mock agent, no LLM
uv run adws/adw_sdlc.py "Add request-id logging"                     # real agent
uv run adws/triggers/trigger_cron.py                                  # poll tasks.md / issues
uv run adws/triggers/trigger_voice.py                                 # talk to the codebase
```

`racket` must see the language: `export PLTCOLLECTS="$PWD/racket:"` (the `al` tool sets it itself).

## Develop and test

```bash
raco test racket/al/tests                                    # compiler rules
(cd kit/runtime && uv run --with pytest pytest -q)           # runtime, incl. real-podman sandbox escape tests
uv run --with pytest --with pyyaml --with jsonschema pytest tests/integration -q   # five pinned public repos, setup/teardown
tests/forge/forgejo_live.sh                                  # throwaway local Forgejo: issue → PR, zero-touch merge, webhook
racket racket/al/cli.rkt schema    > schemas/agentic-layer-v1.schema.json   # after editing vocab.rkt
racket racket/al/cli.rkt reference > docs/AL-DSL.llms.txt
```

The integration suite never works in `testbeds/`: per session it makes a private clone of each example
codebase at its pinned commit (from `testbeds/<name>` when present, else from the pinned URL), builds each
layer from `tests/authored/<name>/` in a temp dir, and its teardown fails any test that leaves a worktree,
branch, container or modified file behind. Tiers and markers (`slow`, `podman`, `nix`, `network`, `llm`): [`tests/README.md`](tests/README.md).

Adding a PITER alternative: one `define-kind` in `racket/al/private/vocab.rkt`, its runtime support in
`kit/runtime`, regenerate schema + reference.
