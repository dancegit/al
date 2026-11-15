# The example layers, explained

Each page walks through one layer definition (`agentic.rkt`) block by block: what the block says in plain
language, the defaults the author left out, and the source. Every page starts with an animation that
steps through the file. The pages and animations are generated with `al explain`, so you can produce the
same for your own layer:

```bash
tools/al explain ~/code/myapp.al                                  # plain text in the terminal
tools/al explain ~/code/myapp.al --md layer.md --gif layer.gif    # a document and an animation
```

| Layer | Codebase | What is distinctive |
|---|---|---|
| [flask](flask.md) | Python web framework | task file + poller; a zero-touch workflow in a container next to a human-reviewed one |
| [ripgrep](ripgrep.md) | Rust command-line tool | Forgejo issues + webhook; proof stored on S3; an agent expert for the search core |
| [gin](gin.md) | Go web framework | GitHub issues + CI trigger; checks in a container so fixed ports cannot collide |
| [express](express.md) | JavaScript web framework | voice and task file; an "ask the codebase" workflow |
| [jq](jq.md) | C command-line tool | spec files as input; Debian build container; opens a pull request, never merges |
| [notes](notes.md) | small Notes web app | screenshots and video as proof; human review and two-agent review |
| [rota](rota.md) | healthcare staffing app | isolation test as its own check; protected migrations; a planted-defect workflow |

The first five were written by an LLM from each repository's description and are kept exactly as written
(`tests/authored/`). The last two are the demo applications' layer templates (`tests/webdemo`, `tests/rotademo`).
