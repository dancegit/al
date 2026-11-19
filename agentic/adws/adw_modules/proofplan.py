"""The plan's Proof plan table, the provability check (step `prove`) and the run-scoped proof folder.

A plan (plan-chore, plan-bug, plan-feature) ends its How it is proven with one table:

    ### Proof plan
    | # | Criterion | Proven by | Writes | Needs |
    |---|---|---|---|---|
    | C1 | the run page shows the proof | `uv run pytest -q` `tests/test_page.py::test_proof` | shots/*.png (3) | tool:chromium |

`parse` reads it: Proven by is the command (its first backticked span) and the test ids (the other
spans); Writes are globs relative to the worktree with `(count)`, and `proof:GLOB` for files of this
run only, under `.al-proof/` (PROOF_DIR); Needs are tokens (NEED_FORMS) or `none`.

Right after the plan step, `check` (run by steps.run_step as the step `prove`, no agent) gives
every row one outcome: `proven-here`, `proven-on:ENV`, `needs-declaration` (the layer author adds a
declaration, printed exactly), `needs-person` (an approver decides: `al approve`), `unprovable`
(the operator, or the agent for a malformed row). Anything but the first two stops the run before
the build, as a StepFailed that is handed over like any other stop. A plan without the table
changes nothing: no step is recorded.

The run-scoped proof: files under `<worktree>/.al-proof/` (git-ignored by its own `.gitignore`)
that a row declares as `proof:GLOB` are collected in full (`collect_proof`) for the reviewers,
without a layer change and without an image limit.
"""
from __future__ import annotations

import fnmatch
import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Any

from . import approvals, checkenv, events, nested_engine
from .errors import LayerError, StepFailed

HEADER = ("#", "criterion", "proven by", "writes", "needs")
PROOF_DIR = ".al-proof"
STEP = "prove"
MAX_PROOF_FILES = 1000
PROVEN = ("proven-here", "proven-on")
OUTCOMES = ("proven-here", "proven-on:ENV", "needs-declaration", "needs-person", "unprovable")
PARTY_ORDER = ("layer author", "operator", "approver")
NEED_FORMS = ("tool:NAME", "engine:podman|docker", "network", "model:NAME", "secret:NAME", "host:ID",
              "approval:KIND[:ARG]")
_NEED = re.compile(r"^(?:tool:[A-Za-z0-9._+-]+|engine:(?:podman|docker)|network|model:[A-Za-z0-9._:/-]+|"
                   r"secret:[A-Za-z_][A-Za-z0-9_]*|host:[A-Za-z0-9._-]+|approval:\S+)$")
_ID = re.compile(r"^C\d+$")
_COUNT = re.compile(r"\s*\((\d+)\)\s*$")
_SPAN = re.compile(r"`([^`]+)`")
_ROW_TEMPLATE = "| C1 | <checkable result> | `<validation command>` `<test id>` | <globs (count), proof:GLOB, or -> | <none, or need tokens> |"
IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
VIDEO = {".mp4", ".webm", ".mov", ".mkv"}
AUDIO = {".mp3", ".wav", ".ogg", ".m4a", ".flac"}


# ---------------------------------------------------------------- the table

def _cells(line: str) -> list[str] | None:
    text = line.strip()
    if not text.startswith("|"):
        return None
    text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    parts = re.split(r"(?<!\\)\|", text)
    return [part.strip().replace("\\|", "|") for part in parts]


def _separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def _empty(text: str) -> bool:
    return text.strip().strip("`").strip().lower() in ("", "-", "none", "—")


def _globs(cell: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    writes: list[dict[str, Any]] = []
    proof: list[dict[str, Any]] = []
    if _empty(cell):
        return writes, proof
    for item in cell.split(","):
        item = item.strip()
        if _empty(item):
            continue
        count = 1
        match = _COUNT.search(item)
        if match:
            count, item = int(match.group(1)), item[:match.start()].strip()
        item = item.strip("`").strip()
        if item.startswith("proof:"):
            proof.append({"glob": item[len("proof:"):].strip().lstrip("/"), "count": count})
        elif item:
            writes.append({"glob": item, "count": count})
    return writes, proof


def _needs(cell: str) -> list[str]:
    if _empty(cell):
        return []
    return [token.strip("`") for token in re.split(r"[\s,]+", cell.strip()) if token.strip("`")]


def _row(cells: list[str]) -> dict[str, Any]:
    cells = (cells + [""] * 5)[:5]
    ident, criterion, proven, writes_cell, needs_cell = cells
    spans = _SPAN.findall(proven)
    writes, proof = _globs(writes_cell)
    row: dict[str, Any] = {"id": ident.strip(), "criterion": criterion.strip(),
                           "command": spans[0].strip() if spans else "", "tests": [s.strip() for s in spans[1:]],
                           "writes": writes, "proof": proof, "needs": _needs(needs_cell)}
    why = []
    if not _ID.fullmatch(row["id"]):
        why.append(f"the id {row['id'] or '(empty)'!r} is not C<n>")
    if not row["criterion"]:
        why.append("the criterion is empty")
    if not row["command"]:
        why.append("Proven by names no command in backticks")
    unknown = [need for need in row["needs"] if not _NEED.fullmatch(need)]
    for need in row["needs"]:
        if need.startswith("approval:") and need not in unknown:
            try:
                approvals.parse(need[len("approval:"):])
            except ValueError:
                unknown.append(need)
    if unknown:
        why.append(f"unknown need {', '.join(unknown)}")
    if why:
        row["invalid"] = "; ".join(why)
    return row


def parse(text: str) -> dict[str, Any]:
    """The first markdown table whose header is HEADER: {"table": "parsed", "rows": [...]};
    {"table": "absent"} without one; {"table": "invalid", "rows": []} for a header with no rows."""
    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        cells = _cells(line)
        if cells is None or tuple(cell.lower() for cell in cells) != HEADER:
            continue
        rows = []
        for following in lines[index + 1:]:
            row = _cells(following)
            if row is None:
                break
            if _separator(row):
                continue
            rows.append(_row(row))
        return {"table": "parsed" if rows else "invalid", "rows": rows}
    return {"table": "absent"}


def plan_text(state: Any, layer: Any = None) -> str:
    plan = str(getattr(state, "plan", "") or "")
    if not plan:
        return ""
    path = Path(plan)
    if not path.is_absolute() and layer is not None:
        path = Path(layer.layer_dir) / path
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def has_table(state: Any) -> bool:
    return (getattr(state, "proof_plan", None) or {}).get("table") == "parsed"


def proof_globs(state: Any) -> list[str]:
    return [str(item["glob"]) for row in (getattr(state, "proof_plan", None) or {}).get("rows") or []
            for item in row.get("proof") or [] if item.get("glob")]


# ---------------------------------------------------------------- what proves a row

def resolve_command(layer: Any, adw: dict[str, Any], text: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(check step, cmd) the text of Proven by names: a check step by name; else the check step whose
    cmd is that name or whose cmd's `run` is exactly the text; else a declared cmd no check step runs
    (the evidence step runs it); else (None, None)."""
    text = str(text or "").strip()
    checks = [step for step in adw.get("steps") or [] if step.get("kind") == "check"]

    def cmd_of(name: str) -> dict[str, Any] | None:
        try:
            return layer.cmd(name)
        except (LayerError, KeyError):
            return None

    for step in checks:
        if str(step.get("name")) == text:
            return step, cmd_of(str(step.get("cmd")))
    for step in checks:
        cmd = cmd_of(str(step.get("cmd")))
        if str(step.get("cmd")) == text or (cmd is not None and str(cmd.get("run", "")).strip() == text):
            return step, cmd
    for cmd in layer.ir.get("codebase", {}).get("commands") or []:
        if str(cmd.get("name")) == text or str(cmd.get("run", "")).strip() == text:
            return None, cmd
    return None, None


def which(stack: Any, name: str) -> bool:
    """True when `name` is a command where `stack` runs codebase commands (seam for tests)."""
    from . import steps

    code, _ = steps.run_command(stack.wrap(f"command -v {shlex.quote(name)} >/dev/null 2>&1"), stack.cwd,
                                stack.process_env(), 60)
    return code == 0


def _artefact_kind(glob: str) -> str:
    suffix = PurePosixPath(glob.replace("*", "x")).suffix.lower()
    if suffix in IMAGE:
        return "image"
    if suffix in VIDEO:
        return "video"
    if suffix in AUDIO:
        return "audio"
    if suffix == ".html":
        return "html"
    if suffix == ".xml":
        return "junit"
    return "file"


def _sample(glob: str) -> str:
    return re.sub(r"\[[^\]]*\]|[*?]", "x", glob)


def _glob_covers(artefact_glob: str, sample: str) -> bool:
    if not artefact_glob:
        return False
    try:
        if PurePosixPath(sample).match(artefact_glob):
            return True
    except ValueError:
        pass
    return fnmatch.fnmatchcase(sample, artefact_glob)


def _review_option(layer: Any, adw: dict[str, Any]) -> dict[str, Any]:
    try:
        return layer.option("reviews", adw["review"])
    except (LayerError, KeyError):
        return {}


def _review_artefacts(layer: Any, adw: dict[str, Any]) -> list[dict[str, Any]]:
    found = []
    for artefact_id in _review_option(layer, adw).get("artefacts") or []:
        try:
            found.append(layer.option("artefacts", str(artefact_id)))
        except (LayerError, KeyError):
            continue
    return found


def _slug(glob: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", glob.lower().split("/")[0].split("*")[0]).strip("-") or "proof"


def _problem(outcome: str, why: str, fix: str, party: str, **extra: Any) -> dict[str, Any]:
    return {"outcome": outcome, "why": why, "fix": fix, "party": party, **extra}


def _env_decl(option: dict[str, Any]) -> str:
    return f"(environment podman #:id {option.get('id')} …)"


def _write_problems(layer: Any, adw: dict[str, Any], row: dict[str, Any]) -> list[dict[str, Any]]:
    problems = []
    declared = _review_artefacts(layer, adw)
    review_id = str(_review_option(layer, adw).get("id") or "R")
    for item in row.get("writes") or []:
        glob, count = str(item["glob"]), int(item.get("count") or 1)
        sample = _sample(glob)
        hits = [option for option in declared if option.get("kind") != "log"
                and _glob_covers(str(option.get("glob") or ""), sample)]
        if not hits:
            ident = _slug(glob)
            problems.append(_problem(
                "needs-declaration", f"{glob} matches no artefact the reviewers are given",
                f'(artefact {_artefact_kind(glob)} #:id {ident} #:glob "{glob}") and add {ident} to #:artefacts '
                f"of (review … #:id {review_id}), or write it under proof: (proof:{PurePosixPath(glob).name})",
                "layer author"))
            continue
        images = [option for option in hits if option.get("kind") == "image"]
        if images and all(int(option.get("maxFiles", 0) or 0) < count for option in images):
            option = images[0]
            problems.append(_problem(
                "needs-declaration",
                f"{glob} writes {count} files but artefact {option.get('id')} hands at most "
                f"{int(option.get('maxFiles', 0) or 0)} images to the reviewers",
                f"#:max-files {count} on (artefact image #:id {option.get('id')} …), or write them under proof: "
                f"(proof:{PurePosixPath(glob).name})", "layer author"))
    return problems


def _need_problem(layer: Any, state: Any, stack: Any, need: str) -> dict[str, Any] | None:
    option = getattr(stack, "podman_option", None) or None
    kind, _, arg = need.partition(":")
    if kind == "tool":
        if which(stack, arg):
            return None
        if option:
            return _problem("needs-declaration", f"{arg} is not installed where the check runs",
                            f"add the package that provides `{arg}` to (environment podman #:id {option.get('id')} "
                            "#:packages …)", "layer author")
        return _problem("unprovable", f"{arg} is not installed on this machine",
                        f"install {arg} on this machine, or run the check #:in a podman environment that has it",
                        "operator")
    if kind == "engine":
        if option:
            if nested_engine.enabled(option):
                return None
            return _problem("needs-declaration", f"the check's container has no {arg} engine",
                            f"#:nested-engine #t on {_env_decl(option)}", "layer author")
        if which(stack, arg):
            return None
        return _problem("needs-declaration", f"{arg} is not available where the check runs",
                        f"run the check #:in a podman environment with #:nested-engine #t", "layer author")
    if kind in ("network", "model"):
        if not option or str(option.get("network") or "") != "none":
            return None
        return _problem("needs-declaration", f"the check's container has no network ({need})",
                        f"#:network slirp on {_env_decl(option)}", "layer author")
    if kind == "secret":
        try:
            present = bool(stack.process_env().get(arg))
        except Exception:  # noqa: BLE001 — an environment that cannot be read has no secret
            present = False
        if not present and option:
            from . import envs

            present = any(arg == name or arg in envs.CREDENTIALS.get(str(name), ("", []))[1]
                          for name in option.get("credentials") or [])
        if present:
            return None
        return _problem("needs-person", f"the secret {arg} is not available to the run",
                        f"provide secret {arg}", "approver", question=f"provide secret {arg}")
    if kind == "host":
        import os

        if os.environ.get("AL_HOST_ID", "") == arg:
            return None
        return _problem("unprovable", f"the check needs host {arg}, and this machine is not it",
                        f"run this task on a machine provisioned as {arg} (al deploy --as {arg})", "operator")
    if kind == "approval":
        if approvals.covered(state, arg) is not None:
            return None
        question = f"may this run {arg}? answer: al approve LAYER {state.adw_id} {arg}"
        return _problem("needs-person", f"{arg} is a decision only a person may take", question, "approver",
                        question=question, approval=arg)
    return _problem("unprovable", f"unknown need {need}", f"use one of {', '.join(NEED_FORMS)}", "agent")


def evaluate(layer: Any, state: Any, adw: dict[str, Any], stack: Any, row: dict[str, Any]) -> dict[str, Any]:
    """{outcome, why, fix, party, approvals, question} of one row (see the module docstring)."""
    if row.get("invalid"):
        return {"outcome": "unprovable", "why": f"malformed row: {row['invalid']}",
                "fix": f"write the row as {_ROW_TEMPLATE}; needs: {', '.join(NEED_FORMS)}, or none",
                "party": "agent", "approvals": [], "question": ""}
    check, cmd = resolve_command(layer, adw, str(row.get("command") or ""))
    if cmd is None:
        name = _slug(str(row.get("command") or "")) or "proof"
        return {"outcome": "needs-declaration", "why": f"`{row.get('command')}` is no cmd or check of the layer",
                "fix": f'(cmd {name} "{row.get("command")}") and (check {name} #:cmd {name}) in (adw {adw.get("name")} …)',
                "party": "layer author", "approvals": [], "question": ""}
    where = stack
    if check is not None:
        try:
            where = checkenv.stack_for(layer, state, check, stack)
        except (LayerError, StepFailed):
            where = stack
    problems = [problem for need in row.get("needs") or []
                if (problem := _need_problem(layer, state, where, str(need))) is not None]
    problems += _write_problems(layer, adw, row)
    if not problems:
        env_id = checkenv.target(adw, check) if check is not None else None
        return {"outcome": f"proven-on:{env_id}" if env_id else "proven-here", "why": "", "fix": "",
                "party": "", "approvals": [], "question": ""}
    ranked = sorted(problems, key=lambda p: PARTY_ORDER.index(p["party"]) if p["party"] in PARTY_ORDER else -1)
    first = ranked[0]
    return {"outcome": first["outcome"], "why": "; ".join(p["why"] for p in ranked),
            "fix": "; ".join(p["fix"] for p in ranked), "party": first["party"],
            "approvals": [p["approval"] for p in ranked if p.get("approval")],
            "question": "; ".join(p["question"] for p in ranked if p.get("question"))}


def proven(outcome: str) -> bool:
    return str(outcome).split(":")[0] in PROVEN


# ---------------------------------------------------------------- the step

def ensure_dir(state: Any, layer: Any = None) -> Path | None:
    """`<worktree>/.al-proof/`, git-ignored by its own `.gitignore` (`*`); None without a worktree."""
    base = str(getattr(state, "worktree", "") or "") or (str(layer.app_root) if layer is not None else "")
    if not base:
        return None
    folder = Path(base) / PROOF_DIR
    folder.mkdir(parents=True, exist_ok=True)
    ignore = folder / ".gitignore"
    if not ignore.is_file() or ignore.read_text(encoding="utf-8", errors="replace") != "*\n":
        ignore.write_text("*\n", encoding="utf-8")
    return folder


def check(layer: Any, state: Any, adw: dict[str, Any], stack: Any) -> dict[str, Any]:
    """The provability check of the plan's Proof plan (step `prove`); raises StepFailed("prove", …)
    when a row cannot be proven where the run runs."""
    parsed = parse(plan_text(state, layer))
    ensure_dir(state, layer)
    rows = []
    for row in parsed.get("rows") or []:
        result = evaluate(layer, state, adw, stack, row)
        rows.append({**row, **result, "state": "planned"})
    previous = dict(getattr(state, "proof_plan", None) or {})
    previous.pop("stopped", None)
    state.proof_plan = {**previous, "plan": state.plan, "table": parsed["table"], "rows": rows}
    failing = [row for row in rows if not proven(row["outcome"])]
    if parsed["table"] == "invalid":
        failing = [{"id": "-", "outcome": "unprovable", "why": "the Proof plan table has no rows",
                    "fix": f"add one row per checkable result: {_ROW_TEMPLATE}", "party": "agent", "approvals": []}]
    events.emit("proof.checked", rows=len(rows), failing=len(failing),
                outcomes=[str(row["outcome"]) for row in rows])
    if not failing:
        from . import convergence

        convergence.record_summary(state)
        state.save(layer.layer_dir)
        return state.proof_plan
    parties = [row["party"] for row in failing]
    party = next((p for p in PARTY_ORDER if p in parties), "agent")
    outcome = next(row["outcome"] for row in failing if row["party"] == party)
    wanted = list(dict.fromkeys(a for row in failing for a in row.get("approvals") or []))
    head = f"proof:{outcome}: {len(failing)} criterion(s) cannot be proven here; party: {party}"
    lines = [head] + [f"{row['id']} | {row['outcome']} | {row['why']} | {row['fix']}" for row in failing]
    text = "\n".join(lines)
    question = "; ".join(row.get("question") or "" for row in failing if row.get("question"))
    if party == "approver" and wanted:
        question = (f"may this run {', '.join(wanted)}? answer: al approve LAYER {state.adw_id} {' '.join(wanted)}")
    state.proof_plan["stopped"] = {"at": STEP, "outcome": outcome, "party": party, "reason": text,
                                   "approvals": wanted, "question": question}
    from . import convergence

    convergence.record_summary(state)
    state.save(layer.layer_dir)
    raise StepFailed(STEP, text)


# ---------------------------------------------------------------- the run-scoped proof

def _group(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if suffix in IMAGE:
        return "proof-image", "image"
    if suffix in VIDEO:
        return "proof-video", "video"
    if suffix in AUDIO:
        return "proof-audio", "audio"
    return "proof-file", "file"


def proof_files(state: Any) -> dict[str, list[Path]]:
    """{row id: files under .al-proof/ matching its proof globs}."""
    folder = Path(str(getattr(state, "worktree", "") or "")) / PROOF_DIR if getattr(state, "worktree", "") else None
    found: dict[str, list[Path]] = {}
    if folder is None or not folder.is_dir():
        return found
    root = folder.resolve()
    for row in (getattr(state, "proof_plan", None) or {}).get("rows") or []:
        files: list[Path] = []
        for item in row.get("proof") or []:
            glob = str(item.get("glob") or "")
            if not glob:
                continue
            try:
                matches = sorted(folder.glob(glob))
            except (ValueError, NotImplementedError):
                continue
            files += [path for path in matches if path.is_file() and path.name != ".gitignore"
                      and path.resolve().is_relative_to(root)]
        found[str(row.get("id"))] = list(dict.fromkeys(files))
    return found


def collect_proof(layer: Any, state: Any, step: str) -> list[dict[str, Any]]:
    """Hand every run-scoped proof file to the reviewers (artefact ids proof-image, proof-video,
    proof-audio, proof-file), all of them; a row with more than MAX_PROOF_FILES is recorded as an
    evidence gap ("too many proof files") instead of being cut."""
    from . import artefacts

    if not proof_globs(state):
        return []
    folder = Path(str(state.worktree)) / PROOF_DIR if state.worktree else None
    groups: dict[str, tuple[str, list[tuple[str, Path]]]] = {}
    too_many: dict[str, int] = {}
    seen: set[Path] = set()
    for row_id, files in proof_files(state).items():
        if len(files) > MAX_PROOF_FILES:
            too_many[row_id] = len(files)
            continue
        for path in files:
            if path in seen:
                continue
            seen.add(path)
            ident, kind = _group(path)
            groups.setdefault(ident, (kind, []))[1].append((path.relative_to(folder).as_posix(), path))
    if too_many:
        state.proof_plan["too_many"] = too_many
    else:
        state.proof_plan.pop("too_many", None)
    collected = []
    present = {str(item.get("id")) for item in state.artefacts}
    for ident in ("proof-image", "proof-video", "proof-audio", "proof-file"):
        if ident in groups:
            kind, matches = groups[ident]
            collected += artefacts.add_files(layer, state, ident, kind, step, matches)
        elif ident in present:
            artefacts.add_files(layer, state, ident, "file", step, [])
    return collected
