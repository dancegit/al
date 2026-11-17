"""What must hold before this repository is shown in public, checked on the tracked tree without
running anything heavy: a licence the README names; a CI workflow that is least-privilege, pinned and
runs only documented commands; no video in the tree, every video link an asset of the release named
in docs/media.json; no quoted course wording in docs/DESIGN.md; the flask run described as its saved
log shows it; the "proven" box under the summary and the Numbers section; a quick-start rehearsal
that follows the README; throwaway credentials that say they are fake, with an allow-list that gives
a reason per entry; and one spelling of PITER.

    uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools/test_publish_ready.py -q
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from conftest import ROOT, SANDBOX_GIT, git
from test_links import CODE, FENCE, documents, resolve, targets

sys.path.insert(0, str(ROOT / "tools"))
import media  # noqa: E402  (tools/media.py)

# Measured when the videos left the tree: 58.8 MB remain (GIFs 36.7 MB in 41 files, PNGs 13.1 MB in
# 57 files, everything else 8.9 MB). The request's target is 30 MB; it cannot be met while every GIF
# and poster is kept (re-encoding all GIFs at 3 fps, 560 px and 32 colours still leaves about 35 MB,
# with clearly worse pictures), so the limit here is what the tree can keep. Lower it when a person
# has decided what happens to the larger GIFs. The size is the sum of the index blobs, so a file
# deleted on disk but still tracked counts.
TREE_LIMIT = 60_000_000

LIVE_SCRIPTS = ("tests/forge/forgejo_live.sh", "tests/forge/proof_video_live.sh", "tests/rotademo/rotademo_live.sh",
                "tests/selfdemo/selfdemo_live.sh", "tests/webdemo/webdemo_live.sh")
FLASK_LOG = "docs/demo/logs/flask-zte-opus-inside-container-d8c03973.log"
QUICKSTART = "## Try it on your own repository"
BINARY = (".png", ".gif", ".jpg", ".jpeg", ".ico", ".woff", ".woff2", ".ttf", ".pdf", ".zip", ".gz", ".b64", ".wav", ".mp3",
          ".ogg", ".zo", ".dep", ".pyc")
URL = re.compile(r"https?://[^\s)>\"'`\]]+")
QUOTED = re.compile(r"\"([^\"\n]+)\"|“([^”\n]+)”")
COURSE = re.compile(r"Tactical Agentic Coding|the course|the lessons|lesson", re.I)
LESSONS = "## What the lessons prescribe, and where it lands"


def _tracked() -> list[str]:
    return git(ROOT, "ls-files").splitlines()


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _manifest() -> dict:
    return json.loads(_read("docs/media.json"))


def _section(text: str, heading: str) -> str:
    """The text of a `## ` section, heading line included, up to the next `## ` or the end."""
    start = text.index(heading + "\n")
    end = text.find("\n## ", start + 1)
    return text[start:] if end < 0 else text[start:end]


def _is_video(path: str) -> bool:
    return path.split("#", 1)[0].split("?", 1)[0].lower().endswith(media.VIDEO_SUFFIXES)


# ---------------------------------------------------------------------------------------- licence

def test_licence_exists_and_readme_names_it() -> None:
    assert "LICENSE" in _tracked(), "LICENSE must be tracked (git add it)"
    text = _read("LICENSE")
    assert "All rights reserved" in text
    assert re.search(r"^Copyright \(c\) 20\d\d dancegit$", text, re.M)
    assert "written permission" in text and "issue" in text and "@" not in text
    readme = _read("README.md")
    last = readme[readme.rindex("\n## "):]
    assert "LICENSE" in targets(last), "the README's last section links LICENSE"
    assert "All rights reserved" in last


# --------------------------------------------------------------------------- continuous integration

def _example_test_lines() -> set[str]:
    section = _section(_read("docs/EXAMPLES.md"), "## Tests")
    blocks = re.findall(r"^```bash[ \t]*\n(.*?)^```[ \t]*$", section, re.M | re.S)
    return {line.strip() for block in blocks for line in block.splitlines() if line.strip()}


FAST_COMMANDS = (
    'PLTCOLLECTS="$PWD/racket:" raco test racket/al/tests',
    "(cd kit/runtime && uv run --with pytest pytest -q)",
    "(cd kit/dashboard && uv run --with pytest pytest -q tests)",
    'uv run --with pytest --with pytest-xdist --with pyyaml --with jsonschema pytest tests/tools -q -n 4 -m "not podman and not browser and not slow"',
    "uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools/test_readme.py "
    "tests/tools/test_generated_references.py tests/tools/test_publish_ready.py -q",
)


def test_ci_workflow_is_pinned_least_privilege_and_documented() -> None:
    yaml = pytest.importorskip("yaml")
    raw = _read(".github/workflows/ci.yml")
    assert ".github/workflows/ci.yml" in _tracked()
    workflow = yaml.safe_load(raw)
    triggers = workflow[True] if True in workflow else workflow["on"]      # PyYAML reads the key `on` as True
    assert sorted(triggers) == ["pull_request", "push"], triggers
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets." not in raw and "pull_request_target" not in raw
    documented = _example_test_lines()
    ran = []
    for name, job in workflow["jobs"].items():
        assert "permissions" not in job or job["permissions"] in ({}, {"contents": "read"}), f"{name} widens the permissions"
        assert isinstance(job.get("timeout-minutes"), int) and job["timeout-minutes"] <= 30, f"{name}: timeout-minutes"
        for step in job["steps"]:
            if "uses" in step:
                assert re.match(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$", step["uses"]), f"{name}: not pinned to a commit: {step['uses']}"
                if step["uses"].startswith("actions/checkout@"):
                    assert step.get("with", {}).get("persist-credentials") is False, f"{name}: checkout keeps credentials"
            assert "env" not in step, f"{name}: a step sets env"
            for line in str(step.get("run", "")).splitlines():
                if line.strip():
                    assert line.strip() in documented, f"{name}: not in docs/EXAMPLES.md#tests: {line.strip()}"
                    ran.append(line.strip())
    for command in FAST_COMMANDS:
        assert command in documented, f"docs/EXAMPLES.md#tests does not list: {command}"
        assert command in ran, f"the workflow does not run: {command}"
    assert sorted(ran) == sorted(FAST_COMMANDS), "the workflow runs exactly the five fast commands"


def test_readme_has_the_ci_badge() -> None:
    repository = _manifest()["repository"]
    head = "\n".join(_read("README.md").splitlines()[:4])
    badge = f"https://github.com/{repository}/actions/workflows/ci.yml/badge.svg"
    assert re.search(r"!\[[^\]]*\]\(" + re.escape(badge) + r"\)", head), f"no badge {badge} in the first 4 lines"


# ------------------------------------------------------------------------------------------ media

def test_no_tracked_file_is_a_video() -> None:
    videos = [p for p in _tracked() if _is_video(p)]
    assert not videos, "videos are release assets (docs/media.json), not tracked files:\n  " + "\n  ".join(videos)


def test_every_video_link_is_in_the_manifest() -> None:
    manifest = _manifest()
    base, repository = manifest["base"], manifest["repository"]
    assets = {base + f["name"] for f in manifest["files"]}
    tracked = _tracked()
    docs = documents(set(tracked))
    wrong, linked = [], 0
    for document in docs:
        text = _read(document)
        for target in targets(text):                       # relative links and images
            if _is_video(target):
                wrong.append(f"{document}: relative link to a video: {target}")
        for url in URL.findall(text):
            url = url.rstrip(".,;:")
            if _is_video(url):
                linked += 1
                if url not in assets:
                    wrong.append(f"{document}: {url} is not base + name of docs/media.json")
    assert linked >= 15, f"only {linked} video links found; the documents link the release's videos"
    marker = repository + "/releases/download/"
    for path in tracked:
        if path == "docs/media.json" or path.lower().endswith(BINARY):
            continue
        try:
            text = _read(path)
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        if marker not in text:
            continue
        if path not in docs:
            wrong.append(f"{path}: names the release; only docs/media.json and the documents' links may")
            continue
        for match in re.finditer(r"\S*" + re.escape(marker) + r"\S*", text):
            if base not in match.group(0):
                wrong.append(f"{path}: {match.group(0)} does not start with the manifest's base")
    readme = _read("README.md")
    tour = re.findall(r"^\[tour\]:\s*(\S+)\s*$", readme, re.M)
    assert tour == [manifest["tour"]["placeholder"]], f"the README's [tour] definition is the manifest's placeholder: {tour}"
    assert readme.count("][tour]") >= 2 and "tour.mp4" not in readme
    assert not wrong, "\n  ".join(["video links:"] + wrong)


def test_manifest_entries_are_complete() -> None:
    manifest = _manifest()
    files = manifest["files"]
    assert manifest["base"] == f"https://github.com/{manifest['repository']}/releases/download/{manifest['release']}/"
    assert len(files) == 35 and [f["name"] for f in files] == sorted(f["name"] for f in files)
    assert len({f["name"] for f in files}) == len(files)
    for entry in files:
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]), entry["name"]
        assert entry["bytes"] > 0 and entry["name"] == media.asset_name(entry["was"]), entry["name"]
    assert manifest["tour"]["asset"] in {f["name"] for f in files}
    assert media.asset_name("docs/demo/dashboard/live.mp4") == "demo-dashboard-live.mp4"
    assert media.url("demo-tour.mp4") == manifest["base"] + "demo-tour.mp4"


def test_tracked_tree_is_under_the_size_limit() -> None:
    entries = []
    for line in git(ROOT, "ls-files", "-s").splitlines():          # <mode> <object> <stage>\t<path>
        meta, path = line.split("\t", 1)
        mode, obj, _stage = meta.split()
        if mode != "160000":                                        # a submodule has no blob
            entries.append((obj, path))
    missing = [path for _obj, path in entries if not (ROOT / path).is_file() and not (ROOT / path).is_symlink()]
    assert not missing, "tracked but not on disk (a deletion that is not in the index):\n  " + "\n  ".join(missing)
    objects = [obj for obj, _path in entries]
    # inside a run the newest blobs are in the run's own object store, which conftest.py hides from the tests
    out = subprocess.run(["git", "-C", str(ROOT), "cat-file", "--batch-check=%(objectsize)"], input="\n".join(objects) + "\n",
                         capture_output=True, text=True, check=True, env={**os.environ, **SANDBOX_GIT})
    sizes = out.stdout.split()
    assert len(sizes) == len(objects) and all(n.isdigit() for n in sizes), f"{len(objects)} index entries, sizes: {out.stdout[:300]}"
    total = sum(int(n) for n in sizes)
    assert total < TREE_LIMIT, f"the tracked tree is {total / 1e6:.1f} MB; the limit is {TREE_LIMIT / 1e6:.0f} MB"


VIDEO_IN_TREE = re.compile(r"(\$OUT|\$\{OUT\}|\$ROOT/docs|docs/(demo|showcase))[^\s\"']*\.(mp4|webm|mov|mkv|avi|m4v|ogv)\b", re.I)


def test_live_scripts_write_no_video_into_the_tree() -> None:
    wrong = [f"{path}:{number}: {line.strip()[:100]}" for path in LIVE_SCRIPTS
             for number, line in enumerate(_read(path).splitlines(), 1) if VIDEO_IN_TREE.search(line)]
    assert not wrong, "a live script names a video under its output folder or docs/:\n  " + "\n  ".join(wrong)
    assert VIDEO_IN_TREE.search('cp "$WORK/b.webm" "$OUT/b-feature.webm"') and VIDEO_IN_TREE.search("ffmpeg -i x $ROOT/docs/demo/a.MP4")
    assert not VIDEO_IN_TREE.search('cp "$WORK/b.webm" "$MEDIA/demo-webdemo-b-feature.webm"')
    release = _manifest()["release"]
    folder = "${AL_MEDIA_DIR:-$ROOT/media/" + release + "}"
    for path in ("tests/webdemo/webdemo_live.sh", "tests/rotademo/rotademo_live.sh"):
        assert f'MEDIA="{folder}"' in _read(path), f"{path}: MEDIA is the ignored media folder of the release"
    assert folder + "/demo-webdemo-b-feature.webm" in _read("tests/forge/proof_video_live.sh")


def test_release_media_lists_and_uploads_nothing(tmp_path: Path) -> None:
    manifest = _manifest()
    files = manifest["files"]
    folder, fake = tmp_path / "media", tmp_path / "bin"
    folder.mkdir()
    fake.mkdir()
    marker = tmp_path / "gh-was-called"
    (fake / "gh").write_text(f"#!/bin/sh\necho called > '{marker}'\n")
    (fake / "gh").chmod(0o755)
    right, wrong = files[0], files[1]
    (folder / wrong["name"]).write_bytes(b"not the recorded file")
    # a file whose sha256 is the recorded one cannot be made up, so the manifest the script reads is a
    # copy whose first entry describes a file written here
    content = b"a stand-in for a video"
    (folder / right["name"]).write_bytes(content)
    import hashlib
    copy = dict(manifest, files=[dict(right, sha256=hashlib.sha256(content).hexdigest(), bytes=len(content)), *files[1:]])
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "tools").mkdir()
    (root / "docs" / "media.json").write_text(json.dumps(copy))
    (root / "tools" / "release_media.sh").write_bytes((ROOT / "tools" / "release_media.sh").read_bytes())
    (root / "tools" / "release_media.sh").chmod(0o755)
    env = dict(os.environ, PATH=f"{fake}{os.pathsep}{os.environ['PATH']}", AL_MEDIA_DIR=str(folder))
    done = subprocess.run([str(root / "tools" / "release_media.sh")], capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not marker.exists(), "release_media.sh called gh"
    out = done.stdout
    states = {line.split()[0]: line for line in out.splitlines() if line.startswith("  ") and " bytes " in line}
    assert set(states) == {f["name"] for f in files}
    assert states[right["name"]].endswith("  present") and states[wrong["name"]].endswith("  checksum differs")
    assert all(states[f["name"]].endswith("  missing") for f in files[2:])
    assert f"gh release create {manifest['release']} --repo {manifest['repository']}" in out
    upload = next(line for line in out.splitlines() if line.strip().startswith(f"gh release upload {manifest['release']} "))
    assert right["name"] in upload and wrong["name"] not in upload and files[2]["name"] not in upload
    assert manifest["tour"]["placeholder"] in out
    # and on the real manifest, wherever the media folder is: exit 0, every name, gh never called
    real = subprocess.run([str(ROOT / "tools" / "release_media.sh")], capture_output=True, text=True,
                          env=dict(env, AL_MEDIA_DIR=str(tmp_path / "nothing-here")), timeout=120)
    assert real.returncode == 0 and all(f["name"] in real.stdout for f in files) and not marker.exists()
    assert "gh release create media-v1" in real.stdout and "gh release upload media-v1" in real.stdout


# ------------------------------------------------------------------------- wording of docs/DESIGN.md

def _quoted_course_spans(text: str) -> list[str]:
    """Quoted spans of two words or more, outside code, that sit in a paragraph naming the course or
    in the first column of the table of the section about what the lessons prescribe."""
    plain = CODE.sub("", FENCE.sub("", text))
    found = []

    def spans(piece: str) -> list[str]:
        return [a or b for a, b in QUOTED.findall(piece) if len((a or b).split()) >= 2]

    for paragraph in re.split(r"\n[ \t]*\n", plain):
        if COURSE.search(paragraph) and not paragraph.lstrip().startswith("|"):
            found += spans(paragraph)
    if LESSONS in plain:
        for line in _section(plain, LESSONS).splitlines():
            if line.startswith("|") and line.count("|") >= 3:
                found += spans(line.split("|")[1])
    return found


def test_design_has_no_quoted_sentence_attributed_to_the_course() -> None:
    text = _read("docs/DESIGN.md")
    assert not _quoted_course_spans(text), "quoted wording attributed to the course: " + "; ".join(_quoted_course_spans(text))
    for kept in ("Tactical Agentic Coding", "agentic layer", "PITER", "ZTE", LESSONS):
        assert kept in text, f"docs/DESIGN.md no longer names {kept}"


def test_the_quote_checker_finds_what_it_must() -> None:
    sample = ('The course says a thing is "two words" and `"in code"` stays.\n\n'
              'Our own rule: "anything not listed does not exist".\n\n'
              "```\nthe course: \"fenced words here\"\n```\n\n"
              f"{LESSONS}\n\n| Concept | In al |\n|---|---|\n"
              '| A “curly quoted” idea | the right column may say "what it likes" |\n'
              '| One "word" only | x |\n')
    assert _quoted_course_spans(sample) == ["two words", "curly quoted"]


# ------------------------------------------------------------------------------ the flask sentence

def test_flask_run_is_not_described_as_merged() -> None:
    log = _read(FLASK_LOG)
    assert "(shipped=False)" in log and "al/ship/main" in log, "the saved log is the evidence the sentences are compared with"
    wrong = []
    for document in ("README.md", "docs/EXAMPLES.md", "docs/CASE-STUDY.md"):
        text = _read(document)
        pieces = re.split(r"\n[ \t]*\n", text) + [line for line in text.splitlines() if line.startswith("| flask |")]
        about_flask = [p for p in pieces if "flask-zte-opus" in p or p.startswith("| flask |")]
        assert about_flask, f"{document} no longer names the flask run"
        for piece in about_flask:
            for phrase in ("gate merged", "was merged", "shipped,"):
                if phrase in piece:
                    wrong.append(f"{document}: “{phrase}” in: {piece[:90]}…")
    assert not wrong, "the flask run waited on a branch for a manual fast-forward:\n  " + "\n  ".join(wrong)
    readme_flask = next(p for p in re.split(r"\n[ \t]*\n", _read("README.md")) if "flask-zte-opus" in p and "zero-touch" in p)
    assert "al/ship/main" in readme_flask and "fast-forward" in readme_flask


# ------------------------------------------------------------------------- the box and the numbers

def test_proven_box_sits_under_the_summary_with_evidence() -> None:
    readme = _read("README.md")
    top = readme[:readme.index("\n## Why it exists\n")]
    box = "\n".join(line for line in top.splitlines() if line.startswith(">"))
    assert "**Proven**" in box and "**Not yet shown**" in box and len(box.splitlines()) <= 8
    assert top.index("> **Proven**") < top.index("[tour]"), "the box comes before the tour line"
    tracked = set(_tracked())
    directories = {"/".join(p.split("/")[:n]) for p in tracked for n in range(1, p.count("/") + 1)}
    links = [resolve("README.md", t) for t in targets(box)]
    resolved = [p for p in links if p and (p in tracked or p.rstrip("/") in directories)]
    assert len(resolved) >= 3 and len(resolved) == len([p for p in links if p]), links
    assert "](#status)" in box
    status = _section(readme, "## Status")
    assert "**Shown to work**" in status and "**Not yet shown**" in status


def test_readme_numbers_section_links_the_generated_file() -> None:
    readme = _read("README.md")
    headings = re.findall(r"^## (.+)$", readme, re.M)
    at = headings.index("Numbers")
    assert (headings[at - 1], headings[at + 1]) == ("How it stays safe", "Status")
    section = _section(readme, "## Numbers")
    assert "docs/KPIS.md" in targets(section) and "docs/KPIS.md" in _tracked()
    assert "`tools/al kpis ~/code/myapp.al`" in section and "sample" in section
    assert re.search(r'sub\.add_parser\("kpis"', _read("tools/al"))
    assert "· kpis" in _section(readme, "## Repository map")


# ---------------------------------------------------------------------------------- the quick start

def test_quickstart_script_follows_the_readme() -> None:
    script = ROOT / "tests" / "quickstart" / "run.sh"
    assert script.stat().st_mode & stat.S_IXUSR, "tests/quickstart/run.sh is executable"
    for path in ("tests/quickstart/run.sh", "tests/quickstart/Containerfile", "tests/quickstart/layer.rkt"):
        assert path in _tracked(), f"{path} must be tracked"
    text = script.read_text(encoding="utf-8")
    section = _section(_read("README.md"), QUICKSTART)
    assert f'SECTION="{QUICKSTART}"' in text and 'README="$ROOT/README.md"' in text, "run.sh reads its commands from the README"
    blocks = re.findall(r"^```bash[ \t]*\n(.*?)^```[ \t]*$", section, re.M | re.S)
    shown = [line.split("#", 1)[0].strip() for block in blocks for line in block.splitlines() if line.strip()]
    assert len(shown) >= 10
    for line in shown:
        if line.startswith("tools/al "):
            sub = line.split()[1]
            assert re.search(rf'"tools/al {sub} "\*', text) or f"tools/al {sub}" in text, f"run.sh does not know `tools/al {sub}`"
        elif line.startswith("uv run adws/"):
            assert '"uv run adws/"*' in text
        else:
            assert line.startswith("cd "), f"a quick-start line run.sh has no case for: {line}"
    for sub in re.findall(r"`tools/al ([a-z-]+)", FENCE.sub("", section)):         # commands shown inline
        assert f'"tools/al {sub}"' in text, f"run.sh neither runs nor lists `tools/al {sub}`"
    assert "not covered: needs a real agent login" in text and "not covered: needs podman inside the container" in text
    assert "no container engine (podman or docker) found; not run" in text and "exit 3" in text
    assert "FAILED at STEP" in text and "tests/quickstart/layer.rkt" in text
    container = _read("tests/quickstart/Containerfile")
    installs = " ".join(re.findall(r"apt-get install[^\n&;]*", container))
    for package in ("podman", "docker", "python3", "nodejs"):
        assert package not in installs.split(), f"the clean machine installs {package}"
    assert {"racket", "git", "ca-certificates", "curl"} <= set(installs.split())
    pins = json.loads(_read("kit/host/pins.json"))["uv"]
    assert f"ARG UV_VERSION={pins['version']}\n" in container
    assert f"ARG UV_SHA256_X86_64={pins['assets']['x86_64']['sha256']}\n" in container
    assert f"ARG UV_SHA256_AARCH64={pins['assets']['aarch64']['sha256']}\n" in container
    assert "sha256sum -c" in container and re.search(r"^USER (?!root)\w+$", container, re.M)
    assert "tests/quickstart/run.sh" in targets(section) and "need a real agent login" in section
    assert "tests/quickstart/*.log" in _read(".gitignore").splitlines()


# ------------------------------------------------------------------------------- fake credentials

def test_fake_credentials_are_marked() -> None:
    for path in LIVE_SCRIPTS:
        lines = _read(path).splitlines()
        text = "\n".join(lines)
        assert "adw-Passw0rd!" not in text and "s3cret-" not in text, path
        creating = [i for i, line in enumerate(lines) if "admin user create" in line]
        assert creating, f"{path} no longer creates its throwaway user"
        for i in creating:
            assert "fake-" in lines[i], f"{path}:{i + 1}: the password does not say it is fake"
            comment = lines[i - 1]
            assert comment.lstrip().startswith("#") and "throwaway" in comment and "fake" in comment, f"{path}:{i + 1}"
    assert 'AL_TEST_HOOK_SECRET="fake-hook-secret-$RANDOM"' in _read("tests/forge/forgejo_live.sh")
    folder = "kit/runtime/tests/data/usage/"
    names = [p[len(folder):] for p in _tracked() if p.startswith(folder) and not p.endswith("/README")]
    readme = _read(folder + "README")
    assert len(names) >= 5 and all(name in readme for name in names), "the usage data README names every file"
    assert "no key" in readme
    config = tomllib.loads(_read(".gitleaks.toml"))
    assert ".gitleaks.toml" in _tracked() and config["extend"]["useDefault"] is True
    entries = config["allowlists"]
    assert len(entries) >= 6
    for entry in entries:
        assert len(str(entry.get("description", "")).strip()) >= 20, f"an allow-list entry without a reason: {entry}"
        assert entry.get("paths") or entry.get("regexes"), f"an allow-list entry that allows nothing: {entry}"
        for pattern in [*entry.get("paths", []), *entry.get("regexes", [])]:
            re.compile(pattern)


# ----------------------------------------------------------------------------------- one spelling

VARIANTS = re.compile(r"\b(PETER|PTER|PITR|PITRE|PTRE|PITTER|P\.I\.T\.E\.R\.?|P-I-T-E-R|P/T/E/R)(?![\w])")
CASE_VARIANTS = re.compile(r"\b(Piter|PiTER)\b")


def test_the_workflow_letters_are_written_piter() -> None:
    wrong = []
    for path in _tracked():
        if path.lower().endswith(BINARY) or path == "tests/tools/test_publish_ready.py":
            continue
        try:
            text = _read(path)
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if VARIANTS.search(line) or CASE_VARIANTS.search(line):
                wrong.append(f"{path}:{number}: {line.strip()[:80]}")
    assert not wrong, "the four choices are written PITER:\n  " + "\n  ".join(wrong)
    for path in ("README.md", "docs/DESIGN.md", "racket/al/private/vocab.rkt"):
        assert "PITER" in _read(path), path
    assert VARIANTS.search("the PITR choices") and VARIANTS.search("P.I.T.E.R. is") and CASE_VARIANTS.search("Piter says")
    assert not VARIANTS.search("PITER") and not CASE_VARIANTS.search('class="piter" #piter-alternatives piter-preferences')
