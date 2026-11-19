"""Skipped tests: how many tests a check's command did not run, and why.

A check that exits 0 while the tests that matter were skipped proves nothing. This module counts
the skips in what the test runners print (pytest, go test, cargo test, mocha's spec reporter) or
in the JUnit XML a check collects, and turns them into a failure when the check says so:

- `#:max-skipped N`: more than N skipped tests fail the check;
- `#:forbid-skip-reason "TEXT"`: a skipped test whose reason contains TEXT (case-insensitive) fails it.

Both rules fail closed: with a rule set, output al cannot read, or skips that show no reason (when
the reason is what the rule needs), fail the check with a line that says which flag to add.

Standard library only. Lines that start with `# ` are comments (the recorded test logs use them).
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ElementTree
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

NO_REASON = "(no reason given)"
PYTEST_NO_RS = "(reason not printed: run pytest with -rs)"
MOCHA_PENDING = "pending (mocha prints no reason)"
GO_NO_V = "go (no -v)"
JUNIT_MAX_BYTES = 20 * 1024 * 1024
REASON_MAX = 120


@dataclass
class SkipReport:
    skipped: int = 0
    passed: int = 0
    failed: int = 0
    reasons: Counter = field(default_factory=Counter)
    unexplained: int = 0  # skipped tests that show no reason
    sources: list[str] = field(default_factory=list)

    def __add__(self, other: "SkipReport") -> "SkipReport":
        return SkipReport(self.skipped + other.skipped, self.passed + other.passed, self.failed + other.failed,
                          self.reasons + other.reasons, self.unexplained + other.unexplained,
                          self.sources + other.sources)

    def top(self, n: int = 5) -> list[list[Any]]:
        ordered = sorted(self.reasons.items(), key=lambda item: (-item[1], item[0]))
        return [[reason, count] for reason, count in ordered[:n]]

    def as_dict(self) -> dict[str, Any]:
        return {"skipped": self.skipped, "passed": self.passed, "failed": self.failed,
                "reasons": self.top(5), "unexplained": self.unexplained, "source": "+".join(self.sources)}


_LOCATION = re.compile(r"^\S+?:\d+:\s*")


def normalise_reason(text: str) -> str:
    """`path:line: reason` → `reason`, whitespace collapsed, `Skipped: ` dropped, at most 120 characters."""
    reason = _LOCATION.sub("", str(text or "").strip(), count=1)
    reason = " ".join(reason.split())
    if reason.startswith("Skipped: "):
        reason = reason[len("Skipped: "):]
    reason = reason[:REASON_MAX].strip()
    return reason or NO_REASON


def _lines(text: str) -> list[str]:
    return [line for line in str(text or "").splitlines() if not line.startswith("# ")]


# --------------------------------------------------------------------------------------------- pytest

_PYTEST_SUMMARY = re.compile(
    r"^=+ (?P<body>[^=]*?\b\d+ (passed|failed|skipped|error|errors|xfailed|xpassed|deselected)\b[^=]*) in [\d.]+s.* =+$")
# `pytest -q` prints the same summary without the `=` rule
_PYTEST_QUIET = re.compile(
    r"^(?P<body>\d+ (passed|failed|skipped|error|errors|xfailed|xpassed|deselected|warnings?)"
    r"(, \d+ (passed|failed|skipped|error|errors|xfailed|xpassed|deselected|warnings?))*) in [\d.]+s\b.*$")
_PYTEST_REASON = re.compile(r"^SKIPPED \[(\d+)\] ([^:]+:\d+): (.*)$")


def _count(body: str, word: str) -> int:
    return sum(int(n) for n in re.findall(rf"\b(\d+) {word}\b", body))


def parse_pytest(text: str) -> SkipReport | None:
    report, seen = SkipReport(), False
    for line in _lines(text):
        match = _PYTEST_SUMMARY.match(line) or _PYTEST_QUIET.match(line)
        if match:
            seen = True
            body = match.group("body")
            report.passed += _count(body, "passed")
            report.failed += _count(body, "failed")
            report.skipped += _count(body, "skipped")
            continue
        reason = _PYTEST_REASON.match(line)
        if reason:
            report.reasons[normalise_reason(reason.group(3))] += int(reason.group(1))
    if not seen:
        return None
    missing = report.skipped - sum(report.reasons.values())
    if missing > 0:
        report.unexplained = missing
        report.reasons[PYTEST_NO_RS] += missing
    report.sources.append("pytest")
    return report


# ----------------------------------------------------------------------------------------------- go

_GO_TEXT = re.compile(r"^(=== RUN|--- (PASS|FAIL|SKIP):|ok\s|FAIL\s|PASS$)")
_GO_VERBOSE = re.compile(r"^(=== RUN|--- (PASS|FAIL|SKIP):)")
_GO_RESULT = re.compile(r"^--- (PASS|FAIL|SKIP): (\S+) \(")
_GO_PACKAGE = re.compile(r"^(ok|FAIL)\s+\S+\s+(\(cached\)|[\d.]+s)")
_GO_LOG = re.compile(r"^\s+\S+\.go:\d+: (.*)$")


def _parse_go_json(lines: list[str]) -> SkipReport | None:
    report, seen = SkipReport(), False
    outputs: dict[tuple[str, str], list[str]] = {}
    for line in lines:
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or "Action" not in event:
            continue
        seen = True
        test = event.get("Test")
        if not test:
            continue
        key = (str(event.get("Package", "")), str(test))
        action = event.get("Action")
        if action == "output":
            outputs.setdefault(key, []).append(str(event.get("Output", "")).rstrip("\n"))
        elif action == "pass":
            report.passed += 1
        elif action == "fail":
            report.failed += 1
        elif action == "skip":
            report.skipped += 1
            logged = [m.group(1) for m in map(_GO_LOG.match, outputs.get(key, [])) if m]
            if logged:
                report.reasons[normalise_reason(logged[-1])] += 1
            else:
                report.unexplained += 1
                report.reasons[NO_REASON] += 1
    if not seen:
        return None
    report.sources.append("go")
    return report


def _parse_go_text(lines: list[str]) -> SkipReport | None:
    if not any(_GO_VERBOSE.match(line) for line in lines):
        if any(_GO_PACKAGE.match(line) for line in lines):
            return SkipReport(sources=[GO_NO_V])
        return None
    report = SkipReport()
    pending: str | None = None  # the last `file.go:N: …` line of the running test (Go ≥ 1.14 streams it first)
    for index, line in enumerate(lines):
        if line.startswith("=== RUN"):
            pending = None
            continue
        log = _GO_LOG.match(line)
        if log:
            pending = log.group(1)
            continue
        result = _GO_RESULT.match(line)
        if not result:
            continue
        status = result.group(1)
        if status == "PASS":
            report.passed += 1
        elif status == "FAIL":
            report.failed += 1
        else:
            report.skipped += 1
            reason = pending
            if reason is None and index + 1 < len(lines):  # older Go prints it after the --- SKIP line
                after = _GO_LOG.match(lines[index + 1])
                reason = after.group(1) if after else None
            if reason is None:
                report.unexplained += 1
                report.reasons[NO_REASON] += 1
            else:
                report.reasons[normalise_reason(reason)] += 1
        pending = None
    report.sources.append("go")
    return report


def parse_go(text: str) -> SkipReport | None:
    lines = _lines(text)
    return _parse_go_json(lines) or _parse_go_text(lines)


# -------------------------------------------------------------------------------------------- cargo

_CARGO_RESULT = re.compile(r"^test result: \w+\. (\d+) passed; (\d+) failed; (\d+) ignored;")
_CARGO_IGNORED = re.compile(r"^test \S+ \.\.\. ignored(?:, (.*))?$")


def parse_cargo(text: str) -> SkipReport | None:
    report, seen = SkipReport(), False
    explained = Counter()
    for line in _lines(text):
        result = _CARGO_RESULT.match(line)
        if result:
            seen = True
            report.passed += int(result.group(1))
            report.failed += int(result.group(2))
            report.skipped += int(result.group(3))
            continue
        ignored = _CARGO_IGNORED.match(line)
        if ignored:
            if ignored.group(1):
                explained[normalise_reason(ignored.group(1))] += 1
            else:
                explained[NO_REASON] += 1
    if not seen:
        return None
    report.reasons = explained
    named = sum(count for reason, count in explained.items() if reason != NO_REASON)
    report.unexplained = max(0, report.skipped - named)
    missing = report.unexplained - explained[NO_REASON]
    if missing > 0:
        report.reasons[NO_REASON] += missing
    report.sources.append("cargo")
    return report


# -------------------------------------------------------------------------------------------- mocha

_MOCHA_PASSING = re.compile(r"^\s+(\d+) passing \(")
_MOCHA_FAILING = re.compile(r"^\s+(\d+) failing$")
_MOCHA_PENDING = re.compile(r"^\s+(\d+) pending$")


def parse_mocha(text: str) -> SkipReport | None:
    report, seen = SkipReport(), False
    for line in _lines(text):
        passing = _MOCHA_PASSING.match(line)
        if passing:
            seen = True
            report.passed += int(passing.group(1))
            continue
        failing = _MOCHA_FAILING.match(line)
        if failing:
            report.failed += int(failing.group(1))
            continue
        pending = _MOCHA_PENDING.match(line)
        if pending:
            report.skipped += int(pending.group(1))
    if not seen:
        return None
    if report.skipped:
        report.reasons[MOCHA_PENDING] += report.skipped
        report.unexplained = report.skipped
    report.sources.append("mocha")
    return report


# -------------------------------------------------------------------------------------------- junit

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_junit(paths: Iterable[Path]) -> SkipReport:
    report = SkipReport()
    for path in paths:
        path = Path(path)
        try:
            if path.stat().st_size > JUNIT_MAX_BYTES:
                report.sources.append(f"junit ({path.name} skipped: over 20 MB)")
                continue
            root = ElementTree.parse(path).getroot()
        except (OSError, ElementTree.ParseError) as exc:
            report.sources.append(f"junit ({path.name} unreadable: {type(exc).__name__})")
            continue
        for case in root.iter():
            if _local(case.tag) != "testcase":
                continue
            children = {_local(child.tag): child for child in case}
            if "skipped" in children:
                skipped = children["skipped"]
                report.skipped += 1
                reason = skipped.get("message") or (skipped.text or "").strip()
                if reason:
                    report.reasons[normalise_reason(reason)] += 1
                else:
                    report.unexplained += 1
                    report.reasons[NO_REASON] += 1
            elif "failure" in children or "error" in children:
                report.failed += 1
            else:
                report.passed += 1
        report.sources.append(f"junit:{path.name}")
    return report


# ---------------------------------------------------------------------------------------- accounting

PARSERS = (parse_pytest, parse_go, parse_cargo, parse_mocha)


def from_log(text: str) -> SkipReport | None:
    """The sum of every parser that recognises the text (several runners in one log add up)."""
    found = [report for report in (parser(text) for parser in PARSERS) if report is not None]
    if not found:
        return None
    total = found[0]
    for report in found[1:]:
        total = total + report
    return total


# ------------------------------------------------------------------------------------- failing tests

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+?)(?: - .*)?$")
_GO_FAIL = re.compile(r"^\s*--- FAIL: (\S+) \(")
_GO_FAIL_PACKAGE = re.compile(r"^FAIL\t(\S+)")
_CARGO_FAILED = re.compile(r"^test (\S+) \.\.\. FAILED$")
_JS_FAIL = re.compile(r"^\s*FAIL\s+(\S+\.(?:[cm]?[jt]sx?))(?:\s|$)")


def failing_tests(text: str) -> list[str]:
    """The tests a log says failed, in order, each once: pytest `FAILED a/b.py::t` (and `ERROR
    a/b.py`) as `a/b.py::t`; go `--- FAIL: T` as `PKG::T` with the package of the `FAIL\\tPKG`
    line that follows (subtests `T/sub` are left out, their test fails too; `go test -json` too);
    cargo `test x ... FAILED` as `x`; jest/mocha `FAIL path` as the test file."""
    found: list[str] = []
    pending: list[str] = []  # go: failed tests whose package line has not come yet
    for raw in _lines(text):
        line = _ANSI.sub("", raw).rstrip()
        if line.startswith("{"):
            try:
                event = json.loads(line)
            except ValueError:
                event = None
            if isinstance(event, dict) and event.get("Action") == "fail" and event.get("Test"):
                if "/" not in str(event["Test"]):
                    found.append(f"{event.get('Package', '')}::{event['Test']}".lstrip(":"))
                continue
        match = _PYTEST_FAILED.match(line)
        if match:
            found.append(match.group(1))
            continue
        match = _GO_FAIL.match(line)
        if match:
            if "/" not in match.group(1):
                pending.append(match.group(1))
            continue
        match = _GO_FAIL_PACKAGE.match(line)
        if match:
            found += [f"{match.group(1)}::{name}" for name in pending]
            pending = []
            continue
        match = _CARGO_FAILED.match(line) or _JS_FAIL.match(line)
        if match:
            found.append(match.group(1))
    found += pending  # a go log without its package line still names the tests
    return list(dict.fromkeys(found))


def _junit_files(layer: Any, state: Any, step: dict[str, Any], started: float) -> list[Path]:
    worktree = Path(state.worktree).resolve() if getattr(state, "worktree", "") else None
    if worktree is None or not worktree.is_dir():
        return []
    files: list[Path] = []
    for artefact_id in step.get("collect") or []:
        try:
            option = layer.option("artefacts", str(artefact_id))
        except Exception:  # an unknown id is the compiler's to reject; nothing to count here
            continue
        if option.get("kind") != "junit" or not option.get("glob"):
            continue
        for path in sorted(worktree.glob(str(option["glob"]))):
            try:
                if path.is_file() and path.resolve().is_relative_to(worktree) and path.stat().st_mtime >= started:
                    files.append(path)
            except OSError:
                continue
    return files


def account(layer: Any, state: Any, step: dict[str, Any], log_path: Path, started: float) -> SkipReport | None:
    """Skips of one attempt: the check's #:collect junit reports written during it, else its log."""
    files = _junit_files(layer, state, step, started)
    if files:
        return parse_junit(files)
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return from_log(text)


def _reasons_text(reasons: Iterable[Iterable[Any]]) -> str:
    return ", ".join(f"{reason} ×{count}" for reason, count in reasons)


def violations(step: dict[str, Any], report: SkipReport | None) -> list[str]:
    """The check's skip rules broken by `report`; each message starts `al: check NAME fails:`."""
    name = str(step.get("name", ""))
    head = f"al: check {name} fails:"
    max_skipped = int(step.get("maxSkipped", -1) if step.get("maxSkipped") is not None else -1)
    forbid = str(step.get("forbidSkipReason") or "")
    problems: list[str] = []
    blind = report is not None and GO_NO_V in report.sources
    if max_skipped >= 0:
        if report is None:
            problems.append(f"{head} no test report recognised in the output, so al cannot count skipped tests "
                            f"(#:max-skipped {max_skipped}); print the runner's summary or #:collect a junit report")
        elif blind:
            problems.append(f"{head} go test ran without -v, so al cannot see skipped tests "
                            f"(#:max-skipped {max_skipped}); run go test with -v or -json")
        elif report.skipped > max_skipped:
            problems.append(f"{head} {report.skipped} tests skipped, more than #:max-skipped {max_skipped} "
                            f"(top reasons: {_reasons_text(report.top(5))})")
    if forbid:
        if report is None:
            problems.append(f"{head} no test report recognised in the output, so al cannot count skipped tests "
                            f"(#:forbid-skip-reason {json.dumps(forbid)}); print the runner's summary or "
                            f"#:collect a junit report")
        else:
            needle = forbid.casefold()
            for reason, count in sorted(report.reasons.items(), key=lambda item: (-item[1], item[0])):
                if needle in str(reason).casefold():
                    problems.append(f"{head} {count} tests skipped because {json.dumps(reason)}, "
                                    f"forbidden by #:forbid-skip-reason {json.dumps(forbid)}")
            if report.unexplained > 0 or blind:
                count = f"{report.unexplained} skipped tests show" if report.unexplained else "skipped tests show"
                problems.append(f"{head} {count} no reason, so #:forbid-skip-reason cannot be checked; "
                                f"run pytest with -rs / go test with -v, or #:collect a junit report")
    return problems


def summary_text(record: dict[str, Any] | None) -> str:
    """`3 skipped: no podman ×2, needs network ×1`, or "" when nothing was skipped."""
    record = record or {}
    skipped = int(record.get("skipped") or 0)
    if skipped <= 0:
        return ""
    reasons = _reasons_text(record.get("reasons") or [])
    return f"{skipped} skipped: {reasons}" if reasons else f"{skipped} skipped"
