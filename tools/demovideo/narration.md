# al in five minutes: the narration script

Every caption of the demo video and every scene length comes from this file. `tools/demovideo/make.py`
reads it (`rules.parse`), records one clip per scene and writes the timed copy beside the video.

Form: `## N. Title` is a part; `### scene-id | kind | SECONDS s` is a scene (`kind` is `card`, `terminal`
or `browser`); `- [+S.S] text` is a caption that starts S.S seconds into its scene and stays until the next
caption or the scene's end. A scene's first caption is at `+0.0`.

## 1. What al is

### 01-what | card | 20 s
- [+0.0] al generates the agentic layer around a codebase.
- [+5.0] A layer says how agents work here: commands, checks, review.
- [+10.0] A compiler checks the layer. A generator writes the rest.
- [+15.0] I built al. Here it is, end to end, in five minutes.

## 2. Writing a layer

### 02-layer | terminal | 45 s
- [+0.0] I wrote this layer for a small cart library.
- [+6.0] Its checks are real commands. The tools folder is protected.
- [+13.0] One workflow: classify, plan, build, check, review, ship.
- [+25.0] al gen compiles the layer and writes every file of it.
- [+36.0] al check finds nothing wrong.

## 3. A real run

### 03-start | terminal | 10 s
- [+0.0] One command starts the dashboard for this layer.
- [+5.0] It watches the layer's runs. Work starts from its page.

### 03-run | browser | 95 s
- [+0.0] A demo app. A scripted stand-in plays the model.
- [+6.0] I pick the workflow, type the change, and press Start.
- [+13.4] The graph is the layer's workflow, filling in live.
- [+19.3] Build: the stand-in edits the code and runs the tests.
- [+41.4] Checks are real commands. This is the log of the tests.
- [+46.0] Evidence: each claim of the plan has its proof.
- [+57.0] Two reviewers read the change. Both approve.
- [+68.0] Ship: a branch and a description, kept for a person.
- [+80.0] Passed. Nothing was merged without a person.

## 4. Proof and guardrails

### 04-proof | browser | 15 s
- [+0.0] The proof table: each claim of the plan and its command.
- [+7.5] Both claims are proven by checks that ran in this run.

### 04-own-plan | terminal | 8 s
- [+0.0] This video's own run: the claims its plan must prove.

### 04-protected | browser | 25 s
- [+0.0] Another run edits a protected file. al stops and asks.
- [+6.0] The Runs view lists it under Needs you.
- [+11.0] I approve that one edit from the command line.
- [+18.0] The run resumes where it stopped, and passes.

### 04-review-no | browser | 12 s
- [+0.0] A test fixture: reviewers refuse three times.
- [+6.0] The run is handed to a person. It does not count as passed.

## 5. Honest limits

### 05-limits | terminal | 28 s
- [+0.0] Tests that only skipped are marked skipped-only, not proven.
- [+7.0] A browser test failed in 5 of 10 runs. The layer says so.
- [+15.0] One failure showed only on the Python that CI runs.
- [+21.0] Runs left worktrees and images. al now cleans up.

### 05-not-yet | card | 14 s
- [+0.0] Of 45 real runs on al itself, 2 passed. 34 were handed over.
- [+5.5] Not shown yet: large or vague tasks, GitHub end to end.
- [+10.0] And this demo run used a stand-in, not a model.

## 6. What shipped

### 06-close | terminal | 13 s
- [+0.0] Three promotions reached main, each as a pull request.
- [+5.0] CI runs the fast suites on pushes and pull requests.
- [+9.0] The code, the guide and the tests: github.com/dancegit/al
