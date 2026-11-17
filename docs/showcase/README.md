# Showcase clips

Short recordings of features that work and that the main README did not show yet. Every frame is real
output of this repository: the dashboard in a real Chromium, or `tools/al` in a terminal, run on the test
suites' own fixtures (invented names: notes, larder, fern, demo-shop, Ada Example). No model is called; the
mock agent plays it, and where a clip needs a machine it cannot have (a podman, a remote host) it uses the
test suite's stand-in and says so on its opening card.

![Contact sheet: the last frame of each clip](contact-sheet.png)

## Regenerate

```sh
uv run tools/showcase/make.py                # all nine clips, the contact sheet, the logs
uv run tools/showcase/make.py 05 adopt       # some: by number, name or stem (05-delivery)
uv run tools/showcase/make.py --out /tmp/x --media /tmp/x/media   # elsewhere; --keep-work keeps each clip's temporary folder
```

No video is kept in the tree. Each MP4 is written to `media/media-v1/` (ignored by git) under the name it
has as a release asset, `showcase-NN-name.mp4`, and its size and sha256 are recorded in
[`docs/media.json`](../media.json); a person uploads the folder with `tools/release_media.sh`, which lists
the files and prints the `gh release` commands without running them.

It needs uv, `ffmpeg` and `ffprobe`, the DejaVu Sans Mono font (`fonts-dejavu-core`, or a monospace TTF in
`AL_SHOWCASE_FONT`) and, for the browser clips, Chrome or Chromium (found on PATH, or `AL_DASHBOARD_CHROME`).
Nothing is downloaded but uv's packages (`pillow`, `playwright`, `pytest`, from the script's inline
metadata): Playwright drives the host's browser, and its video recorder is pointed at the system `ffmpeg`
instead of the copy it would fetch. The layer's sandbox image has all of it.

Each clip is recorded in its own temporary folder with a neutral environment: `HOME` is a folder in it,
the work folder is `~/work`, the git identity is `Showcase <showcase@example.org>`, the time zone is UTC,
none of the caller's `AL_*` settings reach it and PATH holds only links to racket, raco, uv, git, python3
and sh plus the system folders. In the terminal clips the work folder is shown as `~/work` and the neutral
home as `~`; that substitution is the only change made to captured text. Before anything is rendered,
every transcript and the whole text of every dashboard page at each caption is checked for private
values (`tools/showcase/privacy.py`): this machine's home folder, user name, host name and addresses, the
global git identity, any `/home/…` or `/Users/…` path other than the neutral home, any IPv4 address but
`127.0.0.1`/`0.0.0.0`, and any e-mail address outside `example.org`, `example.com`, `example.invalid`. A
page error, a console error, or a command that exits other than as the clip expects stops the recording:
nothing that failed by accident is ever recorded.

What `make.py` writes per clip: `showcase-NN-name.mp4` in the media folder (H.264, 1280×720, 15 fps, 10–40 s,
under 3 MB: a one-line caption card, then the clip with a caption per step, sped up to fit 38 s when
longer), and in `docs/showcase`: `NN-name.gif` (a
smaller preview for the README, under 2 MB), `NN-name.png` (the last frame) and, for terminal clips,
`logs/NN-name.txt` (the captured output, as shown). Then `contact-sheet.png` from every poster. It exits 1
when a file breaks a limit. `tests/tools/test_showcase.py` checks the renderer, the privacy rules, that
every `al` command shown is one the tool accepts, and the limits of the committed files; with
`AL_SHOWCASE_RECORD=1` it also records `07-adopt` end to end.

## The clips

### 01-start-work

The Layers view's Start panel: a prompt, the workflow `plan-build` and `claude-sonnet-5-5` for its heavy
tier; Start lands on the new run, whose graph fills in until plan and build have passed, and its cost
table shows the chosen model on both steps. Proves that work starts from the page with a per-run model.
Fixture: `kit/dashboard/tests/start_layer.py` (`build_site`: a git app and the `notes` layer). Stand-in:
the mock agent.

### 02-repos

The Repos view lists four repositories side by side (`layer`, `setup`, `setup`, `none`); Create on `fern`
shows what it will run, then a job runs the real `tools/al` (through a wrapper that reads one settings
file): it checks every step's tools first and stops at `init` because `AL_RACKET` names a racket that is
not installed, in al's own sentence. With the setting removed ("racket installed"), Retry from init runs
init, author, gen and check, and the job passes. Proves the Repos view, its jobs and Retry from with the
real tool. Fixture: `kit/dashboard/tests/workspace_fixtures.py`; the author's answer is the mock of
`tests/tools/test_dashboard_workspace.py`. Before recording, the same job is run once without video; if
it did not pass after the retry, nothing would be recorded. The job's output panel shows al's output as
printed, with the absolute paths of the temporary folder.

### 03-run-documents

A finished run's Documents tab lists its plan, its review report and a reviewer's verdict; the plan and
the report are rendered in the page, with a raw view and a download each. The Artefacts tab of a fixture
run shows its screenshots (one opened large). The Models panel switches the `heavy` tier to
`claude-fable-5-2`, shown as a local setting for runs started from now on. Fixture:
`kit/dashboard/tests/fixtures.py` (`demo-shop`) plus one finished run with a plan and a review report,
recorded the way `test_browser_documents.py` records one.

### 04-review-says-no

Run `a1000101`: the review step is rejected (⊗), its three rounds are each "not approved" with the
reviewers' reasons in the step panel, the run is handed over to a person, and the Runs list counts two
handed-over runs apart from passed ones. Fixture: `fixtures.py` plus `add_review_runs` of
`kit/dashboard/tests/test_review_rejected_model.py`.

### 05-delivery

A layer with `(delivery sprint)`, a stage and a promotion (`grep` shows the three lines). `al cycle notes.al
start` cuts the sprint branch; two `al run notes.al /change … --dry-run` runs (the mock agent writes
`cart.py` and `search.py`) each deliver a pull request into the sprint, and a person merges each (`git
merge` in a worktree of the sprint branch, as Ada Example). `al promote notes.al sprint main` checks the
sprint, writes the pack and waits for one human approval (exit 75); `al decide … approve --by "Ada
Example"` records it; `al promote` again bumps the version to 0.2.0, writes the changelog, merges into
main, tags `v0.2.0` and closes the cycle. Fixture: `SPRINT_LAYER` and `_layer` of
`tests/tools/test_al_promote.py`. Stand-in: the mock agent.

### 06-sandbox

`al sandbox notes.al status` shows the image's recipe hash, not built; `al sandbox notes.al doctor`
starts a throwaway container as a run does and finds `pytest` missing (exit 1, with the fix in the
table); `al run notes.al /change "Tidy the readme" --dry-run` stops at its preflight with the same table
before any agent works; `al doctor notes.al --agent mock` lists where al found racket, raco, uv and git.
Fixture: `_layer` of `tests/tools/test_sandbox_cli.py` with the test command `pytest -q`. Stand-in: podman
is the test suite's stand-in (`STUB` of that test file), answering the in-container checks with a missing
`pytest`; the status and doctor tables, the exit codes, the preflight stop and `al doctor` are al's own.
A recording against a real podman is a follow-up.

### 07-adopt

`al init larder`, then `al adopt larder.al --app larder --dry-run` lists the 17 pieces of agent material
it found (rule files of several tools, Claude Code commands, two workflow scripts) and what it would
write; `al adopt` writes them; the head of `adopted/REPORT.md` says what came from where; `git -C larder
status --short` prints nothing: the application is untouched. Fixture: `tests/tools/existing_setup.py`.

### 08-deploy-remote

`al deploy notes/agentic --to deploy-host.example --dry-run` prints the host check it would run, its
preflight table, the secrets by name only and every command it would run on the host, and changes
nothing. `al dashboard notes/agentic --remote testhost.invalid --port …` opens an ssh tunnel, loopback on
both ends, prints the local address and closes the tunnel on Ctrl-C (exit 130). Fixture: `DEPLOY_LAYER`
of `tests/tools/conftest.py` as an in-repo layer. Stand-ins: the tests' fake host (`tests/tools/deploy_host.py`,
a folder whose PATH holds stubs for systemctl, loginctl, podman, uv and the agent CLI), reached through
al's local transport for the deploy and through the tests' recording fake `ssh` for the tunnel. The fake
host's `df` is a stub as well (a fixed 50 GB free), so the preflight does not depend on the disk of the
machine that records. `docs/GUIDE.md` has no section on `al deploy` yet; writing one is a follow-up.

### 09-promotion-pack

The layer page lists the open promotion sprint → main; its card on the promotion run shows each delivered
change with its acceptance criteria and proof (test exit codes, a screenshot, a recording, the review);
a person (Ada Example) refuses one change with a comment and it is shown refused; the presentation pack
page shows what the promotion brings with its pictures and recordings. Found shipped but not shown
anywhere before. Fixture: `build_promotion_site` of `kit/dashboard/tests/promotion_fixtures.py` (three
changes delivered by real runs of the layer with the mock agent).

## Not shown

- `al models` on the command line: the Models panel (03) shows the same settings.
- `al run` single-command runs from the terminal: the README already describes them, and 05 and 06 use
  `al run` for workflows.
- `al notify-test`: it needs a real notifier.
- `al verify`, voice input, Forgejo and al used on itself: already in [`docs/demo`](../demo).

## Bugs found while recording

- **A tool setting's path is printed without `~`.** When `AL_RACKET` (or any `AL_<TOOL>` setting) names
  something that is not an executable file, al's sentence shows the setting's value as an absolute path,
  while its other sentences shorten the home folder to `~` (`tools/al_tools.py`, `missing_sentence`, the
  `found.problem` branch). In the dashboard's Repos jobs the sentence is shown as is, so the home folder
  appears on the page. Reproduction:

  ```sh
  AL_RACKET=$HOME/not-installed/racket tools/al doctor --for gen
  # racket is missing: AL_RACKET names /home/<you>/not-installed/racket (in the environment), which is not …
  ```

  Clip 02 therefore points `AL_RACKET` at `/opt/not-installed/racket/bin/racket`, outside the home folder.
- **`## Promote a stage` is not a heading on GitHub.** In `docs/GUIDE.md` the paragraph "Let `al scout`
  propose it" ends with a code fence (line 819) whose example and closing fence are missing, so the fence
  runs to line 837 and swallows `## Promote a stage` and `### Declare how a stage is promoted`; links to
  `docs/GUIDE.md#promote-a-stage` (README's own included) lead nowhere. Reproduction:

  ```sh
  uv run --with markdown-it-py python -c "from markdown_it import MarkdownIt; t=MarkdownIt('commonmark').parse(open('docs/GUIDE.md').read()); print([x.map for x in t if x.type=='fence' and x.map[0]==818])"
  # [[818, 837]]
  ```

  The guide is outside this change, so the README's rows for 05 and 09 link the section's first headings
  that render, `### Run it` and `### Decide`.
- Not a failure, noted: a Repos job's output panel shows al's own output, in which `al init`, `al author`
  and `al gen` print absolute layer paths; the step command lines above it are shortened (`work/fern`,
  `~/…`).
