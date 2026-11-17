---
description: resolve-conflicts for <NAME>
argument-hint: [integration-brief] [conflicts-json]
---
## Your step

${workflow_steps}

Leave your work as uncommitted changes in the worktree. Do not commit, merge into any branch, push, tag, open a pull request, or change any ref. The runtime checks this when you finish: a step that moved HEAD or a ref, changed the main checkout, or left a file at the top of the layer directory fails, and what it did is undone.

Git in this step is for reading (`git status`, `git diff`, `git log`, `git show`) and, when the plan asks, for bringing content into the working tree (`git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>`, `git apply`), always ending uncommitted. Never run in this step: `git commit`, `git merge` without `--no-commit --squash`, `git rebase`, `git cherry-pick`, `git revert`, `git reset` to another commit, `git push`, `git tag`, `git branch`, `git switch`, `git checkout -b`, `git stash`, `git update-ref`.

Notes, scratch files and helper scripts go in `${scratch_dir}`, and nowhere else in the layer.

# Resolve Conflicts

`resolve-conflicts` in layer `<NAME>`: the reviewed work of another run is applied onto the target branch and conflicts with what landed there since. Resolve every conflict so that both sides' behaviour is complete, and report each resolution.

## Variables

BRIEF: $1
CONFLICTS: $2

## Instructions

- Work only inside `${worktree}` (your current directory). Never commit, switch branches, push, or touch the main checkout.
- The worktree is the target branch `${default_branch}` with the reviewed work of another run applied and left uncommitted. Conflict markers are three-way: `<<<<<<<` opens the target's version, `|||||||` the common base, `=======` the run's version, and `>>>>>>>` closes it.
- Read BRIEF first: it names the run, its request, its plan, and who changed each conflicted file on the target since the run's base.
- Resolve every conflict so that the behaviour of both sides is complete: what the target gained since the base stays, and what the run added stays.
- Where both sides changed the same function, write one that serves both. Do not pick a side and drop the other.
- Never drop, skip or weaken a test of either side. When both sides changed the same test, keep every assertion of both.
- A file that CONFLICTS marks `generated` is not merged by hand: take either side, run its `regenerate` command from the worktree root (when it is empty, find the generator named in the file's header) and keep the output.
- Do not change anything that is not needed for the integration: no refactoring, no unrelated fixes, no dependency changes unless a conflict is in a dependency file (then use `(not specified — do not add dependencies without flagging it)`).
- Change nothing under a protected path.
- When a conflict is a real disagreement (the two sides want different behaviour and no single version serves both), change nothing more for that file, list it under `undecidable` with the question a person must answer, and stop.
- Leave no conflict marker in any file.
- Run the checks that cover the files you touched, and fix what your resolutions broke.


## Relevant Files

- `README.md`
- `crates/core`
- `crates/matcher/src`
- `crates/regex/src`
- `crates/pcre2/src`
- `crates/searcher/src`
- `crates/printer/src`
- `crates/grep`
- `crates/cli/src`
- `crates/globset/src`
- `crates/ignore/src`
- `build.rs`
- `tests`
- `crates/ignore/tests`
- `crates/matcher/tests`

Protected paths:

- `.github`
- `ci`
- `pkg`
- `HomebrewFormula`
- `Cargo.lock`
- `fuzz/Cargo.lock`
- `.cargo/config.toml`
- `RELEASE-CHECKLIST.md`
- `AI_POLICY.md`
- `CONTRIBUTING.md`
- `COPYING`
- `LICENSE-MIT`
- `UNLICENSE`

## Conditional Documentation

- `GUIDE.md` — When changing behaviour it documents: automatic filtering, globs, file types, replacements, the configuration file, file encoding, binary data, preprocessors
- `FAQ.md` — When changing behaviour that an FAQ answer describes
- `CHANGELOG.md` — When making a user-visible change, to add the entry under TBD
- `crates/core/README.md` — When changing files under crates/core
- `crates/ignore/benches/README.md` — When changing directory walking or ignore matching in crates/ignore/src
- `fuzz/README.md` — When changing glob parsing in crates/globset/src or anything under fuzz
- `pkg/windows/README.md` — When changing build.rs or pkg/windows/Manifest.xml
- `RELEASE-CHECKLIST.md` — When asked to change a version number, Cargo.lock or anything related to a release
- `AI_POLICY.md` — When work is intended for the upstream repository BurntSushi/ripgrep



## Workflow

1. Read BRIEF and CONFLICTS; run `git status --short` to see the conflicted files.
2. For each conflicted file that is not generated: read the three versions between the markers, read the commits BRIEF names for it (`git show <commit> -- <file>`), and write the version that serves both sides.
3. For each generated file: take either side (`git checkout --theirs -- <file>` or `--ours`), then run its `regenerate` command.
4. Search the worktree for remaining markers (`git diff --check`, and a search for a line starting with seven `<`).
5. Run the checks that cover the files you touched. Loop to step 2 until they pass or you hit a stop condition above.

## Validation Commands

Run those that cover the files you touched. For reference, every command of the codebase:

- **format**: `cargo fmt --all --check` — every Rust file in the workspace is formatted as rustfmt.toml requires
- **build**: `cargo build --verbose --workspace` — the rg binary and every workspace crate compile; produces target/debug/rg
- **test**: `cargo test --verbose --workspace --features pcre2` — unit tests, doc tests and the integration target pass for every workspace crate, each integration test once on the default regex engine and once on PCRE2; builds target/debug/rg
- **e2e**: `ci/test-complete` — the options in crates/core/flags/complete/rg.zsh equal the options in rg --help; needs zsh and an existing target/release/rg or target/debug/rg, so it must run after build or test
- **docs**: `RUSTDOCFLAGS="-D warnings" cargo doc --no-deps --document-private-items --workspace` — rustdoc for every workspace crate, private items included, builds with no warnings

## Report

- One bullet per conflict: the file, what each side did, and the resolution.
- One bullet per regenerated file, with the command you ran.
- The exit code of each check you ran.
- Then exactly one fenced `json` block with every conflicted file in one of its three lists:

```json
{"resolved": [{"file": "", "sides": "", "resolution": ""}], "regenerated": [{"file": "", "command": ""}], "undecidable": [{"file": "", "question": ""}]}
```

End the report with this section, filled from commands you run (`git rev-parse HEAD` before your first change and again now; `git status --short`):

### State of the worktree
- HEAD before: `<sha>`; HEAD after: `<sha>` (must be equal)
- `git status --short`: <n> modified, <n> added, <n> deleted, <n> untracked
- Changed outside the plan's scope: <paths, or "none">
- Nothing is committed; nothing left this worktree.

Wording: say "applied to the worktree". Never write "merged onto main", "shipped" or "delivered": no step before `ship` does any of those.

Finish with a concise report in plain markdown. No preamble.
