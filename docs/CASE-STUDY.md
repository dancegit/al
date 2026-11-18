# Case study: building an agent harness, and finding out where agents fail

This is an account of one AI-assisted development workflow: what was built, how it was verified,
where the agents got things wrong, and what was actually measured. Every number below comes from
a file in this repository; the links say which.

## What it is

`al` generates an *agentic layer* for a codebase: the prompts, workflows, sandbox, triggers and
shipping rules that let coding agents implement, test and repair changes on it. A script and an
LLM describe the codebase (`LLM.txt`); an LLM writes a short definition (`agentic.rkt`); a
compiler rejects definitions that do not hang together; a generator produces the rest.
The [README](../README.md) explains the idea; [DESIGN.md](DESIGN.md) has the details.

It was built by one engineer directing AI coding agents (Claude Code, with Claude Opus 5.5 as
the agent inside the generated workflows). The engineer set the goals, the constraints and the
corrections; the agents wrote the code and the tests. That division is the point of the exercise:
the interesting work was not typing, it was deciding what would count as proof.

## What shipped

- **A small language and compiler** for describing how agents should work on a codebase, with
  rules that refuse unsafe combinations (for example, a workflow that ships without a human must
  run in a container). [`racket/al`](../racket/al)
- **A runtime** that executes workflows: one agent step per prompt, real commands as checks with
  a fix-and-retry loop, review on evidence, and a gate that decides whether a change may ship.
  [`kit/runtime`](../kit/runtime)
- **Isolation per run**: its own copy of the code, container, network namespace and database;
  the application's own checkout is never modified.
- **Handover instead of failure**: a run that cannot finish opens a pull request that says why
  and what it needs from a person.
- **Seven example codebases**: five public repositories in five languages, a small Notes web
  app, and a healthcare staffing app with two separate customer organisations.

## How it was verified

Verification was built in layers, because each layer catches what the one before it cannot.

| Layer | What it proves | Where |
|---|---|---|
| Compiler rules | the layer definition is coherent and not unsafe | 14 tests |
| Runtime tests | the machinery behaves, including attempts to break out of the container run against real podman | 241 tests |
| Integration tests | full runs on the five public repositories, each test on a private copy that must be left clean | 27 tests |
| `al verify` | the commands a layer declares really work in the environment it declares | cargo, go, npm, autotools and Python suites, all run for real |
| Live forge test | issues, pull requests, merges and signed webhooks against a real Forgejo | 13 assertions |
| Real agent runs | the whole loop with a real model, judged on recorded evidence | see below |
| Planted defect | review rejects bad work, not only approves good work | see below |

Real runs with Claude Opus 5.5, each started from a one-paragraph request:

| Codebase | Request | Outcome | Time | Cost |
|---|---|---|---|---|
| flask | add a specific test (three separate runs) | gate passed first attempt each time; the result waited on a branch for one manual fast-forward | 2–3 min | about $0.70 |
| Notes web app | dark theme | pull request with screenshots; a person merged | 111 s | $0.47 |
| Notes web app | search box | two reviewer agents approved on a recorded video; merged | 273 s | $1.31 |
| Staffing app | enforce 11 hours of rest between shifts | two reviewers approved; merged | 405 s | $2.34 |
| Staffing app | a change needing a database migration | agent stopped and handed over a draft pull request; a person added one file and merged | 192 s | $1.24 |
| Staffing app | a planted flawed change | both reviewers rejected it; an agent fixed it; approved; merged | 547 s | $2.96 |

Sources: [`docs/demo/logs`](demo/logs), [`docs/demo/webdemo/summary.json`](demo/webdemo/summary.json),
[`docs/demo/rotademo/summary.json`](demo/rotademo/summary.json).

## Where agents failed

This is the most useful part. None of these was found by asking an agent whether its work was
good. Each was found by a check that did not depend on the agent's own opinion.

**Agents writing the layer definition**

- All five definitions compiled on the first attempt, and one was still wrong. The jq layer's
  install command could not work inside the sandbox. The flask layer named a test-report path
  nobody had ever produced. *A definition that compiles is consistent, not true.* This is why
  `al verify` exists: it runs every declared command for real.

**Agents writing the harness itself**

- A runtime module passed all its tests and failed on the first real run. Its tests used a
  stand-in for the agent command-line tool; the real tool read a prompt beginning with `---` as
  an option. *Found by the first real run.*
- The shipping gate could be bypassed three ways that its author's tests did not cover: file
  names with non-ASCII characters under a protected directory, protected paths written as
  `./dir`, and judging the change before it was merged with the target. *Found by an
  independent review with fresh context.*
- The first sandbox mounted the application's git directory writable, so code in the container
  could have planted a hook that later ran on the host. The fix had its own race between
  checking and using. *Found by review, then by escape tests against real containers.*
- Five bugs were invisible to tests that used stand-ins and appeared only on real
  infrastructure: Nix refusing an untracked file, a missing network backend, Postgres unable to
  start with all privileges dropped, a duplicate mount, and one run able to read another's files.

**Agents doing the work inside the harness**

- Asked for a change that needed a database migration, the agent correctly refused to write to
  the protected `migrations/` directory and stated the exact SQL a person should add. The run
  then simply ended as "failed", with no pull request and the finished work stranded locally.
  The agent behaved well; the harness handed over badly. *Fixed, and re-run for real: a stopped
  run now opens a draft pull request that says why it stopped and what the agent asks of a
  human. A person added the one file and the checks passed.*
- Given a planted change that passed every test but missed an acceptance criterion, both
  reviewer agents rejected it, in both attempts. But in the first attempt, the second review
  round promoted to "blocker" something both reviewers had called minor in round one, so the
  change went to a human instead of merging. *Reviewers are not consistent between rounds. The
  gate treats disagreement as "ask a human", which is the safe direction.*
- After a fix, reviewers were shown a screenshot from before the fix. They noticed and said so.
  *Fixed in the runtime; covered by unit tests, not yet by a real run.*

**What has not been tested**

- Large or ambiguous tasks. Every real request was small and well specified.
- Whether a weaker or differently prompted reviewer would also catch the planted defect.
  Two runs is not a rate.
- The repair loop for layer definitions: since all five compiled first time, no LLM has yet had
  to fix a compiler error.

## What was measured, and what was not

Measured:

- **Cost and time per shipped change:** $0.47 to $2.96 and 2 to 9 minutes, depending on how
  much proof the change needed (a test, screenshots, or a video reviewed twice).
- **First-attempt outcomes:** every real feature run produced correct work first time (flask
  3 of 3; Notes 3 of 3 for each request; staffing app 1 of 1). The planted defect was rejected
  2 of 2.
- **Human involvement:** one action for a zero-touch change (writing the request), plus one
  fast-forward in the flask runs, where `main` was checked out in the application; two for a
  human-reviewed one (the request and the merge).
- **Defects caught per verification layer:** listed above. The layer that caught the most was
  running things for real rather than against stand-ins.

Not measured, and not claimed:

- A comparison with a team working without this harness. There is no baseline here.
- Reliability at scale. The samples are small.
- Production use. These are public repositories and demo applications, not a live service.

## What I would take from it

1. **Decide what counts as proof before the agent starts.** Acceptance criteria in the issue,
   checks that run real commands, and evidence a reviewer can inspect (logs, screenshots, video).
2. **Stopping conditions are a feature.** Protected paths, a size limit, allowed kinds of work,
   and "if anything is unclear, hand it to a person with the reason" did more for safety than
   any amount of prompting.
3. **Never let an agent mark its own homework.** The defects above were found by a compiler,
   by real commands, by an independent reviewer, and by a planted flaw. None was found by
   self-assessment.
4. **Test against the real thing.** Stand-ins are fast and they hide exactly the bugs that matter.
