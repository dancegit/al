"""The five-minute demo video (tools/demovideo): the privacy rules find and blank what must never be on
a frame, the narration script has its seven parts with short, gapless captions and no run statistics,
the scripted Rota layer and the real Rota layer compile, every `al` command the video shows is one the
tool accepts, the protected run is one Approve & resume clears, the Rota pictures shown are committed, and
no video is kept in the tree. The scripted stand-in's change (the 11-hour rest rule) and the refused-review
fixture are Rota too: no scene shows another app. When
a recorded video is in the run's proof folder (.al-proof/demo, or AL_DEMO_VIDEO_DIR), it is verified:
its shape, its contact sheet, its timed script, OCR of a frame every five seconds and the caption band.
Two slow tests record a scene and check that the verifier refuses a bad video.

    uv run --with pytest --with pytest-xdist --with pyyaml --with jsonschema pytest tests/tools/test_demo_video.py -q -rA
    AL_SHOWCASE_RECORD=1 uv run --with pytest --with pyyaml --with jsonschema --with pillow pytest tests/tools/test_demo_video.py -q -m slow
"""
from __future__ import annotations

import dataclasses
import functools
import http.server
import importlib.util
import inspect
import io
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import wave
from datetime import date, datetime
from pathlib import Path

import pytest

from conftest import AL, ROOT, SANDBOX_GIT, al

SHOWCASE = ROOT / "tools" / "showcase"
DEMO = ROOT / "tools" / "demovideo"
sys.path.insert(0, str(SHOWCASE))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(DEMO))

import rules  # noqa: E402
import scenes  # noqa: E402  (standard library only at import: Pillow and Playwright are loaded by the recorders)
import forgejo  # noqa: E402
import scenes_testbed  # noqa: E402
import stories  # noqa: E402
import tts  # noqa: E402

assert Path(rules.__file__).parent == DEMO, rules.__file__
NARRATION = DEMO / "narration.md"
MEDIA_SUFFIXES = (".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".wav", ".mp3", ".m4a", ".ogg", ".gif", ".png", ".jpg", ".jpeg")
VALUES = {"hosts": ["box-17.corp"], "repos": ["larder-internal"]}


# ------------------------------------------------------------------------------ the rules

def test_privacy_rules_find_each_kind():
    for text, kind, match in (("mail someone@corp.example.net today", "mail", "someone@corp.example.net"),
                              ("by ada@example.org", "mail", "ada@example.org"),
                              ("see /home/someone/x", "home", "/home/"),
                              ("in /Users/someone", "home", "/Users/"),
                              ("ssh BOX-17.corp now", "host", "BOX-17.corp"),
                              ("from 10.1.2.3 port 22", "ipv4", "10.1.2.3"),
                              ("http://127.0.0.1:8765/", "ipv4", "127.0.0.1"),
                              ("a token here", "token", "token"),
                              ("Tokens out", "token", "Token"),
                              ("cloned Larder-Internal beside it", "repo", "Larder-Internal")):
        hits = rules.find_private(text, VALUES)
        assert [(h.kind, h.match) for h in hits] == [(kind, match)], text
        assert match in hits[0].excerpt
    # as OCR gives it: neighbouring words joined, no space or boundary beside a match
    joined = rules.find_private("ada@example.org/home/ada/work10.1.2.3 Tokens 1234", VALUES)
    assert [(h.kind, h.match) for h in joined] == [("mail", "ada@example.org"), ("home", "/home/"), ("ipv4", "10.1.2.3"),
                                                   ("token", "Token")]
    assert set(rules.PATTERNS) | {"host", "repo"} == set(rules.KINDS_OF_HIT) and len(rules.KINDS_OF_HIT) == 6
    assert rules.find_private("nothing of this machine", {"hosts": [], "repos": []}) == []
    values = rules.machine_values(ROOT)
    assert set(values) == {"hosts", "repos"} and "al" not in values["repos"] and "localhost" not in values["hosts"]
    assert all(len(v) >= 3 for found in values.values() for v in found)


def test_privacy_rules_accept_what_the_demo_shows():
    for text in ("github.com/dancegit/al", "uv 0.13.8", "v0.2.0", "~/work/rota.al", "sped up ×3.5", "run 0123abcd",
                 "Python 3.12.3", "al dashboard rota.al --port 18765", "48 passed", "256.1.2.3", "1.2.3.4.5"):
        assert rules.find_private(text, VALUES) == [], text


def test_blank_replaces_only_the_match():
    text = "al dashboard on http://127.0.0.1:18765/\n  1 234 tokens out, by ada@example.org on box-17.corp\n"
    blanked, hits = rules.blank(text, VALUES)
    assert len(blanked) == len(text) and blanked.count("\n") == text.count("\n")
    assert blanked == ("al dashboard on http://█████████:18765/\n"
                       "  1 234 █████s out, by ███████████████ on ███████████\n")
    assert [h.kind for h in hits] == ["ipv4", "token", "mail", "host"]
    assert [(a, b) for a, b in zip(text, blanked) if a != b and b != rules.BLOCK] == []
    assert rules.find_private(blanked, VALUES) == []
    assert rules.blank("nothing private", VALUES) == ("nothing private", [])
    # the pattern is the marker alone; the recorder blanks the path behind it too, so no folder name is left
    assert rules.blank("see /home/ada/work now")[0] == "see ██████ada/work now"
    assert rules.blank("see /home/ada/work now", whole_paths=True)[0] == "see ██████████████ now"


def test_fit_speeds_up_or_holds():
    assert rules.fit(190, 95) == (2.0, 0.0)
    assert rules.fit(40, 45) == (1.0, 5.0)
    assert rules.fit(45, 45) == (1.0, 0.0)
    assert (rules.SIZE, rules.FPS, rules.MIN_SECONDS, rules.MAX_SECONDS) == ((1280, 720), 15, 270, 300)
    assert (rules.SHEET_EVERY, rules.OCR_EVERY, rules.STRIP_H) == (10, 5, 64)


# ------------------------------------------------------------------------------ the narration script

def _scene_ids_in_scenes_py() -> list[str]:
    text = (DEMO / "scenes.py").read_text(encoding="utf-8")
    block = text[text.index("\nSCENES = {"):]
    return re.findall(r'^\s+"([0-9a-z-]+)": scene_', block[:block.index("\n}")], re.M)


TITLES = ("The problem", "What al is", "How to use it", "A run, end to end", "Guardrails and proof", "Where it fits", "Try it")
ARC = ["01-problem", "02-layer", "03-use", "04-run", "04-rota", "05-proof", "05-protected", "05-handoff", "05-review-no",
       "06-fit", "07-close"]


def test_narration_has_seven_parts_in_order_and_the_right_length():
    script = rules.parse(NARRATION.read_text(encoding="utf-8"))
    assert rules.TITLES == TITLES
    assert [(p.number, p.title) for p in script.parts] == list(enumerate(rules.TITLES, 1))
    ids = [s.id for s in script.scenes]
    known = _scene_ids_in_scenes_py()
    assert ids and len(set(ids)) == len(ids) and set(ids) <= set(known), sorted(set(ids) - set(known))
    assert ids == [i for i in known if i in ids], "the scenes are recorded in script order"
    assert all(s.kind in rules.KINDS and all(part.scenes for part in script.parts) for s in script.scenes)
    assert 285 <= script.seconds <= 300 and rules.MIN_SECONDS <= script.seconds <= rules.MAX_SECONDS, script.seconds
    captions = {s.id: [c.text for c in s.captions] for s in script.scenes}
    assert captions["04-run"][0] == "Rota again. A scripted stand-in plays the model."
    assert captions["05-review-no"][0] == "A Rota test fixture: reviewers refuse three times."
    assert "real model" in captions["04-rota"][0]


def test_narration_captions_are_short_gapless_and_private():
    text = NARRATION.read_text(encoding="utf-8")
    script = rules.parse(text)
    assert rules.validate(script) == []
    for scene in script.scenes:
        assert scene.captions[0].offset == 0.0, scene.id
        for caption in scene.captions:
            assert len(caption.text) <= 64, caption.text
    first = script.scenes[0].captions
    line = f"- [+{first[1].offset:.1f}] {first[1].text}"
    assert text.count(line) == 1
    for changed, word in ((text.replace(line, f"- [+{first[1].offset:.1f}] " + "x" * 70), "70 characters"),
                          (text.replace(line, f"- [+1.0] {first[1].text}"), "under 2.5 s"),
                          (text.replace(line, f"- [+{first[1].offset:.1f}] see /home/x"), "private match (home)")):
        problems = rules.validate(rules.parse(changed))
        assert len(problems) == 1 and word in problems[0], problems
    # the timed copy `make.py assemble` writes parses to the same script, with its times
    seconds = script.scenes[0].seconds
    heading = f"### {script.scenes[0].id} | {script.scenes[0].kind} | {seconds:g} s"
    assert text.count(heading) == 1
    timed = text.replace(heading, f"{heading} | 0:00.0–{rules.clock(seconds)}").replace(
        "- [+0.0] " + first[0].text, "- [+0.0 | 0:00.0–0:05.0] " + first[0].text, 1) + "\n## Scenes sped up\n\n- none\n"
    again = rules.parse(timed)
    assert rules.validate(again) == [] and (again.scenes[0].start, again.scenes[0].end) == (0.0, seconds)
    assert (again.scenes[0].captions[0].start, again.scenes[0].captions[0].end) == (0.0, 5.0)
    assert rules.clock(125.04) == "2:05.0" and rules.unclock("2:05.0") == 125.0


def test_narration_spoken_lines_are_parsed_and_checked():
    text = NARRATION.read_text(encoding="utf-8")
    script = rules.parse(text)
    captions = [c for s in script.scenes for c in s.captions]
    assert captions and all(c.say.strip() for c in captions), [c.text for c in captions if not c.say.strip()]
    words = sum(len(c.say.split()) for c in captions)
    assert 600 <= words <= 700, words
    for caption in captions:
        assert "`" not in caption.say and "/" not in caption.say, caption.say
        assert not re.search(r"\bI'm [A-Z]", caption.say), "the script names no person"
    assert rules.MAX_WORDS_PER_SECOND == 2.6
    # one spoken line changed at a time gives one problem each
    last = script.scenes[1].captions[-1]
    spoken = f"  > {last.say}"
    assert text.count(spoken) == 1
    for changed, word in ((text.replace(spoken, spoken + " see /home/x"), "private match (home)"),
                          (text.replace(spoken, "  > " + " ".join(["word"] * 40)), "words a second"),
                          (text.replace(spoken + "\n", ""), "no spoken line")):
        problems = rules.validate(rules.parse(changed))
        assert len(problems) == 1 and word in problems[0], problems
        if "private" in word:
            assert "spoken line" in problems[0], problems
    # a spoken line with no caption before it in its scene
    heading = "### 02-layer | terminal |"
    at = text.index(heading)
    end = text.index("\n", at) + 1
    stray = rules.parse(text[:end] + "  > a line nobody shows\n" + text[end:])
    assert any("a spoken line without a caption" in p for p in stray.problems), stray.problems
    # several `> ` lines under one caption are joined by one space
    doubled = rules.parse(text.replace(spoken, spoken + "\n  > And more."))
    assert doubled.scene(script.scenes[1].id).captions[-1].say == last.say + " And more."
    # the timed copy keeps the spoken lines, and parses to the same
    timed = text.replace(f"- [+{last.offset:.1f}] {last.text}", f"- [+{last.offset:.1f} | 1:09.0–1:14.0] {last.text}")
    again = rules.parse(timed + "\n## Scenes sped up\n\n- none\n")
    assert [c.say for s in again.scenes for c in s.captions] == [c.say for c in captions]
    assert rules.validate(again) == []


def test_no_run_statistics_in_the_video():
    for name in ("read_kpis", "Kpis", "KPI_SCENES", "kpi_problems", "spoken_numbers"):
        assert not hasattr(rules, name), name
    for name in ("kpis_for", "tiles_image", "results_bar_image"):
        assert not hasattr(scenes, name), name
    assert [p.name for p in DEMO.iterdir() if p.is_file() and "KPIS.md" in p.read_text(encoding="utf-8", errors="replace")] == []
    script = rules.parse(NARRATION.read_text(encoding="utf-8"))
    said = "\n".join(f"{c.text}\n{c.say}" for s in script.scenes for c in s.captions).lower()
    for number in ("forty-five", "45 ", "thirty-four"):
        assert number not in said, number
    assert not re.search(r"handed over \d", said)
    ids = [s.id for s in script.scenes]
    assert "01-what" not in ids and "05-not-yet" not in ids


# ------------------------------------------------------------------------------ the demo layer and the commands shown

PROTECTED = re.compile(r"#:protected \(([^)]*)\)")


def _protected(text: str) -> list[str]:
    return re.findall(r'"([^"]+)"', PROTECTED.search(text).group(1))


def _rota_protected() -> list[str]:
    return _protected((scenes.ROTA / "layer.rkt.in").read_text(encoding="utf-8"))


def test_the_demo_layer_compiles(tmp_path, monkeypatch):
    import rota_agent
    calls: list = []
    real_hidden = scenes.hidden
    monkeypatch.setattr(scenes, "hidden", lambda argv, *a, **k: calls.append([str(x) for x in argv]) or real_hidden(argv, *a, **k))
    site = scenes.demo_site(tmp_path)
    app, layer = site["app"], site["layer"]
    assert app == tmp_path / "rota" and (app / "rules.py").is_file() and (app / "tests" / "test_isolation.py").is_file()
    status = subprocess.run(["git", "-C", str(app), "status", "--porcelain"], capture_output=True, text=True)
    assert status.returncode == 0 and status.stdout == "", "the copy is committed once and clean"
    assert not (layer / "agentic-layer.json").exists(), "al gen is run by the scenes that need it, not by the setup"
    text = (layer / "agentic.rkt").read_text(encoding="utf-8")
    for needed in ("(adw feature ", "(adw tidy ", "(cmd test ", "(cmd isolation ", "(review agent #:reviewers 2)",
                   "(agent mock)", "#:agent replay", "{cwd} 1.0", str(scenes.STAND_IN)):
        assert needed in text, needed
    assert not re.search(r"@[A-Z_]+@", text) and "(forge" not in text and "cart" not in text
    assert _protected(text) == _rota_protected()
    assert f'"{rota_agent.PYTEST} tests"' in text and f'"{rota_agent.PYTEST} tests/test_isolation.py"' in text
    files = json.loads((layer / "mock" / "build.json").read_text())["files"]
    assert files.keys() == {"CONTRIBUTING.md"}
    assert "$" not in files["CONTRIBUTING.md"]
    assert files["CONTRIBUTING.md"].startswith((scenes.ROTA / "app" / "CONTRIBUTING.md").read_text())
    gen = al("gen", layer)
    assert "generated " in gen.stdout, gen.stdout
    assert al("check", layer).stdout.strip().endswith("check: ok")
    assert scenes.demo_site(tmp_path, generated=True)["layer"] == layer      # set up once, generated once
    assert not any("flask" in " ".join(argv) for argv in calls), "the setup does not warm Rota's Python (no network)"


def test_no_scene_shows_cart():
    for name in ("scenes.py", "narration.md", "demo_layer.rkt.in", "rota_agent.py"):
        text = (DEMO / name).read_text(encoding="utf-8")
        assert not re.search(r"(?i)\bcart\b", text), name
    source = (DEMO / "scenes.py").read_text(encoding="utf-8")
    for gone in ("live_demo", "replay_agent", "demo-shop", "dash_fixtures().build"):
        assert gone not in source, gone
    assert scenes.STAND_IN == DEMO / "rota_agent.py"
    assert scenes.APPROVAL == "edit:CONTRIBUTING.md"
    assert "CONTRIBUTING.md" in _rota_protected()
    allowed = "classify plan prove build test isolation commit evidence review ship".split()
    assert all(step in allowed for step in scenes.STEPS_A) and "lint" not in scenes.STEPS_A


def _load_rules(path: Path):
    spec = importlib.util.spec_from_file_location("edited_rota_rules", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module                 # dataclasses look their module up while the class is made
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_the_rota_stand_in_edits_rules_and_adds_tests(tmp_path):
    import py_compile
    import rota_agent
    app = tmp_path / "rota"
    shutil.copytree(scenes.ROTA / "app", app, ignore=shutil.ignore_patterns("__pycache__", "e2e-results", "*.db", ".pytest_cache"))
    assert rota_agent.apply(app) == ["rules.py", "tests/test_rules.py"]
    for name in ("rules.py", "tests/test_rules.py"):
        py_compile.compile(str(app / name), cfile=str(tmp_path / (name.replace("/", "_") + "c")), doraise=True)
    edited = _load_rules(app / "rules.py")
    nurse = edited.Worker(id=1, name="Test Nurse", role="Registered nurse", registration_expires=date(2027, 1, 31))

    def shift(shift_id, start, end):
        return edited.Shift(id=shift_id, role_required="Registered nurse", starts_at=datetime.fromisoformat(start),
                            ends_at=datetime.fromisoformat(end))

    day, today = shift(10, "2026-03-03T07:30", "2026-03-03T19:30"), date(2026, 3, 2)
    assert edited.NEEDS_REST == "Needs 11 hours rest"
    before = shift(20, "2026-03-02T13:30", "2026-03-02T21:30")          # ends 10 hours before
    after = shift(21, "2026-03-04T05:30", "2026-03-04T13:30")           # starts 10 hours after
    exactly = shift(22, "2026-03-02T12:30", "2026-03-02T20:30")         # ends exactly 11 hours before
    assert edited.assignment_problem(nurse, day, [before], today) == edited.NEEDS_REST
    assert edited.assignment_problem(nurse, day, [after], today) == edited.NEEDS_REST
    assert edited.assignment_problem(nurse, day, [exactly], today) is None
    with pytest.raises(ValueError):
        rota_agent.apply(app)                       # the anchors are gone: a drifted app is refused
    assert rota_agent.PYTEST.startswith("uv run --quiet --no-project")
    assert "flask>=3.1,<4" in rota_agent.PYTEST and "pytest>=8,<9" in rota_agent.PYTEST


def _env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != "AL_APP_ROOT"}


def _head(app: Path, ref: str) -> str:
    return subprocess.run(["git", "-C", str(app), "rev-parse", ref], capture_output=True, text=True, check=True).stdout.strip()


def test_the_scripted_rota_run_passes(tmp_path):
    site = scenes.demo_site(tmp_path, generated=True)
    scenes.warm_rota_python(site["work"])
    layer, app = site["layer"], site["app"]
    main = _head(app, "main")
    before = scenes.runs_of(layer)
    done = subprocess.run([str(AL), "run", str(layer), "/feature", scenes.PROMPT_A], cwd=site["work"], capture_output=True,
                          text=True, env=_env(), timeout=900)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    run = scenes.new_run(layer, before)
    state = scenes.run_state(layer, run)
    assert state.get("status") == "passed", state.get("status")
    steps = [(s.get("name"), s.get("status")) for s in state.get("steps", [])]
    assert sorted(steps) == sorted((name, "passed") for name in scenes.STEPS_A), steps
    changed = subprocess.run(["git", "-C", str(app), "diff", "--name-only", "main", state["branch"]], capture_output=True,
                             text=True, check=True).stdout.split()
    assert sorted(changed) == ["rules.py", "tests/test_rules.py"], changed
    assert _head(app, "main") == main, "nothing is merged"
    assert (layer / "agents" / run / "PR.md").is_file()


def _scrub(text: str, tmp_path: Path) -> str:
    return text.replace(str(tmp_path.resolve()), "~/work").replace(str(tmp_path), "~/work")


def test_rota_layer_excerpt_shows_checks_protected_paths_and_review():
    cp = subprocess.run(["sh", "-c", scenes.ROTA_EXCERPT], cwd=scenes.ROTA, capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stderr
    lines = cp.stdout.splitlines()
    assert len(lines) == 7 and all(len(line) <= 96 for line in lines), lines
    for needed in ("(cmd test", "(cmd isolation", "(cmd e2e", "#:protected", "(review auto", "#:require-checks"):
        assert needed in cp.stdout, needed
    assert "token-env" not in cp.stdout and "@FORGE_HOST@" not in cp.stdout, "the forge line is never shown"
    assert rules.find_private(cp.stdout, VALUES) == []
    for cue, keep in scenes.HIGHLIGHTS.items():
        kept = [line for line in lines if any(s in line for s in keep)]
        assert kept and len(kept) < len(lines), (cue, kept)
    assert len(scenes.ROTA_EXCERPT) <= 100 - 2


def test_rota_site_init_gen_check(tmp_path):
    site = scenes.rota_site(tmp_path)
    app = site["app"]
    assert app == tmp_path / "rota" and (app / "app.py").is_file() and (app / "tests" / "test_isolation.py").is_file()
    status = subprocess.run(["git", "-C", str(app), "status", "--porcelain"], capture_output=True, text=True)
    assert status.returncode == 0 and status.stdout == ""
    assert not site["layer"].exists(), "al init makes the layer on screen"
    env = {k: v for k, v in os.environ.items() if k != "AL_APP_ROOT"}
    init = subprocess.run([str(AL), "init", "rota"], cwd=tmp_path, capture_output=True, text=True, env=env, timeout=300)
    assert init.returncode == 0, init.stdout + init.stderr
    llm = (site["layer"] / "LLM.txt").read_text(encoding="utf-8")
    layer = scenes.install_rota_layer(tmp_path)
    assert layer == site["layer"]
    text = (layer / "agentic.rkt").read_text(encoding="utf-8")
    assert not re.search(r"@[A-Z_]+@", text) and scenes.FORGE_BASE in text
    assert (layer / "LLM.txt").read_text(encoding="utf-8") == llm and (layer / "planted").is_dir()
    gen = subprocess.run([str(AL), "gen", "rota.al"], cwd=tmp_path, capture_output=True, text=True, env=env, timeout=600)
    assert gen.returncode == 0 and "generated " in gen.stdout, gen.stdout + gen.stderr
    check = subprocess.run([str(AL), "check", "rota.al"], cwd=tmp_path, capture_output=True, text=True, env=env, timeout=600)
    assert check.returncode == 0 and check.stdout.strip().endswith("check: ok"), check.stdout + check.stderr
    for cp in (init, gen, check):
        assert rules.find_private(_scrub(cp.stdout, tmp_path), VALUES) == [], cp.args
    status = subprocess.run(["git", "-C", "rota", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True)
    assert status.returncode == 0 and status.stdout == "", "the app is untouched"
    (app / "marker.txt").write_text("kept")
    assert scenes.rota_site(tmp_path)["app"] == app and (app / "marker.txt").is_file(), "a second call reuses the folder"


def test_every_al_command_the_demo_shows_is_accepted():
    shown = [scenes.fill(argv, scenes.SAMPLES) for argv in scenes.COMMANDS.values()]
    assert len(shown) >= 4 and all(argv[0] == "al" for argv in shown)
    assert set(scenes.COMMANDS) == {"init", "gen", "check", "run"} and "approve" not in scenes.COMMANDS
    assert not [a for argv in shown for a in argv if "{" in a], "every placeholder has a sample"
    env = {k: v for k, v in os.environ.items() if k != "AL_APP_ROOT"}
    for argv in shown:
        cp = subprocess.run([str(AL), *argv[1:], "--help"], capture_output=True, text=True, env=env, timeout=120)
        assert cp.returncode == 0, (argv, cp.stdout, cp.stderr)
        for flag in (a for a in argv if a.startswith("--")):
            assert flag in cp.stdout, (argv, flag)


def test_the_protected_run_waits_for_approve_and_resume(tmp_path):
    site = scenes.demo_site(tmp_path, generated=True)
    scenes.warm_rota_python(site["work"])
    layer = site["layer"]
    before = scenes.runs_of(layer)
    stopped = subprocess.run([str(AL), "run", str(layer), "/tidy", scenes.PROMPT_B], cwd=site["work"], capture_output=True,
                             text=True, env=_env(), timeout=900)
    assert stopped.returncode == 1, stopped.stdout[-3000:] + stopped.stderr[-3000:]
    run = scenes.new_run(layer, before)
    assert "evidence:needs-person" in json.dumps(scenes.run_state(layer, run))
    # the same approvable.approve_resume the dashboard's Approve & resume calls
    approved = subprocess.run([str(AL), "approve", str(layer), run, "--all", "--who", scenes.WHO], cwd=site["work"],
                              capture_output=True, text=True, env=_env(), timeout=600)
    assert approved.returncode == 0 and "resuming from step evidence" in approved.stdout, approved.stdout + approved.stderr
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline and scenes.run_state(layer, run).get("status") != "passed":
        time.sleep(0.5)
    state = scenes.run_state(layer, run)
    assert state.get("status") == "passed", state.get("status")
    approvals = json.dumps(state.get("approvals"))
    assert scenes.APPROVAL in approvals and scenes.WHO in approvals, approvals


def test_the_refused_review_fixture_is_a_rota_change(tmp_path):
    import fixture_imports as fx
    layer = scenes.demo_site(tmp_path, generated=True)["layer"]
    rid = scenes.add_refused_rota_run(layer)
    d = fx.review_model().d
    model = d.Model([layer], [])
    model.discover()
    model.tick()
    summary = model.summaries[f"rota/{rid}"]
    assert summary["status"] == "handed-over"
    assert next(s for s in summary["steps"] if s["name"] == "review")["status"] == "rejected"
    state = json.loads((layer / "agents" / rid / "state.json").read_text())
    review = model.run_detail("rota", rid)["steps_detail"]["review"]
    assert len(review.get("rounds") or []) == 3 or len(state["review"]["rounds"]) == 3
    patch = (scenes.ROTA / "planted" / "planted.patch").read_text(encoding="utf-8")
    assert state["ship"]["gate"]["changed_files"] == re.findall(r"^\+\+\+ b/(\S+)", patch, re.M)
    assert "Expired" in state["prompt"]
    dumped = json.dumps(state)
    assert not re.search(r"(?i)\bcart\b", dumped) and "shop" not in dumped


def test_the_rota_pictures_are_committed():
    folder = ROOT / "docs" / "demo" / "rotademo"
    for name, path in scenes.PICTURES.items():
        assert path.is_file() and path.parent == folder and path.suffix in (".png", ".gif"), (name, path)
    for fn in (scenes.scene_01_problem, scenes.scene_04_rota, scenes.scene_05_handoff):
        assert "PICTURES[" in inspect.getsource(fn), fn.__name__
    assert scenes.example_rows() == [("flask", "Python"), ("ripgrep", "Rust"), ("gin", "Go"), ("express", "JavaScript"),
                                     ("jq", "C")]
    assert any("Not yet shown" in line for line in (ROOT / "README.md").read_text(encoding="utf-8").splitlines())


def test_picture_frames_fit_the_video():
    pytest.importorskip("PIL")
    board = scenes.picture_frames(scenes.PICTURES["board"], "Rota", "# docs/demo/rotademo/before.png")
    assert board and all(frame.size == rules.SIZE for frame in board)
    rest = scenes.gif_frames(scenes.PICTURES["rest"], "Rota", "# docs/demo/rotademo/a-rest-rule.gif", max_frames=50)
    assert 0 < len(rest) <= 50 and all(frame.size == rules.SIZE for frame in rest)
    top = scenes.picture_image(scenes.PICTURES["handoff"], "Rota", "# source", crop=(0, 0, 872, 700))
    ask = scenes.picture_image(scenes.PICTURES["handoff"], "Rota", "# source", crop=(0, 700, 872, 1047))
    box = (0, 125, rules.SIZE[0], rules.SIZE[1] - rules.STRIP_H - 45)
    assert top.crop(box).tobytes() != ask.crop(box).tobytes()
    with pytest.raises(ValueError):
        scenes.picture_image(scenes.PICTURES["board"], "x" * 200, "# source")


def test_recorder_keeps_videos_out_of_the_tree():
    present = [p for p in DEMO.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES]
    assert present == [], "tools/demovideo holds no video, sound or picture"
    cp = subprocess.run(["git", "-C", str(ROOT), "ls-files", "tools/demovideo", ".al-proof", "media"], capture_output=True, text=True,
                        env={**os.environ, **SANDBOX_GIT})
    if cp.returncode == 0:
        assert [p for p in cp.stdout.splitlines() if p.lower().endswith(MEDIA_SUFFIXES) or p.startswith((".al-proof/demo", "media/"))] == []
    make = (DEMO / "make.py").read_text(encoding="utf-8")
    assert '".al-proof/demo"' in make and '"media/demo-video"' in make
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/media/" in ignore, "the fallback folder is ignored by git"
    for script in ("make.py", "verify.py", "scenes.py", "rules.py", "rota_agent.py"):
        text = (DEMO / script).read_text(encoding="utf-8")
        assert not re.search(r"https?://(?!127\.0\.0\.1)", text), f"{script} names no address to send anything to"
    tts_text = (DEMO / "tts.py").read_text(encoding="utf-8")
    assert set(re.findall(r"https?://[^\s\"'/]+", tts_text)) == {"https://api.elevenlabs.io"}, "tts.py names only ElevenLabs"



# ------------------------------------------------------------------------------ the voice (tts.py, against a fake server)

KEY = "sk-test-0123456789abcdef-not-real"


def _make():
    """tools/demovideo/make.py under a name of its own (tools/showcase/make.py is called make.py too)."""
    name = "demovideo_make"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, DEMO / "make.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _tone_wav(lead: float, tone: float, tail: float) -> bytes:
    """44.1 kHz 16-bit mono: `lead` s of silence, `tone` s of 440 Hz at half scale, `tail` s of silence."""
    rate = 44100
    samples = [0] * int(lead * rate)
    samples += [int(16383 * math.sin(2 * math.pi * 440 * n / rate)) for n in range(int(tone * rate))]
    samples += [0] * int(tail * rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buffer.getvalue()


def _files_holding(root: Path, needle: str) -> list[Path]:
    return [p for p in Path(root).rglob("*") if p.is_file() and needle.encode() in p.read_bytes()]


def _wav_info(path: Path) -> tuple[float, int, int]:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / float(wav.getframerate()), wav.getframerate(), wav.getnchannels()


class _FakeEleven:
    def __init__(self):
        self.requests: list[dict] = []
        self.answer = (200, "audio/mpeg", _tone_wav(0.2, 1.0, 0.2))
        self.root = ""


@pytest.fixture
def fake_eleven():
    fake = _FakeEleven()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            fake.requests.append({"method": "POST", "path": self.path, "headers": dict(self.headers.items()), "body": body})
            status, content_type, answer = fake.answer
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    fake.root = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()


def _settings(voice: str = tts.DEFAULT_VOICE_ID, key: str = KEY) -> tts.Settings:
    return tts.Settings(voice_id=voice, model=tts.MODEL, output_format=tts.OUTPUT_FORMAT, key=key)


def test_tts_backend_follows_the_environment(monkeypatch):
    monkeypatch.setenv(tts.KEY_VAR, KEY)
    assert tts.choose("auto", os.environ, espeak=True) == "elevenlabs"
    assert tts.choose("auto", {tts.KEY_VAR: KEY}, espeak=False) == "elevenlabs"
    for blank in ("", "  "):
        monkeypatch.setenv(tts.KEY_VAR, blank)
        assert tts.choose("auto", os.environ, espeak=True) == "local"
        assert tts.choose("auto", os.environ, espeak=False) == "none"
    monkeypatch.delenv(tts.KEY_VAR)
    assert tts.choose("auto", os.environ, espeak=False) == "none"
    with pytest.raises(tts.TtsError, match="ELEVENLABS_API_KEY"):
        tts.choose("elevenlabs", os.environ, espeak=True)
    with pytest.raises(tts.TtsError, match="espeak-ng"):
        tts.choose("local", os.environ, espeak=False)
    assert tts.choose("local", os.environ, espeak=True) == "local"
    assert tts.choose("none", {tts.KEY_VAR: KEY}, espeak=True) == "none"
    monkeypatch.setenv(tts.KEY_VAR, f" {KEY} ")
    monkeypatch.delenv(tts.VOICE_VAR, raising=False)
    settings = tts.Settings.from_env(os.environ)
    assert (settings.voice_id, settings.model, settings.output_format, settings.key) == (
        "JBFqnCBsd6RMkjVDRZzb", "eleven_multilingual_v2", "mp3_44100_128", KEY)
    assert KEY not in repr(settings)
    monkeypatch.setenv(tts.VOICE_VAR, " someVoice42 ")
    assert tts.Settings.from_env(os.environ).voice_id == "someVoice42"


def _stub_say(tmp_path: Path) -> Path:
    """A stand-in for kit/runtime/voice/say.py: the same command line, a short tone as the WAV, and one JSON
    line per call in calls.jsonl beside it. STUB_SAY_EXIT makes it fail after writing a part of the file."""
    folder = tmp_path / "stub"
    folder.mkdir(exist_ok=True)
    tone = folder / "tone.wav"
    tone.write_bytes(_tone_wav(0.1, 1.0, 0.1))
    stub = folder / "say.py"
    stub.write_text(f"""import argparse, json, os, shutil, sys
from pathlib import Path
ap = argparse.ArgumentParser()
ap.add_argument("--voice", required=True)
ap.add_argument("--speed", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--no-play", action="store_true")
a = ap.parse_args()
if not a.no_play:
    sys.exit(2)
text = sys.stdin.read()
with open(Path(__file__).with_name("calls.jsonl"), "a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "text": text}}) + "\\n")
shutil.copyfile({str(tone)!r}, a.out)
sys.exit(int(os.environ.get("STUB_SAY_EXIT") or 0))
""")
    return stub


def _stub_calls(stub: Path) -> list[dict]:
    log = stub.with_name("calls.jsonl")
    return [json.loads(line) for line in log.read_text().splitlines()] if log.is_file() else []


def test_tts_kokoro_backend_choice(tmp_path, monkeypatch):
    monkeypatch.delenv(tts.KEY_VAR, raising=False)
    assert tts.BACKENDS == ("auto", "elevenlabs", "kokoro", "local", "none")
    assert tts.choose("auto", os.environ, espeak=True, kokoro=True) == "kokoro"
    assert tts.choose("auto", os.environ, espeak=False, kokoro=True) == "kokoro"
    assert tts.choose("auto", {tts.KEY_VAR: KEY}, espeak=True, kokoro=True) == "elevenlabs"
    assert tts.choose("auto", os.environ, espeak=True, kokoro=False) == "local"
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}" if name == "uv" else None)
    assert tts.choose("kokoro", os.environ, espeak=False) == "kokoro"
    monkeypatch.setattr(tts, "SAY", tmp_path / "missing" / "say.py")
    with pytest.raises(tts.TtsError, match="say.py"):
        tts.choose("kokoro", os.environ, espeak=True)
    monkeypatch.setattr(tts, "SAY", _stub_say(tmp_path))
    cache = tmp_path / "kokoro-cache"
    cache.mkdir()
    env = {"AL_KOKORO_CACHE": str(cache), "HOME": str(tmp_path / "nohome")}
    assert tts.kokoro_caches(env)[0] == cache
    assert tts.kokoro_caches({"XDG_CACHE_HOME": "/x/cache", "HOME": "/h"})[0] == Path("/x/cache/al/kokoro")
    assert not tts.kokoro_ready(env)
    (cache / "kokoro-v1.0.onnx").write_bytes(b"model")
    (cache / "voices-v1.0.bin").write_bytes(b"voices")
    assert tts.kokoro_ready(env)
    (cache / "kokoro-v1.0.onnx").unlink()
    (cache / "kokoro-v1.0.int8.onnx").write_bytes(b"model")
    assert tts.kokoro_ready(env)
    (cache / "kokoro-v1.0.int8.onnx").write_bytes(b"")
    assert not tts.kokoro_ready(env)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    (cache / "kokoro-v1.0.int8.onnx").write_bytes(b"model")
    assert not tts.kokoro_ready(env), "no uv: Kokoro cannot run"


def test_tts_kokoro_lines_are_cached_with_a_stub(tmp_path, monkeypatch):
    stub = _stub_say(tmp_path)
    monkeypatch.setattr(tts, "SAY", stub)
    monkeypatch.setattr(tts, "KOKORO_RUN", (sys.executable,))
    monkeypatch.delenv(tts.KOKORO_VOICE_VAR, raising=False)
    monkeypatch.delenv("STUB_SAY_EXIT", raising=False)
    settings = tts.KokoroSettings.from_env(os.environ)
    assert (settings.voice, settings.speed) == ("af_heart", 1.0)
    cache = tmp_path / "voice-cache"
    first, cached = tts.kokoro_line("One line.", cache, settings)
    assert not cached and first.is_file() and first.stat().st_size > 0
    calls = _stub_calls(stub)
    assert len(calls) == 1 and calls[0]["text"] == "One line."
    argv = calls[0]["argv"]
    assert argv[:4] == ["--voice", "af_heart", "--speed", "1"] and argv[4] == "--out" and argv[6:] == ["--no-play"]
    again, cached = tts.kokoro_line("One line.", cache, settings)
    assert cached and again == first and len(_stub_calls(stub)) == 1
    monkeypatch.setenv(tts.KOKORO_VOICE_VAR, " bm_george ")
    other = tts.KokoroSettings.from_env(os.environ)
    assert other.voice == "bm_george"
    path, cached = tts.kokoro_line("One line.", cache, other)
    assert not cached and path != first and len(_stub_calls(stub)) == 2
    assert _stub_calls(stub)[1]["argv"][1] == "bm_george"
    names = sorted(p.name for p in cache.iterdir())
    assert len(names) == 2 and all(re.fullmatch(r"[0-9a-f]{64}\.wav", name) for name in names), names
    failing = tmp_path / "failing-cache"
    monkeypatch.setenv("STUB_SAY_EXIT", "1")
    with pytest.raises(tts.TtsError):
        tts.kokoro_line("Another line.", failing, settings)
    assert not failing.exists() or list(failing.iterdir()) == []


def test_tts_sends_the_key_only_in_the_header(fake_eleven):
    served = fake_eleven.answer[2]
    got = tts.request_speech("Hello there.", _settings("voiceA"), api_root=fake_eleven.root)
    assert got == served
    assert len(fake_eleven.requests) == 1
    request = fake_eleven.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/v1/text-to-speech/voiceA?output_format=mp3_44100_128"
    headers = {k.lower(): v for k, v in request["headers"].items()}
    assert headers["xi-api-key"] == KEY
    assert json.loads(request["body"]) == {"text": "Hello there.", "model_id": "eleven_multilingual_v2"}
    assert KEY not in request["path"] and KEY.encode() not in request["body"]


def test_tts_caches_by_text_voice_and_model(fake_eleven, tmp_path):
    cache = tmp_path / "voice-cache"
    first, cached = tts.fetch_line("One line.", cache, _settings(), api_root=fake_eleven.root)
    assert not cached and first.read_bytes() == fake_eleven.answer[2]
    again, cached = tts.fetch_line("One line.", cache, _settings(), api_root=fake_eleven.root)
    assert cached and again == first and len(fake_eleven.requests) == 1
    tts.fetch_line("One line.", cache, _settings("otherVoice"), api_root=fake_eleven.root)
    assert len(fake_eleven.requests) == 2
    tts.fetch_line("Another line.", cache, _settings(), api_root=fake_eleven.root)
    assert len(fake_eleven.requests) == 3
    _path, cached = tts.fetch_line("One line.", cache, _settings(key="sk-test-another-key-not-real"), api_root=fake_eleven.root)
    assert cached and len(fake_eleven.requests) == 3, "the key is not part of the cache name"
    names = sorted(p.name for p in cache.iterdir())
    assert len(names) == 3 and all(re.fullmatch(r"[0-9a-f]{64}\.mp3", name) for name in names), names
    assert _files_holding(tmp_path, KEY) == []


def _fake_clips(work: Path) -> None:
    parsed = rules.parse(NARRATION.read_text(encoding="utf-8"))
    clips = work / "clips"
    clips.mkdir(parents=True)
    for scene in parsed.scenes:
        offsets = [c.offset for c in scene.captions] + [scene.seconds]
        captions = [{"offset": c.offset, "start": c.offset, "end": offsets[i + 1], "text": c.text, "moved": 0.0}
                    for i, c in enumerate(scene.captions)]
        (clips / f"{scene.id}.mp4").write_bytes(b"")
        (clips / f"{scene.id}.json").write_text(json.dumps({"scene": scene.id, "seconds": scene.seconds, "speed": 1.0,
                                                             "real_seconds": scene.seconds, "hold": 0.0, "captions": captions,
                                                             "blanked": [], "shortened": [], "stand_ins": []}))


def test_tts_failure_does_not_leak_the_key(fake_eleven, tmp_path, monkeypatch, capsys):
    fake_eleven.answer = (401, "application/json", json.dumps({"detail": f"invalid api key {KEY}"}).encode())
    cache = tmp_path / "voice-cache"
    with pytest.raises(tts.TtsError) as caught:
        tts.fetch_line("One line.", cache, _settings(), api_root=fake_eleven.root)
    assert "401" in str(caught.value) and KEY not in str(caught.value)
    assert not cache.exists() or list(cache.iterdir()) == []
    # the whole command: assemble stops before any video is made
    work, out = tmp_path / "work", tmp_path / "out"
    _fake_clips(work)
    monkeypatch.setattr(tts, "API_ROOT", fake_eleven.root)
    monkeypatch.setenv(tts.KEY_VAR, KEY)
    capsys.readouterr()
    code = _make().main(["assemble", "--work", str(work), "--out", str(out), "--voice", "elevenlabs"])
    printed = capsys.readouterr()
    assert code == 1, printed
    assert "401" in printed.out + printed.err and KEY not in printed.out + printed.err
    assert _files_holding(tmp_path, KEY) == []
    assert not (out / "al-demo.mp4").exists() and not (out / "narration.md").exists()


def _needs_ffmpeg():
    if not shutil.which("ffmpeg"):
        pytest.skip("no ffmpeg on PATH")


def test_tts_fits_a_voiced_line_to_its_caption(tmp_path, monkeypatch):
    _needs_ffmpeg()
    monkeypatch.setenv(tts.KEY_VAR, KEY)
    argvs: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(argv, *args, **kwargs):
        argvs.append([str(a) for a in argv])
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)
    audio = tmp_path / "line.mp3"
    audio.write_bytes(_tone_wav(0.5, 2.0, 0.5))
    roomy = tmp_path / "roomy.wav"
    assert tts.fit_line(audio, roomy, slot=3.0) == 1.0
    seconds, rate, channels = _wav_info(roomy)
    assert abs(seconds - 2.0) <= 0.15 and (rate, channels) == (44100, 1), seconds
    tight = tmp_path / "tight.wav"
    tempo = tts.fit_line(audio, tight, slot=1.8)
    assert 1.0 < tempo <= 1.35
    assert _wav_info(tight)[0] <= 1.8
    with pytest.raises(tts.TtsError, match="×1.35"):
        tts.fit_line(audio, tmp_path / "short.wav", slot=1.0)
    with pytest.raises(tts.TtsError, match="×1.12"):
        tts.fit_line(audio, tmp_path / "short.wav", slot=1.0, max_tempo=1.12)
    assert argvs and all(argv[0] == "ffmpeg" for argv in argvs)
    assert not [argv for argv in argvs if any(KEY in a for a in argv)]


def test_assemble_voices_through_elevenlabs_with_the_fake_server(fake_eleven, tmp_path, monkeypatch, capsys):
    _needs_ffmpeg()
    monkeypatch.setattr(tts, "API_ROOT", fake_eleven.root)
    monkeypatch.setenv(tts.KEY_VAR, KEY)
    monkeypatch.delenv(tts.VOICE_VAR, raising=False)
    make = _make()
    build, cache = tmp_path / "build", tmp_path / "voice-cache"
    build.mkdir()
    lines = [("Hello there.", 0.0, 3.0), ("Second line.", 3.0, 3.0)]
    delays, voice = make.voice_lines(lines, build, cache, "elevenlabs")
    assert [start for _wav, start in delays] == [0.3, 3.3]
    assert all(_wav_info(wav)[0] <= 2.7 for wav, _start in delays)
    assert "Only the spoken lines were sent to the ElevenLabs service" in voice
    assert "ElevenLabs" in voice and "JBFqnCBsd6RMkjVDRZzb" in voice and "eleven_multilingual_v2" in voice and KEY not in voice
    assert len(fake_eleven.requests) == 2
    make.voice_lines(lines, build, cache, "elevenlabs")
    assert len(fake_eleven.requests) == 2, "the second assemble takes every line from the cache"
    printed = capsys.readouterr()
    assert KEY not in printed.out + printed.err
    assert _files_holding(tmp_path, KEY) == []


def test_assemble_voices_through_kokoro_with_a_stub(tmp_path, monkeypatch):
    _needs_ffmpeg()
    stub = _stub_say(tmp_path)
    monkeypatch.setattr(tts, "SAY", stub)
    monkeypatch.setattr(tts, "KOKORO_RUN", (sys.executable,))
    monkeypatch.delenv(tts.KOKORO_VOICE_VAR, raising=False)
    monkeypatch.delenv("STUB_SAY_EXIT", raising=False)
    make = _make()
    build, cache = tmp_path / "build", tmp_path / "voice-cache"
    build.mkdir()
    lines = [("Hello there.", 0.0, 3.0), ("Second line.", 3.0, 3.0)]
    delays, voice = make.voice_lines(lines, build, cache, "kokoro")
    assert [start for _wav, start in delays] == [0.3, 3.3]
    assert all(_wav_info(wav)[0] <= 2.7 for wav, _start in delays)
    assert "Kokoro" in voice and "af_heart" in voice and "synthetic" in voice
    assert len(_stub_calls(stub)) == 2
    make.voice_lines(lines, build, cache, "kokoro")
    assert len(_stub_calls(stub)) == 2, "the second assemble takes every line from the cache"


def test_assemble_audio_is_led_normalised_and_128k():
    make = _make()
    assert make.VOICE_LEAD == 0.3 and make.FADE == 0.35
    chain = ";".join(make.mix_filter([(Path("a.wav"), 0.3), (Path("b.wav"), 4.0)], 296.0))
    assert "loudnorm=I=-16" in chain and "adelay=300" in chain and "adelay=4000" in chain and chain.endswith("[a]")
    assert "128k" in make.AUDIO and "aac" in make.AUDIO
    note = make.voice_note("kokoro", "af_heart")
    assert "synthetic" in note and "Kokoro" in note and "af_heart" in note
    assert "ElevenLabs" in make.voice_note("elevenlabs", "JBFqnCBsd6RMkjVDRZzb") and "espeak-ng" in make.voice_note("local", "")
    assert make.voice_note("none", "") == ""
    for backend, detail in (("kokoro", "af_heart"), ("elevenlabs", "JBFqnCBsd6RMkjVDRZzb"), ("local", "")):
        assert rules.find_private(make.voice_note(backend, detail)) == []


def test_part_boundary_fades():
    make = _make()
    parsed = rules.parse(NARRATION.read_text(encoding="utf-8"))
    seconds = {s.id: s.seconds for s in parsed.scenes}

    def fade(scene_id):
        return make.fade_filter(parsed, scene_id, seconds[scene_id])
    assert "fade=t=in:st=0:d=0.35" in fade("01-problem") and "fade=t=out" in fade("01-problem")
    assert seconds["01-problem"] == 28
    assert f"fade=t=out:st={seconds['01-problem'] - 0.35:.3f}:d=0.35" in fade("01-problem")
    assert "fade=t=in" in fade("05-proof") and "fade=t=out" not in fade("05-proof") and "0.35" in fade("05-proof")
    assert fade("05-handoff") == ""
    assert "fade=t=out" in fade("05-review-no") and "fade=t=in" not in fade("05-review-no") and "0.35" in fade("05-review-no")


def test_the_story_is_the_new_arc():
    script = rules.parse(NARRATION.read_text(encoding="utf-8"))
    assert [s.id for s in script.scenes] == ARC == list(scenes.SCENES) and script.seconds == 293
    for gone in ("01-what", "04-own-plan", "05-limits", "05-not-yet", "06-close", "03-start"):
        assert gone not in scenes.SCENES, gone
    assert "own_plan" not in {f.name for f in dataclasses.fields(scenes.Ctx)}
    assert "--own-plan" not in (DEMO / "make.py").read_text(encoding="utf-8")
    assert "dashboard" not in scenes.COMMANDS
    run = inspect.getsource(scenes.scene_04_run)
    assert "pause(6)" not in run and '"Timeline"' not in run


def test_browser_scenes_zoom_and_spotlight():
    assert scenes.ZOOM == 1.25 and callable(getattr(scenes.Browser, "spot", None))
    for fn in (scenes.scene_04_run, scenes.scene_05_proof, scenes.scene_05_protected, scenes.scene_05_review_no):
        assert ".spot(" in inspect.getsource(fn), fn.__name__
    for script in (scenes.ZOOMER, scenes.SPOTTER):
        assert "<style" not in script and "<script" not in script
    assert "highlight_frame" in inspect.getsource(scenes.scene_02_layer)


def test_approve_resume_scene_uses_the_dashboard():
    source = inspect.getsource(scenes.scene_05_protected)
    for needed in ("button.approve-resume", "approve-resume-go", "aria-label='your name'", "resuming from step evidence"):
        assert needed in source, needed
    assert "COMMANDS[" not in source


def test_assemble_offers_the_voice_option(monkeypatch, capsys):
    make = _make()
    with pytest.raises(SystemExit) as done:
        make.main(["assemble", "--help"])
    assert done.value.code == 0
    shown = capsys.readouterr().out
    assert "--voice" in shown and "{auto,elevenlabs,kokoro,local,none}" in shown
    with pytest.raises(SystemExit) as done:
        make.main(["assemble", "--work", "/nonexistent", "--voice", "local", "--no-voice"])
    assert done.value.code == 2
    capsys.readouterr()
    monkeypatch.delenv(tts.KEY_VAR, raising=False)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/espeak-ng" if name == "espeak-ng" else None)
    assert tts.choose("auto", os.environ, bool(shutil.which("espeak-ng"))) == "local"


def test_showcase_readme_documents_the_voice():
    text = (ROOT / "docs" / "showcase" / "README.md").read_text(encoding="utf-8")
    for needed in ("ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb", "--voice", "voice-cache",
                   "--voice kokoro", "af_heart", "AL_DEMO_KOKORO_VOICE", "-16 LUFS", "> "):
        assert needed in text, needed
    assert not re.search(r"sk_[A-Za-z0-9]{20,}", text), "no real-looking key in the docs"


def test_showcase_readme_tells_the_new_story():
    text = (ROOT / "docs" / "showcase" / "README.md").read_text(encoding="utf-8")
    for needed in ("seven parts", "Rota", "Approve & resume", "tests/rotademo/layer.rkt.in", "make.py scene 01-problem",
                   "tools/demovideo/rota_agent.py", "edit:CONTRIBUTING.md", "Flask"):
        assert needed in text, needed
    assert "`cart` library" not in text and "replay_agent.py" not in text
    assert "docs/KPIS.md" not in text and "--own-plan" not in text


# ------------------------------------------------------------------------------ the recorded video

@functools.lru_cache(maxsize=None)
def _verify(folder: str) -> tuple[int, dict]:
    env = {k: v for k, v in os.environ.items() if k != "AL_APP_ROOT"}
    cp = subprocess.run(["uv", "run", str(DEMO / "verify.py"), folder, "--json"], capture_output=True, text=True, env=env, timeout=1500)
    assert cp.stdout.strip().startswith("{"), cp.stdout + cp.stderr
    return cp.returncode, json.loads(cp.stdout)


def _verified() -> tuple[int, dict, Path]:
    folder = Path(os.environ.get("AL_DEMO_VIDEO_DIR") or ROOT / ".al-proof" / "demo")
    if not (folder / "al-demo.mp4").is_file():
        pytest.skip("no recorded demo in .al-proof/demo: record it with tools/demovideo/make.py")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            pytest.skip(f"no {tool} on PATH: the recorded demo is not verified")
    code, report = _verify(str(folder))
    return code, report, folder


def test_recorded_demo_has_the_right_shape():
    _code, report, _folder = _verified()
    shape = report["shape"]
    assert shape["ok"], shape["problems"]
    assert shape["codec"] == "h264" and (shape["width"], shape["height"]) in (("1280", "720"), ("1920", "1080"))
    assert shape["pix_fmt"] == "yuv420p" and 270 <= shape["duration"] <= 300 and shape["decodes"]


def test_recorded_demo_has_a_contact_sheet():
    _code, report, folder = _verified()
    sheet = folder / "al-demo-contact-sheet.png"
    assert report["sheet"]["ok"] and sheet.is_file()
    assert sheet.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    expected = int(report["shape"]["duration"] // 10) + 1
    assert abs(report["sheet"]["frames"] - expected) <= 1, (report["sheet"]["frames"], expected)


def test_recorded_demo_narration_has_times_for_every_scene():
    _code, report, folder = _verified()
    assert report["narration"]["ok"], report["narration"]["problems"]
    script = rules.parse((folder / "narration.md").read_text(encoding="utf-8"))
    assert [p.title for p in script.parts] == list(rules.TITLES) and rules.validate(script) == []
    at = 0.0
    for scene in script.scenes:
        assert scene.start is not None and scene.end is not None and abs(scene.start - at) < 0.11, scene.id
        assert all(c.start is not None and c.end is not None and scene.start <= c.start < c.end <= scene.end + 0.11
                   for c in scene.captions), scene.id
        at = scene.end
    assert abs(at - report["shape"]["duration"]) <= 1.0
    text = (folder / "narration.md").read_text(encoding="utf-8")
    for section in ("## Scenes sped up", "## Blanked on screen", "## Stand-ins and fixtures", "mute: ffmpeg -i al-demo.mp4"):
        assert section in text, section


def test_recorded_demo_privacy_ocr_finds_nothing():
    _code, report, folder = _verified()
    ocr = report["ocr"]
    assert ocr["frames"] >= report["shape"]["duration"] / 5, ocr["frames"]
    assert ocr["hits"] == [] and ocr["ok"]
    assert set(report["rules"]["kinds"]) == set(rules.KINDS_OF_HIT)
    assert (folder / "privacy-ocr.json").is_file()
    assert len(ocr["captions"]) == ocr["frames"] and any(sample["read"] for sample in ocr["captions"])


def test_recorded_demo_has_a_caption_band_on_every_sampled_frame():
    code, report, _folder = _verified()
    assert report["bands"]["failures"] == [] and report["bands"]["ok"] and report["bands"]["frames"] == report["ocr"]["frames"]
    assert report["ok"] and code == 0


# ------------------------------------------------------------------------------ recording (slow)

def _recording() -> dict[str, str]:
    if os.environ.get("AL_SHOWCASE_RECORD") != "1":
        pytest.skip("records with ffmpeg: set AL_SHOWCASE_RECORD=1")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("no ffmpeg")
    return {k: v for k, v in os.environ.items() if k != "AL_APP_ROOT"}


@pytest.mark.slow
def test_recording_one_scene(tmp_path):
    env = _recording()
    pytest.importorskip("PIL")
    work = tmp_path / "work"
    cp = subprocess.run(["uv", "run", str(DEMO / "make.py"), "scene", "02-layer", "--work", str(work)],
                        capture_output=True, text=True, env=env, timeout=1800)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    clip = work / "clips" / "02-layer.mp4"
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=codec_name,width,height:format=duration", "-of", "default=noprint_wrappers=1", str(clip)],
                           capture_output=True, text=True, check=True)
    info = dict(line.split("=", 1) for line in probe.stdout.splitlines() if "=" in line)
    assert (info["codec_name"], info["width"], info["height"]) == ("h264", "1280", "720")
    assert abs(float(info["duration"]) - 34) <= 0.11
    record = json.loads((work / "clips" / "02-layer.json").read_text())
    assert record["seconds"] == 34 and record["speed"] == 1.0 and len(record["captions"]) == 5
    assert not [p for p in DEMO.rglob("*") if p.suffix.lower() in MEDIA_SUFFIXES]
    assert subprocess.run(["uv", "run", str(DEMO / "make.py"), "clean", "--work", str(work)], capture_output=True, env=env).returncode == 0
    assert not work.exists()


@pytest.mark.slow
def test_verify_refuses_a_private_frame_and_a_wrong_length(tmp_path):
    env = _recording()
    font = subprocess.run(["fc-match", "-f", "%{file}", "DejaVu Sans Mono"], capture_output=True, text=True).stdout.strip()
    if not font or not Path(font).is_file():
        pytest.skip("no font for the test video")
    (tmp_path / "a.txt").write_text("mail ada@example.org")
    (tmp_path / "b.txt").write_text("see /home/ada/work")
    draw = (f"drawtext=fontfile={font}:textfile={tmp_path / 'a.txt'}:fontcolor=white:fontsize=40:x=80:y=200,"
            f"drawtext=fontfile={font}:textfile={tmp_path / 'b.txt'}:fontcolor=white:fontsize=40:x=80:y=320")
    made = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x0d1117:s=1280x720:r=15:d=12", "-vf", draw,
                           "-c:v", "libx264", "-pix_fmt", "yuv420p", str(tmp_path / "al-demo.mp4")], capture_output=True, text=True)
    assert made.returncode == 0, made.stderr
    cp = subprocess.run(["uv", "run", str(DEMO / "verify.py"), str(tmp_path), "--json"], capture_output=True, text=True, env=env, timeout=1500)
    assert cp.returncode == 1, cp.stdout + cp.stderr
    report = json.loads(cp.stdout)
    assert not report["ok"] and not report["shape"]["ok"] and any("duration" in p for p in report["shape"]["problems"])
    assert {"mail", "home"} <= {hit["kind"] for hit in report["ocr"]["hits"]}
    assert len(report["bands"]["failures"]) == report["bands"]["frames"] >= 2
    assert not report["narration"]["ok"]


# ------------------------------------------------------------------------------ the second story: flask on a local Forgejo

TESTBED_NARRATION = DEMO / "narration-testbed.md"
TESTBED_IDS = ["01-problem", "02-layer", "03-use", "04-run", "04-pr", "05-proof", "05-protected", "05-review-no", "06-fit",
               "07-close"]
AUTHORED_FLASK = ROOT / "tests" / "authored" / "flask"
PINNED_RETURN = '    return bool(val and val.lower() not in {"0", "false", "no"})'
FAKE_TEST_CMD = f"PYTHONPATH=src {sys.executable} -m pytest -q -p no:cacheprovider tests/test_helpers.py"
FORGE = {"owner": "demo", "repo": "flask", "token": "fake-forgejo-value"}
PULLS = "/api/v1/repos/demo/flask/pulls"


def _fake_flask(tmp_path: Path) -> tuple[Path, str]:
    """A small git repository shaped like the pinned flask, committed by an upstream identity."""
    repo = tmp_path / "upstream-flask"
    files = {
        "src/flask/__init__.py": "",
        "src/flask/helpers.py": ('import os\n\n\ndef get_debug_flag() -> bool:\n'
                                 '    """Get whether debug mode should be enabled for the app."""\n'
                                 '    val = os.environ.get("FLASK_DEBUG")\n' + PINNED_RETURN + "\n"),
        "src/flask/sessions.py": '"""Sessions."""\n\n\nclass SessionInterface:\n    cookie_name = "session"\n',
        "tests/test_helpers.py": ("from flask.helpers import get_debug_flag\n\n\ndef test_get_debug_flag(monkeypatch):\n"
                                  '    monkeypatch.setenv("FLASK_DEBUG", "0")\n    assert get_debug_flag() is False\n'),
        "README.md": "# Flask\n", "LICENSE.txt": "A licence.\n", "uv.lock": "version = 1\n",
        ".pre-commit-config.yaml": "repos: []\n", ".readthedocs.yaml": "version: 2\n",
        ".github/workflows/tests.yaml": "name: Tests\n", ".gitignore": "__pycache__/\n",
        "src/flask/sansio/README.md": "# Sans-IO\n", "docs/web-security.rst": "Security\n", "docs/extensiondev.rst": "Extensions\n",
        "docs/async-await.rst": "Async\n", "docs/cli.rst": "CLI\n", "docs/testing.rst": "Testing\n",
        "docs/contributing.rst": "Contributing\n",
    }
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=Jane Upstream", "-c", "user.email=jane@upstream.example", "commit", "-qm", "upstream work"]):
        cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        assert cp.returncode == 0, cp.stderr
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return repo, sha


def _bare(tmp_path: Path) -> Path:
    """A bare repository: the git side of the forge."""
    bare = tmp_path / "forge.git"
    cp = subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr
    return bare


class _FakeForgejo:
    def __init__(self):
        self.requests: list[tuple[str, str, dict, object]] = []
        self.pulls: list[dict] = []
        self.port = 0
        self.url = ""

    def posts(self, path: str) -> list[tuple[str, str, dict, object]]:
        return [r for r in self.requests if r[0] == "POST" and r[1].split("?")[0] == path]


@pytest.fixture
def fake_forgejo():
    fake = _FakeForgejo()

    class Handler(http.server.BaseHTTPRequestHandler):
        def _answer(self, data) -> None:
            raw = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _serve(self, method: str) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                body = json.loads(raw) if raw else None
            except ValueError:
                body = None
            fake.requests.append((method, self.path, {k.lower(): v for k, v in self.headers.items()}, body))
            path = self.path.split("?")[0]
            if method == "GET" and path == "/api/v1/version":
                self._answer({"version": "11.0.0"})
            elif method == "POST" and path == "/api/v1/user/repos":
                self._answer({"name": (body or {}).get("name")})
            elif method == "POST" and path == PULLS:
                number = len(fake.pulls) + 1
                pull = {"number": number, "html_url": f"{fake.url}/demo/flask/pulls/{number}", "state": "open",
                        "title": (body or {}).get("title", ""), "body": (body or {}).get("body", ""),
                        "head": {"ref": (body or {}).get("head")}, "base": {"ref": (body or {}).get("base")}}
                fake.pulls.append(pull)
                self._answer(pull)
            elif method == "GET" and path == PULLS:
                self._answer(fake.pulls)
            elif method == "GET":
                self._answer([])
            else:
                self._answer({"id": 1, "browser_download_url": f"{fake.url}/attachments/1"})

        def do_GET(self):
            self._serve("GET")

        def do_POST(self):
            self._serve("POST")

        def do_PATCH(self):
            self._serve("PATCH")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    fake.port = server.server_address[1]
    fake.url = f"http://localhost:{fake.port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()


def _verify_module():
    name = "demovideo_verify"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, DEMO / "verify.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _plain_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in ("AL_APP_ROOT", forgejo.TOKEN_VAR)}


def _testbed(tmp_path: Path, forge: dict | None = None) -> dict:
    source, sha = _fake_flask(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    return scenes_testbed.testbed_site(work, source=source, commit=sha, forge=forge)


def _testbed_layer(tmp_path: Path, fake) -> tuple[dict, dict]:
    forge = {**FORGE, "url": fake.url, "git": str(_bare(tmp_path))}
    site = _testbed(tmp_path, forge)
    scenes_testbed.demo_layer(site["work"], forge, test_cmd=FAKE_TEST_CMD, generated=True)
    return site, forge


def _al_run(site: dict, workflow: str, prompt: str) -> tuple[subprocess.CompletedProcess, str]:
    layer = site["layer"]
    before = scenes.runs_of(layer)
    cp = subprocess.run([str(AL), "run", str(layer), workflow, prompt], cwd=site["work"], capture_output=True, text=True,
                        env=_plain_env(), timeout=900)
    return cp, scenes.new_run(layer, before)


def test_stories_keep_rota_as_the_default():
    assert stories.DEFAULT == "rota" and set(stories.STORIES) == {"rota", "testbed"}
    rota = stories.get("rota")
    assert (rota.narration, rota.video, rota.timed, rota.sheet, rota.report, rota.clips) == (
        NARRATION, "al-demo.mp4", "narration.md", "al-demo-contact-sheet.png", "privacy-ocr.json", "clips")
    assert stories.scenes_of(rota) is scenes
    testbed = stories.get("testbed")
    assert (testbed.narration, testbed.video, testbed.timed, testbed.sheet, testbed.report, testbed.clips) == (
        TESTBED_NARRATION, "al-demo-testbed.mp4", "narration-testbed.md", "al-demo-testbed-contact-sheet.png",
        "privacy-ocr-testbed.json", "clips-testbed")
    assert stories.scenes_of(testbed) is scenes_testbed
    make = _make()
    assert [s.id for s in make.script().scenes] == list(scenes.SCENES)
    assert [s.id for s in make.script("testbed").scenes] == list(scenes_testbed.SCENES)
    assert (make.NARRATION, make.VIDEO, make.TIMED) == (NARRATION, "al-demo.mp4", "narration.md")
    with pytest.raises(KeyError, match="rota"):
        stories.get("nope")


def test_make_and_verify_offer_the_story_option(tmp_path, capsys):
    make = _make()
    for verb in ("scene", "assemble"):
        with pytest.raises(SystemExit) as done:
            make.main([verb, "--help"])
        assert done.value.code == 0
        shown = capsys.readouterr().out
        assert "--story" in shown and "{rota,testbed}" in shown, verb
    with pytest.raises(SystemExit) as done:
        _verify_module().main(["--help"])
    assert done.value.code == 0
    assert "--story" in capsys.readouterr().out
    # a rota id asked of the testbed story
    assert make.main(["scene", "04-rota", "--story", "testbed", "--work", str(tmp_path / "work")]) == 2
    refused = capsys.readouterr().err
    assert "no scene" in refused and "story testbed" in refused and "04-pr" in refused
    assert not (tmp_path / "work").exists()
    with pytest.raises(SystemExit) as done:
        make.main(["scene", "01-problem", "--story", "nope", "--work", str(tmp_path / "work")])
    assert done.value.code == 2
    capsys.readouterr()


def test_verify_and_assemble_take_their_file_names_from_the_story(tmp_path, capsys):
    verify = _verify_module()
    assert (verify.VIDEO, verify.TIMED, verify.SHEET, verify.REPORT) == (
        "al-demo.mp4", "narration.md", "al-demo-contact-sheet.png", "privacy-ocr.json")
    testbed = " ".join(verify.verify(tmp_path, story="testbed")["problems"])
    assert "al-demo-testbed.mp4" in testbed
    default = " ".join(verify.verify(tmp_path)["problems"])
    assert "al-demo.mp4" in default and "testbed" not in default
    # nothing recorded yet
    capsys.readouterr()
    assert _make().main(["assemble", "--story", "testbed", "--work", str(tmp_path), "--no-voice"]) == 1
    message = capsys.readouterr().err
    assert "01-problem" in message and "--story testbed" in message
    assert _make().main(["assemble", "--work", str(tmp_path), "--no-voice"]) == 1
    assert "--story" not in capsys.readouterr().err


def test_testbed_narration_has_seven_parts_and_the_right_length():
    script = rules.parse(TESTBED_NARRATION.read_text(encoding="utf-8"))
    assert rules.validate(script) == []
    assert [(p.number, p.title) for p in script.parts] == list(enumerate(rules.TITLES, 1))
    assert [s.id for s in script.scenes] == list(scenes_testbed.SCENES) == TESTBED_IDS
    assert 285 <= script.seconds <= 300, script.seconds
    assert all(s.kind in rules.KINDS and s.captions[0].offset == 0.0 for s in script.scenes)


def test_testbed_narration_spoken_lines_are_private_and_conversational():
    script = rules.parse(TESTBED_NARRATION.read_text(encoding="utf-8"))
    captions = [c for s in script.scenes for c in s.captions]
    assert captions and all(c.say.strip() and len(c.text) <= 64 for c in captions)
    words = sum(len(c.say.split()) for c in captions)
    assert 600 <= words <= 700, words
    spoken = " ".join(c.say for c in captions)
    for caption in captions:
        assert "`" not in caption.say and "/" not in caption.say, caption.say
    assert re.search(r"\bI\b", spoken), "first person"
    assert not re.search(r"\bI'm [A-Z]", spoken) and "my name is" not in spoken.lower(), "the script names no person"
    assert rules.find_private("\n".join(f"{c.text}\n{c.say}" for c in captions), VALUES) == []
    for part in script.parts[:6]:
        said = " ".join(f"{c.text} {c.say}" for s in part.scenes for c in s.captions)
        assert "Flask" in said or "flask" in said or "sessions.py" in said, part.title
    first = {s.id: s.captions[0].text for s in script.scenes}
    assert "stand-in" in first["04-run"]
    assert any("Forgejo" in c.text for c in script.scene("04-pr").captions)
    assert "sessions.py" in first["05-protected"]


def test_testbed_layer_excerpt_shows_checks_protected_paths_and_review():
    cp = subprocess.run(["sh", "-c", scenes_testbed.EXCERPT], cwd=AUTHORED_FLASK, capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stderr
    excerpt = cp.stdout.splitlines()
    assert len(excerpt) == 5 and all(len(line) <= 96 for line in excerpt), excerpt
    for needed in ("(cmd lint", "(cmd typecheck", "(cmd test", "(review auto", "#:require-checks"):
        assert needed in cp.stdout, needed
    shown = subprocess.run(["sh", "-c", scenes_testbed.PROTECTED_SHOWN], cwd=AUTHORED_FLASK, capture_output=True, text=True,
                           timeout=60)
    assert shown.returncode == 0, shown.stderr
    protected = shown.stdout.splitlines()
    assert len(protected) == 2 and all(len(line) <= 96 for line in protected), protected
    assert "#:protected" in protected[0] and "src/flask/sessions.py" in protected[1]
    lines = excerpt + protected
    for cue, keep in scenes_testbed.HIGHLIGHTS.items():
        kept = [line for line in lines if any(s in line for s in keep)]
        assert kept and len(kept) < len(lines), (cue, kept)
    assert rules.find_private(cp.stdout + shown.stdout, VALUES) == []
    # one line of the terminal: its 100 columns less the prompt `~/work $ ` (scenes.shell refuses a longer command)
    assert len(scenes_testbed.EXCERPT) <= 91 and len(scenes_testbed.PROTECTED_SHOWN) <= 91


def test_every_al_command_the_testbed_story_shows_is_accepted():
    shown = [scenes.fill(argv, scenes_testbed.SAMPLES) for argv in scenes_testbed.COMMANDS.values()]
    assert list(scenes_testbed.COMMANDS) == ["init", "adopt", "gen", "check", "run"]
    assert all(argv[0] == "al" for argv in shown) and not [a for argv in shown for a in argv if "{" in a]
    env = _plain_env()
    for argv in shown:
        cp = subprocess.run([str(AL), *argv[1:], "--help"], capture_output=True, text=True, env=env, timeout=120)
        assert cp.returncode == 0, (argv, cp.stdout, cp.stderr)


def test_testbed_site_copies_with_a_neutral_identity(tmp_path):
    source, sha = _fake_flask(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    site = scenes_testbed.testbed_site(work, source=source, commit=sha)
    app = site["app"]
    assert app == work / "flask" and site["layer"] == work / "flask.al" and not site["layer"].exists()

    def git_out(repo: Path, *args: str) -> str:
        cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        assert cp.returncode == 0, cp.stderr
        return cp.stdout

    assert git_out(app, "log", "--format=%an %ae|%cn %ce").splitlines() == [
        "Showcase showcase@example.org|Showcase showcase@example.org"]
    texts = [git_out(app, "log", "--format=%an%ae%cn%ce%B"), (app / ".git" / "config").read_text(),
             "\n".join(str(p.relative_to(work)) for p in work.rglob("*"))]
    for private in ("Jane", "jane@", "upstream.example"):
        assert not [t for t in texts if private in t], private
    assert git_out(app, "ls-files") == git_out(source, "ls-files")
    assert git_out(app, "status", "--porcelain") == ""
    other = tmp_path / "other"
    other.mkdir()
    again = scenes_testbed.testbed_site(other, source=source, commit=sha)
    assert git_out(again["app"], "rev-parse", "HEAD") == git_out(app, "rev-parse", "HEAD") != sha + "\n"
    (app / "marker.txt").write_text("kept")
    assert scenes_testbed.testbed_site(work, source=source, commit=sha)["app"] == app and (app / "marker.txt").is_file()


def test_testbed_init_adopt_gen_check(tmp_path):
    site = _testbed(tmp_path)
    work, env = site["work"], _plain_env()

    def shown(*argv: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(AL), *argv], cwd=work, capture_output=True, text=True, env=env, timeout=600)

    init = shown(*scenes_testbed.COMMANDS["init"][1:])
    assert init.returncode == 0, init.stdout + init.stderr
    llm = (site["layer"] / "LLM.txt").read_text(encoding="utf-8")
    layer = scenes_testbed.install_flask_layer(work)
    assert layer == site["layer"]
    text = (layer / "agentic.rkt").read_text(encoding="utf-8")
    assert '#:root "../flask"' in text and "testbeds/" not in text
    assert (layer / "LLM.txt").read_text(encoding="utf-8") == llm
    adopt = shown(*scenes_testbed.COMMANDS["adopt"][1:])
    assert adopt.returncode == 0 and "nothing to adopt" in adopt.stdout, adopt.stdout + adopt.stderr
    gen = shown(*scenes_testbed.COMMANDS["gen"][1:])
    assert gen.returncode == 0 and "generated " in gen.stdout, gen.stdout + gen.stderr
    check = shown(*scenes_testbed.COMMANDS["check"][1:])
    assert check.returncode == 0 and check.stdout.strip().endswith("check: ok"), check.stdout + check.stderr
    status = subprocess.run(["git", "-C", str(site["app"]), "status", "--porcelain"], capture_output=True, text=True)
    assert status.returncode == 0 and status.stdout == "", "the app is untouched"
    for cp in (init, adopt, gen, check):
        assert rules.find_private(_scrub(cp.stdout, work), VALUES) == [], cp.args


def test_testbed_demo_layer_compiles_with_its_forge(tmp_path):
    site = _testbed(tmp_path)
    forge = {**FORGE, "url": "http://localhost:3999"}
    made = scenes_testbed.demo_layer(site["work"], forge, test_cmd=FAKE_TEST_CMD)
    layer = made["layer"]
    assert layer == site["layer"] and not (layer / "agentic-layer.json").exists()
    text = (layer / "agentic.rkt").read_text(encoding="utf-8")
    assert '(forge forgejo #:host "http://localhost:3999" #:repo "demo/flask"' in text
    assert f'#:token-env "{forgejo.TOKEN_VAR}"' in text and not re.search(r"@[A-Z_]+@", text)
    for needed in ("(adw change ", "(adw tidy ", "(adw strict ", "(review agent #:reviewers 2)", "(agent mock)", "#:agent replay",
                   FAKE_TEST_CMD):
        assert needed in text, needed
    protected = re.compile(r"#:protected \(([^)]*)\)")
    authored = protected.search((AUTHORED_FLASK / "agentic.rkt").read_text(encoding="utf-8")).group(1)
    assert protected.search(text).group(1).split() == authored.split() and '"src/flask/sessions.py"' in authored
    assert [p.relative_to(layer).as_posix() for p in _files_holding(layer, FORGE["token"])] == ["local.env"]
    assert (layer / "local.env").stat().st_mode & 0o777 == 0o600
    assert json.loads((layer / "replay.json").read_text()) == {"test": FAKE_TEST_CMD}
    tidy = json.loads((layer / "mock" / "tidy" / "build.json").read_text())["files"]
    assert tidy.keys() == {"src/flask/sessions.py"}
    assert tidy["src/flask/sessions.py"].startswith((site["app"] / "src" / "flask" / "sessions.py").read_text())
    assert json.loads((layer / "mock" / "strict" / "build.json").read_text())["files"].keys() == {"README.md"}
    for reviewer in ("review-1", "review-2"):       # the step names of the two reviewers
        refused = json.loads(json.loads((layer / "mock" / "strict" / f"{reviewer}.json").read_text())["output"])
        assert refused["success"] is False and refused["issues"][0]["severity"] == "blocker", reviewer
    gen = al("gen", layer)
    assert "generated " in gen.stdout, gen.stdout
    assert al("check", layer).stdout.strip().endswith("check: ok")
    assert scenes_testbed.demo_layer(site["work"], forge, test_cmd=FAKE_TEST_CMD, generated=True)["layer"] == layer
    assert [p.relative_to(layer).as_posix() for p in _files_holding(layer, FORGE["token"])] == ["local.env"]


def test_testbed_replay_agent_makes_the_small_change(tmp_path):
    app = _testbed(tmp_path)["app"]
    config = tmp_path / "replay.json"
    config.write_text(json.dumps({"test": FAKE_TEST_CMD}))
    helpers, tests = app / "src" / "flask" / "helpers.py", app / "tests" / "test_helpers.py"

    def replay() -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(DEMO / "flask_replay.py"), str(app), "0", str(config)], input="the plan",
                              capture_output=True, text=True, timeout=300)

    def suite() -> subprocess.CompletedProcess:
        return subprocess.run(["sh", "-c", FAKE_TEST_CMD], cwd=app, capture_output=True, text=True, timeout=300)

    done = replay()
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr
    events = [json.loads(line) for line in done.stdout.splitlines()]
    assert events[0]["type"] == "system" and events[-1]["type"] == "result" and events[-1]["is_error"] is False
    used = [block["name"] for e in events if e["type"] == "assistant" for block in e["message"]["content"]
            if block["type"] == "tool_use"]
    assert used == ["Grep", "Read", "Edit", "Edit", "Bash"]
    assert '"no", "off"}' in helpers.read_text() and "def test_get_debug_flag_off" in tests.read_text()
    passed = suite()
    assert passed.returncode == 0 and "2 passed" in passed.stdout, passed.stdout + passed.stderr
    helpers.write_text(helpers.read_text().replace('"no", "off"}', '"no"}'))
    failed = suite()
    assert failed.returncode != 0 and "test_get_debug_flag_off" in failed.stdout, "the new test is real"
    # a helpers.py that is not the pinned one: nothing is changed
    helpers.write_text(helpers.read_text().replace('"no"}', '"no", "n"}'))
    before = (helpers.read_text(), tests.read_text())
    refused = replay()
    assert refused.returncode == 1 and "is not the pinned get_debug_flag" in refused.stderr, refused.stderr
    assert (helpers.read_text(), tests.read_text()) == before


def test_testbed_run_opens_a_pull_request_on_a_fake_forgejo(tmp_path, fake_forgejo):
    site, forge = _testbed_layer(tmp_path, fake_forgejo)
    layer = site["layer"]
    done, run = _al_run(site, "/change", scenes_testbed.PROMPT_A)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    state = scenes.run_state(layer, run)
    assert state.get("status") == "passed", state.get("status")
    assert sorted(s["name"] for s in state["steps"]) == sorted(scenes_testbed.STEPS_A)
    posted = fake_forgejo.posts(PULLS)
    assert len(posted) == 1, [r[:2] for r in fake_forgejo.requests]
    _method, _path, headers, body = posted[0]
    assert body["head"] == state["branch"] and body["base"] == "main"
    assert headers["authorization"] == f"token {FORGE['token']}", "read from local.env: the variable is not in the environment"
    assert forgejo.TOKEN_VAR not in _plain_env()
    assert not [r for r in fake_forgejo.requests if r[1].split("?")[0].endswith("/merge")]
    bare = forge["git"]
    names = subprocess.run(["git", "-C", bare, "diff", "--name-only", "main", f"refs/heads/{state['branch']}"],
                           capture_output=True, text=True)
    assert names.returncode == 0, names.stderr
    assert {"src/flask/helpers.py", "tests/test_helpers.py"} <= set(names.stdout.split())
    assert scenes_testbed.pull_request_url(layer, run) == fake_forgejo.pulls[0]["html_url"]
    assert _files_holding(layer / "agents", FORGE["token"]) == []
    with pytest.raises(scenes.SceneStopped):
        scenes_testbed.pull_request_url(layer, "00000000")


def test_testbed_protected_run_waits_for_approve_and_resume(tmp_path, fake_forgejo):
    site, _forge = _testbed_layer(tmp_path, fake_forgejo)
    layer = site["layer"]
    stopped, run = _al_run(site, "/tidy", scenes_testbed.PROMPT_B)
    assert stopped.returncode == 1, stopped.stdout[-3000:] + stopped.stderr[-3000:]
    assert "evidence:needs-person" in json.dumps(scenes.run_state(layer, run))
    # a stopped run is handed over on the forge (ship.handoff): a pull request marked for a person, not the delivery one
    waiting = [r[3]["title"] for r in fake_forgejo.posts(PULLS)]
    assert len(waiting) == 1 and waiting[0].startswith("WIP: [needs human]"), waiting
    assert scenes.run_state(layer, run)["ship"]["action"] == "handoff"
    approved = subprocess.run([str(AL), "approve", str(layer), run, "--all", "--who", scenes.WHO], cwd=site["work"],
                              capture_output=True, text=True, env=_plain_env(), timeout=300)
    assert approved.returncode == 0 and "resuming from step evidence" in approved.stdout, approved.stdout + approved.stderr
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline and scenes.run_state(layer, run).get("status") != "passed":
        time.sleep(0.5)
    state = scenes.run_state(layer, run)
    assert state.get("status") == "passed", state.get("status")
    approvals = json.dumps(state.get("approvals"))
    assert scenes_testbed.APPROVAL in approvals and scenes.WHO in approvals, approvals
    posted = fake_forgejo.posts(PULLS)
    assert [r[3]["head"] for r in posted] == [state["branch"]] * 2, "only after the approval: the delivery pull request"
    assert "needs human" not in posted[1][3]["title"] and state["ship"]["action"] == "pr"
    assert scenes_testbed.pull_request_url(layer, run) == fake_forgejo.pulls[1]["html_url"]
    assert not [r for r in fake_forgejo.requests if r[1].split("?")[0].endswith("/merge")]


def test_testbed_reviewers_refuse_and_hand_over(tmp_path, fake_forgejo):
    site, _forge = _testbed_layer(tmp_path, fake_forgejo)
    layer = site["layer"]
    done, run = _al_run(site, "/strict", scenes_testbed.PROMPT_C)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    state = scenes.run_state(layer, run)
    assert state.get("status") == "handed-over", state.get("status")
    assert "review-rejected" in json.dumps(state["ship"].get("handed_over")), state["ship"]
    verdict = state["review"]["verdict"]
    assert verdict["success"] is False and "has no test" in json.dumps(verdict)
    assert not [r for r in fake_forgejo.requests if r[1].split("?")[0].endswith("/merge")]


def test_forgejo_up_starts_a_loopback_instance_with_a_throwaway_admin(tmp_path, fake_forgejo, capsys):
    value = "0123abcd-fake-access-value"
    work = tmp_path / "work"

    def podman(calls: list, failing: str = ""):
        def run(argv, **_kwargs):
            argv = [str(a) for a in argv]
            calls.append(argv)
            code, out = 0, ""
            if argv[1:3] == ["image", "exists"]:
                code = 1
            elif "generate-access-token" in argv:
                out = f"a line before\n{value}\n"
            elif failing and failing in argv:
                code = 1
            return subprocess.CompletedProcess(argv, code, out, "refused" if code else "")
        return run

    def found(_name):
        return "/usr/bin/podman"

    calls: list[list[str]] = []
    assert forgejo.up(work, port=fake_forgejo.port, run=podman(calls), which=found) == 0
    port = fake_forgejo.port
    assert all(argv[0] == "podman" for argv in calls)
    assert ["podman", "pull", "-q", forgejo.IMAGE] in calls or ["podman", "pull", forgejo.IMAGE] in calls
    started = next(argv for argv in calls if argv[1] == "run")
    assert started[1:8] == ["run", "-d", "--name", "al-demo-forgejo", "-p", f"127.0.0.1:{port}:3000", "-e"]
    assert f"FORGEJO__server__ROOT_URL=http://localhost:{port}/" in started and "FORGEJO__server__OFFLINE_MODE=true" in started
    assert started[-1] == forgejo.IMAGE == "codeberg.org/forgejo/forgejo:11" and "-v" not in started
    created = next(argv for argv in calls if "create" in argv and "user" in argv)
    assert created[:5] == ["podman", "exec", "-u", "git", "al-demo-forgejo"]
    assert "--must-change-password=false" in created and "demo@example.org" in created and "--admin" in created
    password = created[created.index("--password") + 1]
    assert password != "fake-Passw0rd-1!" and len(password) >= 20
    posted = fake_forgejo.posts("/api/v1/user/repos")
    assert len(posted) == 1
    assert posted[0][3]["name"] == "flask" and posted[0][3]["private"] is False and posted[0][3]["auto_init"] is False
    assert posted[0][2]["authorization"] == f"token {value}"
    state = json.loads((work / "forgejo.json").read_text())
    assert state["url"] == f"http://localhost:{port}" and (state["owner"], state["repo"]) == ("demo", "flask")
    assert value not in (work / "forgejo.json").read_text()
    assert (work / "forgejo.env").stat().st_mode & 0o777 == 0o600
    assert (work / "forgejo.env").read_text() == f"{forgejo.TOKEN_VAR}={value}\n"
    printed = capsys.readouterr()
    assert f"http://localhost:{port}/demo/flask" in printed.out
    assert value not in printed.out + printed.err and password not in printed.out + printed.err
    assert forgejo.status(work) == {"url": f"http://localhost:{port}", "owner": "demo", "repo": "flask", "token": value}
    assert forgejo.reachable(fake_forgejo.url)
    assert scenes_testbed.require_forge(work)["token"] == value
    # make.py clean refuses while the instance is recorded
    assert _make().main(["clean", "--work", str(work)]) == 2
    assert "forgejo down" in capsys.readouterr().err and work.is_dir()
    calls.clear()
    assert forgejo.down(work, run=podman(calls), which=found) == 0
    assert calls == [["podman", "rm", "-f", "-v", "al-demo-forgejo"]]
    assert not (work / "forgejo.json").exists() and not (work / "forgejo.env").exists() and forgejo.status(work) is None
    assert "removed the Forgejo container and its data" in capsys.readouterr().out
    assert forgejo.down(work, run=podman(calls), which=found) == 0, "nothing running is not an error"
    # the admin cannot be created: the container is removed and no state is left
    calls.clear()
    other = tmp_path / "other"
    assert forgejo.up(other, port=port, run=podman(calls, failing="create"), which=found) == 1
    assert calls[-1] == ["podman", "rm", "-f", "-v", "al-demo-forgejo"] and "create" in calls[-2]
    assert not (other / "forgejo.json").exists() and not (other / "forgejo.env").exists()
    assert "make.py forgejo" in capsys.readouterr().err


def test_forgejo_helper_refuses_without_podman_and_the_story_stops_without_an_instance(tmp_path, capsys):
    assert forgejo.up(tmp_path / "work", which=lambda _name: None, run=None) == 2
    assert "podman" in capsys.readouterr().err and not (tmp_path / "work" / "forgejo.json").exists()
    with pytest.raises(scenes.SceneStopped, match="make.py forgejo up"):
        scenes_testbed.require_forge(tmp_path)
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    (tmp_path / "forgejo.json").write_text(json.dumps({"url": f"http://localhost:{closed}", "port": closed, "owner": "demo",
                                                       "repo": "flask", "container": "al-demo-forgejo", "image": forgejo.IMAGE}))
    (tmp_path / "forgejo.env").write_text(f"{forgejo.TOKEN_VAR}=fake-forgejo-value\n")
    assert forgejo.status(tmp_path)["token"] == "fake-forgejo-value" and not forgejo.reachable(f"http://localhost:{closed}")
    with pytest.raises(scenes.SceneStopped, match="make.py forgejo up") as stopped:
        scenes_testbed.require_forge(tmp_path)
    assert "never fakes" in str(stopped.value)
    with pytest.raises(SystemExit) as done:
        _make().main(["forgejo", "--help"])
    assert done.value.code == 0
    shown = capsys.readouterr().out
    assert "up" in shown and "down" in shown and "--port" in shown


def test_testbed_scenes_reuse_the_shared_helpers():
    assert scenes_testbed.Ctx is scenes.Ctx and scenes_testbed.Browser is scenes.Browser
    assert scenes_testbed.card_part is scenes.card_part and scenes_testbed.SceneStopped is scenes.SceneStopped
    assert scenes_testbed.SCENES["06-fit"] is scenes.scene_06_fit and scenes_testbed.SCENES["07-close"] is scenes.scene_07_close
    text = (DEMO / "scenes_testbed.py").read_text(encoding="utf-8")
    for copied in ("def caption_png", "def card_image", "BLANKER =", "class Browser"):
        assert copied not in text, copied
    browser = (scenes_testbed.scene_04_run, scenes_testbed.scene_04_pr, scenes_testbed.scene_05_proof,
               scenes_testbed.scene_05_protected, scenes_testbed.scene_05_review_no)
    for fn in browser:
        assert ".spot(" in inspect.getsource(fn), fn.__name__
    for fn in (scenes_testbed.scene_04_run, scenes_testbed.scene_04_pr, scenes_testbed.scene_05_protected,
               scenes_testbed.scene_05_review_no):
        assert "require_forge" in inspect.getsource(fn), fn.__name__
    assert "page.goto" in inspect.getsource(scenes_testbed.scene_04_pr)
    protected = inspect.getsource(scenes_testbed.scene_05_protected)
    assert "button.approve-resume" in protected and "APPROVAL" in protected
    assert "highlight_frame" in inspect.getsource(scenes_testbed.scene_02_layer)
    assert list(scenes.SCENES) == ARC, "the rota story is as it was"


def test_showcase_readme_documents_the_testbed_story_and_forgejo():
    text = (ROOT / "docs" / "showcase" / "README.md").read_text(encoding="utf-8")
    for needed in ("--story testbed", "narration-testbed.md", "make.py forgejo up", "make.py forgejo down", "al-demo-testbed.mp4",
                   "src/flask/sessions.py", "codeberg.org/forgejo/forgejo:11", "localhost"):
        assert needed in text, needed
    heading = "### The second story: flask on a local Forgejo"
    assert text.count(heading) == 1
    section = text[text.index(heading):]
    section = section[:section.index("\n## ")]
    assert "localhost" in section and not rules.PATTERNS["ipv4"].search(section), "localhost, never an address"


def test_recorded_testbed_demo_verifies():
    folder = Path(os.environ.get("AL_DEMO_VIDEO_DIR") or ROOT / ".al-proof" / "demo")
    if not (folder / "al-demo-testbed.mp4").is_file():
        pytest.skip("no recorded testbed demo in .al-proof/demo: record it with tools/demovideo/make.py --story testbed")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            pytest.skip(f"no {tool} on PATH: the recorded demo is not verified")
    env = {k: v for k, v in os.environ.items() if k != "AL_APP_ROOT"}
    cp = subprocess.run(["uv", "run", str(DEMO / "verify.py"), str(folder), "--story", "testbed", "--json"], capture_output=True,
                        text=True, env=env, timeout=1500)
    assert cp.stdout.strip().startswith("{"), cp.stdout + cp.stderr
    report = json.loads(cp.stdout)
    assert cp.returncode == 0 and report["ok"] and report["story"] == "testbed", report.get("problems") or report
