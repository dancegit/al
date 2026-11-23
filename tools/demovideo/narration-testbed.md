# al in five minutes, on Flask: the narration script

The second story of the demo video: the same seven parts as `narration.md`, on Flask, one of the pinned
public codebases of `docs/EXAMPLES.md`, in every scene. `tools/demovideo/make.py --story testbed` reads
this file (`rules.parse`), records one clip per scene with `scenes_testbed.py` and writes the timed copy
beside the video. The form is that of `narration.md`: `## N. Title`, `### scene-id | kind | SECONDS s`,
`- [+S.S] caption`, and under each caption the indented `  > ` line the voice says.

## 1. The problem

### 01-problem | card | 28 s
- [+0.0] Flask: a web framework many applications depend on.
  > Say I help maintain Flask, the Python web framework. A lot of applications sit on top of it, so every change matters.
- [+9.5] Locked dependencies, strict tests, sensitive session code.
  > The dependencies are locked, any warning fails the tests, and a slip in the session code becomes a security problem.
- [+18.5] What an agent must know: test, never touch, prove.
  > Now I want an agent to help. It must know how to test, what never to touch, and how to prove a change works.

## 2. What al is

### 02-layer | terminal | 34 s
- [+0.0] The real Flask layer: one definition file.
  > al answers those questions in one file. This is the real one for Flask.
- [+7.0] Checks: lint, type check, the test suite.
  > Its checks are Flask's own commands: the linters, the type checker and the test suite.
- [+14.0] Protected: the lock file, the CI setup, the session code.
  > Some paths are protected: the lock file, the CI setup, and the session module.
- [+21.0] Review, then a gate that requires every check.
  > Every change is reviewed, and a gate with hard rules decides what may ship.
- [+27.0] From it: prompts, workflows, checks, rules, a dashboard.
  > From that one file, al generates the whole workflow: prompts, workflows, checks, rules and a dashboard.

## 3. How to use it

### 03-use | terminal | 38 s
- [+0.0] al init: a layer folder beside the checkout.
  > How do I get one? al init makes a layer folder beside my checkout, with a first draft describing it.
- [+8.0] al adopt takes over existing agent rules. Flask has none.
  > If the repository already had agent rules, al adopt would take them over. Flask has none, and it says so.
- [+16.0] A model writes the definition. al gen writes the layer.
  > A model writes the definition, and al gen compiles it into the whole layer.
- [+22.0] al check: the layer is complete, the app untouched.
  > al check confirms the layer is complete and that Flask itself was not touched.
- [+29.0] Start a run: al run, or the dashboard's Start panel.
  > Then I start work, from the command line with al run, or from the Start panel of the dashboard.

## 4. A run, end to end

### 04-run | browser | 70 s
- [+0.0] Flask, a small change. A scripted stand-in plays the model.
  > Now a run, end to end, on Flask. No model is called: a script plays the agent, but it really edits code and really runs the tests.
- [+11.0] Pick the workflow, type the change, press Start.
  > I pick the workflow, type the change I want, and press Start.
- [+17.0] The graph is the layer's workflow, filling in live.
  > The graph is the layer's own workflow, filling in live as each step runs.
- [+23.0] Plan, then build: edits in a private worktree.
  > First a plan, then the build, in a private copy of the repository. It finds the debug flag helper, makes it treat off as off, adds a test and runs it.
- [+38.0] Checks are real commands: pytest on Flask's own tests.
  > Then the checks. Not the agent's opinion: the real pytest command on Flask's own tests, and this is its log.
- [+46.0] Two reviewers read the change. Both approve.
  > Two reviewers read the change against the plan and the evidence. Here both approve.
- [+54.0] Ship: a branch is pushed and a pull request opened.
  > Ship does not mean merge. It pushes a branch and opens a pull request, and a person decides.
- [+62.0] Passed. Nothing was merged.
  > The run passed, and nothing was merged. That decision stays with a person.

### 04-pr | browser | 18 s
- [+0.0] The pull request, on a local Forgejo started for the demo.
  > And here is that pull request, on a throwaway Forgejo server running on this machine, not the project's real home.
- [+9.0] Its description: the plan, the checks, both reviews.
  > Its description carries the plan, the checks that ran and both reviews, for whoever merges it.

## 5. Guardrails and proof

### 05-proof | browser | 14 s
- [+0.0] The proof table: each claim of the plan and its command.
  > Before the build, the plan must say what it will prove, and which command proves it.
- [+8.0] Both claims proven by checks that ran in this run.
  > Afterwards each claim is marked from what actually ran, not from what was said.

### 05-protected | browser | 34 s
- [+0.0] A second scripted run edits sessions.py, a protected path.
  > Now a guardrail. A second scripted run edits the session module, a protected path. al stops and asks a person.
- [+8.0] Needs you: the run waits for an approval.
  > It shows up in the dashboard under Needs you, waiting for an approval.
- [+15.0] Approve & resume: what is approved, and where it resumes.
  > I press Approve and resume. The dialog says exactly what I approve, under my name, and where the run picks up.
- [+25.0] The run continues where it stopped, and passes.
  > The run continues from where it stopped, through the same checks, and passes. Its pull request is on the forge too.

### 05-review-no | browser | 14 s
- [+0.0] A third scripted run: the reviewers refuse.
  > And reviewers can say no. Here the scripted reviewers refuse, each with reasons.
- [+7.0] Not passed. It is left to a person.
  > The run does not pass. al stops there and leaves the decision to a person.

## 6. Where it fits

### 06-fit | card | 24 s
- [+0.0] Flask is one of five public codebases with a layer.
  > Flask is one of five public examples, next to ripgrep, gin, express and jq, each in its own language.
- [+8.0] Fits: real tests, real commands, a person who decides.
  > It fits where a repository has real tests and commands, and a person who wants proof before anything ships.
- [+16.0] Not yet shown: large or vague tasks, GitHub end to end.
  > What this does not show yet: large or vague tasks, GitHub end to end, and a real team's own repository.

## 7. Try it

### 07-close | card | 12 s
- [+0.0] github.com/dancegit/al: the code, the guide, the examples.
  > That is al: one short definition, a whole workflow, and proof before anything ships. The code, the guide and the examples are public. Thanks for watching.
