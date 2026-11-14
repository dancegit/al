import sys
from pathlib import Path

import pytest

RUNTIME = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RUNTIME))

from adw_modules import tasklist as tl  # noqa: E402

SAMPLE = """# Tasks

- [ ] Loose task before any heading
## feat-dark-mode
- [ ] Add a dark mode toggle to the settings page {sdlc, heavy}
- [>] Document the toggle in docs/settings.md
## fix-login
- [x] Fix the off-by-one in pagination <!-- adw:1a2b3c4d commit:9f8e7d6 -->
- [ ] Null check in session refresh {class: bug}
- [~] Running thing <!-- adw:deadbeef -->
- [!] Broken thing <!-- adw:cafe0001 reason:tests failed -->
- [>] Waits for the running one
## docs
- [x] Done first
- [>] Then this {chore}
- not a task
"""


def test_parse_markers_sections_tags():
    tasks = tl.parse(SAMPLE)
    by = {t.text: t for t in tasks}
    assert len(tasks) == 10
    assert by["Loose task before any heading"].section == "default"
    t = by["Add a dark mode toggle to the settings page"]
    assert (t.section, t.status, t.tags, t.line_no) == ("feat-dark-mode", "open", ["sdlc", "heavy"], 5)
    assert t.model_set == "heavy" and t.workflow(["sdlc", "zte"]) == "sdlc" and t.klass is None
    assert by["Document the toggle in docs/settings.md"].status == "blocked"
    done = by["Fix the off-by-one in pagination"]
    assert done.status == "done" and done.adw_id == "1a2b3c4d"
    assert by["Null check in session refresh"].klass == "bug"
    assert by["Null check in session refresh"].tags == ["class:bug"]
    assert by["Running thing"].status == "running"
    assert by["Broken thing"].status == "failed"
    assert by["Then this"].klass == "chore"
    assert t.raw == "- [ ] Add a dark mode toggle to the settings page {sdlc, heavy}"


def test_hash_tags_and_custom_markers():
    opt = {"tagSyntax": "hash", "openMarker": "TODO", "doneMarker": "DONE", "runningMarker": "WIP",
           "failedMarker": "FAIL", "blockedMarker": "WAIT"}
    text = "## s\n- TODO Fix issue #12 in parser #bug #zte #base\n- WAIT later #feature\n- DONE x\n"
    tasks = tl.parse(text, opt)
    assert tasks[0].text == "Fix issue #12 in parser"
    assert tasks[0].tags == ["bug", "zte", "base"]
    assert tasks[0].klass == "bug" and tasks[0].model_set == "base" and tasks[0].workflow(["zte"]) == "zte"
    assert [t.status for t in tasks] == ["open", "blocked", "done"]
    assert tl.split_tags("Do it #class:patch", "hash") == ("Do it", ["class:patch"])


def test_blocked_eligibility():
    tasks = tl.parse(SAMPLE)
    names = [t.text for t in tl.eligible(tasks)]
    assert names == ["Loose task before any heading", "Add a dark mode toggle to the settings page",
                     "Null check in session refresh", "Then this"]
    # once the predecessor is done, the blocked task opens up
    text = SAMPLE.replace("- [ ] Add a dark mode toggle to the settings page {sdlc, heavy}",
                          "- [x] Add a dark mode toggle to the settings page {sdlc, heavy}")
    assert "Document the toggle in docs/settings.md" in [t.text for t in tl.eligible(tl.parse(text))]
    serial = tl.eligible(tl.parse(SAMPLE), serial_sections=True)
    assert "Null check in session refresh" not in [t.text for t in serial]  # fix-login has a running task


def _write(tmp_path, text):
    p = tmp_path / "tasks.md"
    p.write_bytes(text.encode())
    return p


def _changed_lines(before: str, after: str) -> list[int]:
    a, b = before.splitlines(keepends=True), after.splitlines(keepends=True)
    assert len(a) == len(b)
    return [i + 1 for i, (x, y) in enumerate(zip(a, b)) if x != y]


def test_claim_complete_round_trip_preserves_bytes(tmp_path):
    p = _write(tmp_path, SAMPLE)
    task = next(t for t in tl.parse(SAMPLE) if t.text.startswith("Null check"))
    claimed = tl.claim(p, task, "abc12345")
    assert claimed.status == "running" and claimed.adw_id == "abc12345"
    after = p.read_text()
    assert _changed_lines(SAMPLE, after) == [task.line_no]
    assert after.splitlines()[task.line_no - 1] == "- [~] Null check in session refresh {class: bug} <!-- adw:abc12345 -->"
    assert tl.complete(p, "abc12345", "9f8e7d6") == task.line_no
    final = p.read_text()
    assert _changed_lines(SAMPLE, final) == [task.line_no]
    assert final.splitlines()[task.line_no - 1] == \
        "- [x] Null check in session refresh {class: bug} <!-- adw:abc12345 commit:9f8e7d6 -->"
    reparsed = next(t for t in tl.parse(final) if t.line_no == task.line_no)
    assert reparsed.text == "Null check in session refresh" and reparsed.klass == "bug"


def test_claim_fail_round_trip_crlf_and_no_trailing_newline(tmp_path):
    text = SAMPLE.replace("\n", "\r\n").rstrip("\r\n")
    p = _write(tmp_path, text)
    task = tl.eligible(tl.parse(text))[0]
    tl.claim(p, task, "f00d")
    tl.fail(p, "f00d", "lint failed --> twice\nsecond line")
    raw = p.read_bytes().decode()
    assert not raw.endswith("\n")
    a, b = text.split("\r\n"), raw.split("\r\n")
    diff = [i + 1 for i, (x, y) in enumerate(zip(a, b)) if x != y]
    assert diff == [task.line_no] and len(a) == len(b)
    assert b[task.line_no - 1] == "- [!] Loose task before any heading <!-- adw:f00d reason:lint failed -> twice second line -->"
    assert not (tmp_path / "tasks.md.lock").read_text()  # sidecar lock exists, stays empty


def test_two_sequential_claims_never_pick_same_task(tmp_path):
    p = _write(tmp_path, SAMPLE)
    stale = tl.parse(SAMPLE)  # both claimers parsed before either wrote
    first = tl.eligible(stale)[0]
    tl.claim(p, first, "one")
    with pytest.raises(tl.TaskError):
        tl.claim(p, first, "two")  # the stale view of the same task is refused
    nxt = tl.eligible(tl.parse(p.read_text()))[0]
    assert nxt.line_no != first.line_no
    tl.claim(p, nxt, "two")
    running = {t.adw_id: t.line_no for t in tl.parse(p.read_text()) if t.status == "running"}
    assert running["one"] == first.line_no and running["two"] == nxt.line_no


def test_claim_follows_moved_line(tmp_path):
    p = _write(tmp_path, SAMPLE)
    task = next(t for t in tl.parse(SAMPLE) if t.text.startswith("Null check"))
    p.write_text("<!-- note -->\n" + SAMPLE)  # a human inserted a line above
    tl.claim(p, task, "moved1")
    assert "- [~] Null check in session refresh {class: bug} <!-- adw:moved1 -->" in p.read_text()


def test_retag(tmp_path):
    p = _write(tmp_path, SAMPLE)
    task = tl.eligible(tl.parse(SAMPLE))[0]
    tl.claim(p, task, "trg-1")
    tl.retag(p, "trg-1", "real42")
    assert tl.parse(p.read_text())[0].adw_id == "real42"
    with pytest.raises(tl.TaskError):
        tl.complete(p, "trg-1", "x")


def test_append(tmp_path):
    p = _write(tmp_path, SAMPLE)
    n = tl.append(p, "feat-dark-mode", "Add keyboard shortcut", ["heavy"])
    lines = p.read_text().splitlines()
    assert lines[n - 1] == "- [ ] Add keyboard shortcut {heavy}" and lines[n] == "## fix-login"
    n2 = tl.append(p, "new-thing", "Brand new")
    assert p.read_text().endswith("## new-thing\n- [ ] Brand new\n")
    assert tl.parse(p.read_text())[-1].section == "new-thing" and n2 == len(p.read_text().splitlines())
    fresh = tmp_path / "sub" / "t.md"
    tl.append(fresh, "default", "First", option={"tagSyntax": "hash"}, tags=["bug"])
    assert fresh.read_text() == "# Tasks\n\n- [ ] First #bug\n"
    assert tl.parse(fresh.read_text(), {"tagSyntax": "hash"})[0].klass == "bug"
