# al in five minutes: the narration script

Every caption of the demo video, every spoken line and every scene length comes from this file.
`tools/demovideo/make.py` reads it (`rules.parse`), records one clip per scene and writes the timed copy
beside the video.

Form: `## N. Title` is a part; `### scene-id | kind | SECONDS s` is a scene (`kind` is `card`, `terminal`
or `browser`); `- [+S.S] text` is a caption that starts S.S seconds into its scene and stays until the next
caption or the scene's end. A scene's first caption is at `+0.0`. The indented `  > text` lines under a
caption are what the voice says while that caption is shown (several such lines are joined); every caption
has one. Captions stay short labels, so a viewer with the sound off still follows; a spoken line holds at
most 2.6 words a second of its caption's time on screen.

## 1. The problem

### 01-problem | card | 28 s
- [+0.0] Rota: shift scheduling for hospital wards.
  > Say you run Rota, a scheduling app for hospital wards. A ward manager fills open shifts with nurses and care assistants.
- [+9.5] Rules matter. Two organisations' data must stay apart.
  > Mistakes here are expensive. A nurse with an expired registration must not work, and one hospital must never see another's staff.
- [+18.5] What an agent must know: test, never touch, prove.
  > Now hand it to an agent. It must know how to test, what never to touch, and how to prove a change works.

## 2. What al is

### 02-layer | terminal | 34 s
- [+0.0] The real Rota layer: one short definition.
  > al answers those questions in one short file. This is the real one for Rota.
- [+6.0] Three checks: the tests, data isolation, a browser run.
  > Its checks are the app's own commands: the test suite, a data isolation test, and a recorded browser run.
- [+14.0] Protected: migrations, sign-in, the isolation test.
  > Some paths are protected: the migrations, the sign-in code, and the isolation test itself.
- [+21.0] Two reviewer agents, then a gate with hard rules.
  > Two reviewer agents must approve, then a gate checks hard rules.
- [+27.0] From it: prompts, workflows, checks, rules, a dashboard.
  > From that one file, al generates the whole workflow: prompts, workflows, checks, rules and a dashboard.

## 3. How to use it

### 03-use | terminal | 36 s
- [+0.0] al init: a layer folder beside the app, a draft LLM.txt.
  > How do you get one? al init makes a layer folder beside your repository, with a first draft describing it.
- [+8.0] An LLM writes the definition. al adopt takes over a setup.
  > A model writes the definition from it. If you already have agent rules or scripts, al adopt takes them over.
- [+16.0] al gen writes the whole layer.
  > al gen compiles it and writes every file of the layer.
- [+22.0] al check: the layer is complete, the app untouched.
  > al check confirms the layer is complete and the app was not touched.
- [+28.0] Start a run: al run, or the dashboard's Start panel.
  > Then you start work, from the command line with al run, or from the Start panel of the dashboard.

## 4. A run, end to end

### 04-run | browser | 68 s
- [+0.0] A small demo app. A scripted stand-in plays the model.
  > Now a run, end to end, on a small demo app. No model is called: a script plays the agent, really editing code and running tests.
- [+11.0] Pick the workflow, type the change, press Start.
  > I pick the workflow, type the change I want, and press Start.
- [+17.0] The graph is the layer's workflow, filling in live.
  > The graph is the layer's workflow, filling in live.
- [+23.0] Plan, then build: edits in a private worktree.
  > First a plan, then the build, in a private copy of the repository. Every tool call is on the record: it searches, edits the cart, writes a test and runs it.
- [+38.0] Checks are real commands. This is the log of the tests.
  > Then the checks: not the agent's opinion, the real command, and this is its log.
- [+45.0] Two reviewers read the change. Both approve.
  > Two reviewers read the change against the plan. Here both approve.
- [+53.0] Ship: a branch and its pull request text, for a person.
  > Ship does not mean merge here. It means a branch and a pull request description, waiting for a person.
- [+61.0] Passed. Nothing was merged without a person.
  > Passed, and nothing was merged without a person.

### 04-rota | card | 18 s
- [+0.0] Rota, with a real model: an 11-hour rest rule, on video.
  > On Rota itself, with a real model, a run added an eleven-hour rest rule, proven by a recorded browser run.
- [+9.0] Two reviewers approved on the evidence. The gate merged it.
  > Two reviewer agents checked it against the evidence, the gate's rules held, and it merged.

## 5. Guardrails and proof

### 05-proof | browser | 13 s
- [+0.0] The proof table: each claim of the plan and its command.
  > Before the build, the plan must say what it will prove, and which command proves it.
- [+8.0] Both claims proven by checks that ran in this run.
  > Afterwards each claim is marked from what actually ran.

### 05-protected | browser | 32 s
- [+0.0] A second run edits a protected file. al stops and asks.
  > Now a guardrail. A second run edits the lint tool, a protected file. al stops and asks a person.
- [+8.0] Needs you: the run waits for an approval.
  > It shows up under Needs you, waiting for an approval.
- [+15.0] Approve & resume: what is approved, and where it resumes.
  > I press Approve and resume. The dialog says exactly what I approve, under my name, and where the run picks up.
- [+24.0] The run continues where it stopped, and passes.
  > The run continues from where it stopped, through the same checks, and passes.

### 05-handoff | card | 14 s
- [+0.0] Rota: a schema change. The agent hands it to a person.
  > On Rota, a request needed a database migration, which is protected. The agent did the rest and stopped.
- [+7.0] A draft pull request: why it stopped, what to add.
  > Its draft pull request says why, and the exact file a person should add.

### 05-review-no | browser | 14 s
- [+0.0] A test fixture: reviewers refuse three times.
  > And reviewers can say no. This test fixture shows three rounds, three refusals, each with reasons.
- [+7.0] Handed to a person. It does not count as passed.
  > After that, al stops trying and hands the run to a person.

## 6. Where it fits

### 06-fit | card | 24 s
- [+0.0] Layers for five public codebases in five languages.
  > The examples cover five public codebases: flask, ripgrep, gin, express and jq, each in its own language.
- [+8.0] Fits: real tests, real commands, a person who decides.
  > It fits where a repository has real tests and commands, and a person who wants proof before anything ships.
- [+16.0] Not yet shown: large or vague tasks, GitHub end to end.
  > What it does not show yet: large or vague tasks, GitHub end to end, and a real team's repository.

## 7. Try it

### 07-close | card | 12 s
- [+0.0] github.com/dancegit/al: the code, the guide, the examples.
  > That is al: one short definition, a whole workflow, and proof before anything ships. The code, the guide and the examples are public. Thanks for watching.
