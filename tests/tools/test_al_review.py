"""The ship-without-review limit as `tools/al` shows it: `al review` (show, --set, --reset, bad use, a
layer whose gate does not read the limit), the `limit:` lines and the readable size stop of `al runs`,
the closing lines of `al explain`, the dashboard's mirror of the local setting held equal to the
runtime's, and `al check` on a tracked setting. No network, no LLM (the mock agent), no podman."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import KIT, al

sys.path.insert(0, str(KIT / "dashboard"))
import dashboard  # noqa: E402

# true once a person applied the hook to the protected gate (kit/runtime/adw_modules/review.py)
CAPABLE = "READS_LOCAL_LIMIT = True" in (KIT / "runtime" / "adw_modules" / "review.py").read_text()
NEEDS_HOOK = "REQUIRES HUMAN: review.py does not read the local limit yet (READS_LOCAL_LIMIT)"
RUN = "5122e001"
AT = "2026-11-23T10:00:00Z"

# two auto review options: zero-touch writes its limit, nightly leaves it to al's default; a human option
REVIEWED = """#lang al
(agentic-layer #:name "notes" #:description "Agentic layer of a tiny test app"
  (codebase #:root "../notes" #:languages (python) #:description "A tiny app: app.py and test.sh."
    (cmd test "./test.sh" #:timeout 60 #:description "the tests pass")
    (paths #:source ("app.py") #:tests ("test.sh") #:protected ("test.sh")))
  (layer sibling)
  (command plan #:template plan-chore #:model-set heavy #:output path #:validation (test))
  (command commit #:template commit)
  (command review #:template review #:output json)
  (hop implement #:template run-plan #:takes plan)
  (review human)
  (review auto #:id zero-touch #:ship pr #:require-checks (test) #:max-diff-lines 2000)
  (review auto #:id nightly #:ship pr #:require-checks (test))
  (adw change #:description "Plan, build, test; a pull request when every rule holds" #:review zero-touch
    (steps (run plan #:command plan #:args ("$prompt")) (run build #:command implement #:args ("$plan"))
           (check test #:cmd test) (run commit #:command commit) (review) (ship))))
"""


@pytest.fixture
def layer(app: Path) -> Path:
    path = app.parent / "reviewed.al"
    path.mkdir()
    (path / "agentic.rkt").write_text(REVIEWED)
    al("gen", path)
    return path


def setting(layer: Path) -> Path:
    return layer / "agents" / "settings" / "review.json"


def runtime(layer: Path, code: str, ok: bool = True) -> subprocess.CompletedProcess:
    """Run `code` with the layer's own runtime importable (adw_modules), in a fresh interpreter."""
    cp = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(layer / 'adws')!r})\n{code}"],
                        capture_output=True, text=True, timeout=120)
    if ok:
        assert cp.returncode == 0, cp.stderr
    return cp


def default_limit(layer: Path) -> int:
    """What the compiler gave the option that writes no #:max-diff-lines (the vocabulary's default)."""
    ir = json.loads((layer / "agentic-layer.json").read_text())
    return next(r for r in ir["reviews"] if r["id"] == "nightly")["maxDiffLines"]


def with_capability(layer: Path, capable: bool) -> None:
    """Make the layer's gate say (or not say) that it reads the local limit: the one line `al review` and
    the dashboard look for. The gate's code is not changed."""
    gate = layer / "adws" / "adw_modules" / "review.py"
    text = re.sub(r"(?m)^READS_LOCAL_LIMIT\s*=.*\n", "", gate.read_text())
    gate.write_text(text + ("READS_LOCAL_LIMIT = True\n" if capable else ""))


def test_al_review_shows_each_auto_option_and_its_source(layer) -> None:
    out = al("review", layer).stdout.splitlines()
    assert out == [
        "zero-touch: max-diff-lines 2000 (layer definition)",
        f"nightly: max-diff-lines {default_limit(layer)} (al's default)",
        f"settings: {setting(layer)} (local to this machine, never committed); applies to every gate from now on"]
    data = json.loads(al("review", layer, "--json").stdout)
    assert sorted(data) == ["error", "gate_reads", "options", "settings_file"]
    assert (data["settings_file"], data["error"], data["gate_reads"]) == (str(setting(layer)), "", CAPABLE)
    assert [o["id"] for o in data["options"]] == ["zero-touch", "nightly"]   # the human option has no limit
    limit = data["options"][0]["maxDiffLines"]
    assert (limit["value"], limit["declared"], limit["source"], limit["local"]) == (2000, 2000, "layer definition", None)
    assert data["options"][1]["maxDiffLines"]["source"] == "al's default"
    assert data["options"][0]["ship"] == {"value": "pr", "source": "layer definition"}
    assert not setting(layer).exists()


@pytest.mark.skipif(not CAPABLE, reason=NEEDS_HOOK)
def test_al_review_sets_and_resets(layer) -> None:
    out = al("review", layer, "--set", "zero-touch=3000", "--who", "Ada").stdout
    assert "zero-touch: max-diff-lines 3000 (local override; declared 2000)" in out.splitlines()
    stored = json.loads(setting(layer).read_text())
    assert stored["version"] == 1 and stored["maxDiffLines"] == {"zero-touch": 3000}
    assert (stored["set"]["zero-touch"]["who"], stored["set"]["zero-touch"]["via"]) == ("Ada", "cli")
    assert stored["set"]["zero-touch"]["at"].endswith("Z")
    al("review", layer, "--set", "zero-touch=2500", "--set", "nightly=900", "--who", "Ada")
    assert json.loads(setting(layer).read_text())["maxDiffLines"] == {"zero-touch": 2500, "nightly": 900}
    out = al("review", layer, "--reset", "nightly").stdout
    assert f"nightly: max-diff-lines {default_limit(layer)} (al's default)" in out.splitlines()
    assert json.loads(setting(layer).read_text())["maxDiffLines"] == {"zero-touch": 2500}
    out = al("review", layer, "--reset", "zero-touch").stdout
    assert "zero-touch: max-diff-lines 2000 (layer definition)" in out.splitlines()
    assert not setting(layer).exists()

    setting(layer).parent.mkdir(parents=True, exist_ok=True)
    setting(layer).write_text("{not json")
    out = al("review", layer).stdout
    assert "note: the local setting is ignored: agents/settings/review.json: not JSON" in out
    assert "zero-touch: max-diff-lines 2000 (layer definition)" in out.splitlines()
    al("review", layer, "--reset")   # a bare --reset removes a broken file
    assert not setting(layer).exists() and not list(setting(layer).parent.glob(".review.*"))


def test_al_review_refuses_bad_use(layer) -> None:
    with_capability(layer, True)   # so each refusal below is about the value, not about the gate
    for args, words in ((["--set", "zero-touch=0"], "whole number from 1 to 50000"),
                        (["--set", "zero-touch=50001"], "whole number from 1 to 50000"),
                        (["--set", "zero-touch=abc"], "--set takes ID=N"),
                        (["--set", "zero-touch"], "--set takes ID=N"),
                        (["--set", "nope=10"], "no auto review option named 'nope'; this layer has: zero-touch, nightly"),
                        (["--set", "human=10"], "no auto review option named 'human'"),
                        (["--set", "nightly=900", "--set", "zero-touch=0"], "whole number from 1 to 50000"),
                        (["--set", "zero-touch=3000", "--who", "x" * 81], "who: a name of 1 to 80 characters"),
                        (["--reset", "nope"], "no auto review option named 'nope'")):
        cp = al("review", layer, *args, ok=False)
        assert cp.returncode == 2, (args, cp.stdout, cp.stderr)
        assert cp.stderr.startswith("al review: ") and words in cp.stderr, (args, cp.stderr)
        assert cp.stdout == "" and not setting(layer).exists(), args
    assert al("review", layer, "--set", "zero-touch=3000", "--reset", ok=False).returncode == 2   # one or the other

    with_capability(layer, False)   # a layer generated before the gate read the limit
    cp = al("review", layer, "--set", "zero-touch=3000", "--who", "Ada", ok=False)
    assert cp.returncode == 2 and not setting(layer).exists()
    assert cp.stderr.strip() == (f"al review: refused: this layer's gate does not read the local limit: "
                                 f"run `al gen {layer}`")
    # an override that is there anyway is shown as not read, and --reset still removes it
    dashboard.write_review_setting(layer, "zero-touch", 3000, who="Ada", via="dashboard", at=AT)
    out = al("review", layer).stdout
    assert ("zero-touch: max-diff-lines 2000 (layer definition; the local override 3000 is not read by this "
            "layer's gate: run al gen)") in out.splitlines()
    al("review", layer, "--reset")
    assert not setting(layer).exists()

    (layer / "adws" / "adw_modules" / "reviewpolicy.py").unlink()   # generated before the setting existed
    cp = al("review", layer, ok=False)
    assert cp.returncode != 0 and "was generated before the local review limit: run `al gen" in cp.stderr


def stopped_run(layer: Path) -> None:
    """A run of `change` the gate handed to a person: its diff of 2500 lines is over the limit of 2000."""
    reason = "diff-too-large:2500>2000"
    run = layer / "agents" / RUN
    run.mkdir(parents=True)
    steps = [{"name": name, "kind": kind, "status": "passed", "attempts": 1, "started": "2026-11-23T09:00:00+00:00",
              "ended": "2026-11-23T09:01:00+00:00", "detail": ""}
             for name, kind in (("plan", "run"), ("build", "run"), ("test", "check"), ("commit", "run"),
                                ("review", "review"), ("ship", "ship"))]
    (run / "state.json").write_text(json.dumps({
        "adw_id": RUN, "adw": "change", "prompt": "Rework how notes are stored", "source": "cli", "klass": "chore",
        "dry_run": False, "branch": f"chore/{RUN}-rework", "steps": steps, "checks": {"test": {"exit": 0}},
        "review": {"verdict": {"success": True, "issues": []}}, "status": "handed-over",
        "started": "2026-11-23T09:00:00+00:00", "ended": "2026-11-23T09:06:00+00:00",
        "ship": {"action": "pr", "shipped": False, "reasons": [reason], "handed_over": [reason],
                 "gate": {"action": "pr", "ship": False, "reasons": [reason], "diff_lines": 2500,
                          "changed_files": ["app.py", "notes.py", "store.py"]}}}, indent=2))
    (run / "run.log").write_text(f"[adw {RUN}] change start: class=chore dry_run=False\n")


def test_al_runs_and_explain_show_the_effective_limit(layer, tmp_path) -> None:
    stopped_run(layer)
    with_capability(layer, True)
    dashboard.write_review_setting(layer, "zero-touch", 3000, who="Ada", via="dashboard", at=AT)
    limit = "zero-touch: max-diff-lines 3000 (local override; declared 2000)"
    other = f"nightly: max-diff-lines {default_limit(layer)} (al's default)"

    out = al("runs", layer).stdout
    row = next(line for line in out.splitlines() if line.startswith(RUN))
    assert "diff 2500 lines > limit 2000 (review option zero-touch), 3 files changed" in row
    assert "diff-too-large" not in out
    assert out.splitlines()[-2:] == [f"limit: {limit}", f"limit: {other}"]
    rows = json.loads(al("runs", layer, "--json").stdout)   # the JSON keeps its shape: a list of rows
    assert [r["id"] for r in rows] == [RUN]
    assert rows[0]["reason"].startswith("diff 2500 lines > limit 2000 (review option zero-touch), 3 files changed")

    out = al("explain", layer).stdout
    assert out.rstrip("\n").splitlines()[-4:] == [
        "", "Ship without review, on this machine", f"    - {limit}", f"    - {other}"]
    written = tmp_path / "EXPLAIN.md"
    out = al("explain", layer, "--md", written).stdout
    assert "Ship without review, on this machine" not in out
    assert "3000" not in written.read_text() and "local override" not in written.read_text()   # a committed file

    with_capability(layer, False)   # the page never claims a limit the gate does not use
    assert ("limit: zero-touch: max-diff-lines 2000 (layer definition; the local override 3000 is not read by "
            "this layer's gate: run al gen)") in al("runs", layer).stdout.splitlines()


def test_review_setting_mirror_equal(layer) -> None:
    path = setting(layer)
    where = repr(str(layer))
    # the same writes, one after the other, by the dashboard and by the layer's runtime: the same bytes
    writes = (("zero-touch", 3000, "Ada", "cli"), ("nightly", 1, "Grace Example", "dashboard"),
              ("zero-touch", 50000, "Ada", "dashboard"), ("nightly", None, "", "cli"))
    for count in range(1, len(writes) + 1):
        path.unlink(missing_ok=True)
        for option, limit, who, via in writes[:count]:
            dashboard.write_review_setting(layer, option, limit, who=who, via=via, at=AT)
        written = path.read_bytes()
        read = json.loads(runtime(layer, "import json\nfrom adw_modules import reviewpolicy\n"
                                         f"print(json.dumps(reviewpolicy.read_setting({where})))").stdout)
        assert read == dashboard.read_review_setting(layer)   # each reads the other's file
        path.unlink()
        runtime(layer, "from adw_modules import reviewpolicy\n" + "".join(
            f"reviewpolicy.write_setting({where}, {option!r}, {limit!r}, who={who!r}, via={via!r}, at={AT!r})\n"
            for option, limit, who, via in writes[:count]))
        assert path.read_bytes() == written, "the dashboard writes the file byte for byte as the runtime does"
        assert dashboard.read_review_setting(layer) == read
    assert dashboard.read_review_setting(layer) == {
        "maxDiffLines": {"zero-touch": 50000}, "set": {"zero-touch": {"who": "Ada", "via": "dashboard", "at": AT}}}
    runtime(layer, f"from adw_modules import reviewpolicy\nreviewpolicy.write_setting({where}, None, None)")
    assert not path.exists()

    for bad in (0, 50001):
        with pytest.raises(ValueError) as mirrored:
            dashboard.write_review_setting(layer, "zero-touch", bad, who="Ada")
        cp = runtime(layer, "from adw_modules import reviewpolicy\n"
                            f"try:\n    reviewpolicy.write_setting({where}, 'zero-touch', {bad}, who='Ada')\n"
                            "except ValueError as exc:\n    print(exc)")
        assert cp.stdout.strip() == str(mirrored.value) and "whole number from 1 to 50000" in cp.stdout
        assert not path.exists()
    constants = runtime(layer, "from adw_modules import reviewpolicy as r\n"
                               "print(r.MIN_LINES, r.MAX_LINES, r.WHO_MAX, r.SETTING_FILE, r.GATE_CAPABILITY)").stdout.split()
    assert constants == [str(dashboard.REVIEW_LIMIT_MIN), str(dashboard.REVIEW_LIMIT_MAX), str(dashboard.REVIEW_WHO_MAX),
                         dashboard.REVIEW_FILE, "READS_LOCAL_LIMIT"]
    # the layer's gate and the dashboard agree on whether the limit is read
    reads = runtime(layer, "from adw_modules import reviewpolicy\nprint(reviewpolicy.gate_reads_limit())").stdout.strip()
    assert reads == str(dashboard.gate_reads_limit(layer)) == str(CAPABLE)


def test_tracked_review_setting_is_a_problem(in_repo, app) -> None:
    dashboard.write_review_setting(in_repo, "zero-touch", 3000, who="Ada", via="dashboard", at=AT)
    subprocess.run(["git", "-C", str(app), "add", "-f", "agentic/agents/settings/review.json"], check=True)
    cp = al("check", in_repo, ok=False)
    assert cp.returncode != 0
    assert ("agents/settings/review.json is a local setting and must not be committed: "
            "git rm --cached agentic/agents/settings/review.json") in cp.stdout + cp.stderr
