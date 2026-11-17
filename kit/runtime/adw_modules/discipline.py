"""Step discipline: each step does its own job, and the runtime checks that it did.

Every agent prompt opens with a "Your step" block (`${workflow_steps}`, `${scratch_dir}`): which
step this is in its workflow, what the later steps do, and so what this step must leave undone.
The runtime does not trust the prompt. Around every `run` step that may not commit, `watched`
records the worktree's HEAD, the repository's branches and tags, the main checkout's status and
the entries at the top of the layer directory, and compares them after the step. A difference
fails the step as a step-discipline violation (not a test failure); what can be undone safely is
undone (HEAD reset with the changes kept, stray layer entries moved into the run's scratch
directory) and the rest is left for a person. Around a step that may commit (the `commit`
template, or a command with #:may-commit #t), `clean_after_commit` restores what the commit left
uncommitted under the layer directory and protected paths, so the worktree equals the commit.

Standard library only. The protected modules are used, never changed: `gitsafe.run` /
`gitsafe.run_app` for a sandboxed run's git, plain `git -C` otherwise.
"""
from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import events
from . import gitsafe
from .errors import LayerError, StepFailed

GIT_TIMEOUT = 120
_HEX8 = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}(?![0-9a-fA-F])")
_PLACEHOLDER = re.compile(r"\{[^}]*\}")


# ---------------------------------------------------------------- what a step is

def may_commit(layer: Any, command_name: str) -> bool:
    """True for a command (not a hop) whose template is `commit` or that declares #:may-commit #t.
    An IR compiled before the field existed has no `mayCommit`: only the commit template may."""
    try:
        command = layer.command(command_name)
    except LayerError:
        return False
    if command.get("isHop"):
        return False
    return command.get("template") == "commit" or bool(command.get("mayCommit", False))


def scratch_dir(layer: Any, state: Any) -> Path:
    """agents/<run id>/scratch: where a step's notes, scratch files and helper scripts go."""
    path = Path(layer.layer_dir) / "agents" / state.adw_id / "scratch"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _running(state: Any) -> str:
    """The step running now (the rule of steps.running_step; not imported, to avoid a cycle)."""
    return next((str(r.get("name")) for r in reversed(state.steps) if r.get("status") == "running"), "")


def _phrase(layer: Any, step: dict[str, Any]) -> str:
    kind, name = str(step.get("kind") or ""), str(step.get("name") or "")
    if kind == "run":
        if may_commit(layer, str(step.get("command") or "")):
            return f"`{name}` commits"
        return f"`{name}` is another agent step on this worktree"
    return {"script": f"`{name}` runs a script", "review": f"`{name}` judges", "ship": f"`{name}` delivers",
            "fan-out": f"`{name}` races candidate runs"}.get(kind, f"`{name}` runs")


def workflow_steps(layer: Any, state: Any) -> str:
    """The ${workflow_steps} text: the workflow's steps with this one marked, and what follows it."""
    from .launch import COMMAND_PREFIX   # launch imports agents, which imports this module

    adw_name = str(getattr(state, "adw", "") or "")
    if adw_name.startswith(COMMAND_PREFIX):
        name = adw_name[len(COMMAND_PREFIX):]
        return (f"This is a single-command run: `{name}` is its only step. Nothing after it checks, commits or "
                "ships the work; a person takes the worktree as it is.")
    try:
        adw = layer.adw(adw_name)
    except (LayerError, AttributeError):
        return ""
    klass = str(getattr(state, "klass", "") or "")
    listed = [s for s in adw.get("steps") or []
              if isinstance(s, dict) and str(s.get("when") or "always") in ("always", f"class-{klass}")]
    current = _running(state)
    names = [str(s.get("name")) for s in listed]
    shown = [f"**{n} (this step)**" if n == current else n for n in names]
    line1 = f"Workflow `{adw_name}` (class {klass}): " + " → ".join(shown)
    after = listed[names.index(current) + 1:] if current in names else []
    phrases: list[str] = []
    checks = [s for s in after if s.get("kind") == "check"]
    for step in after:
        if step.get("kind") == "check":
            if step is checks[0]:
                suites = list(dict.fromkeys(str(s.get("cmd") or s.get("name")) for s in checks))
                phrases.append(f"the checks run the suites ({', '.join(suites)})")
            continue
        phrases.append(_phrase(layer, step))
    line2 = ("After you: " + "; ".join(phrases) + "." if phrases
             else "After you: nothing; the run ends and a person takes the worktree as it is.")
    return f"{line1}\n\n{line2}"


def run_state_entries(layer: Any) -> set[str]:
    """Top-level names of the layer's run state (mirrors Generator.gitignore() in tools/al)."""
    ir = layer.ir
    names = {"agents", "artefacts", "specs", "docs", "trees", "experts", ".al-improve"}
    for option in ir.get("environments") or []:
        if isinstance(option, dict) and option.get("kind") == "worktree":
            names.add(str(option.get("dir") or "trees"))
    storage = ir.get("storage") or {}
    if isinstance(storage, dict):
        names.add(str(storage.get("localDir") or storage.get("dir") or "artefacts"))
    for option in ir.get("promptInputs") or []:
        if not isinstance(option, dict):
            continue
        if option.get("kind") == "tasklist" and option.get("path"):
            names.add(str(option["path"]).strip("/").split("/")[0])
        if option.get("kind") == "spec-file" and option.get("dir"):
            names.add(str(option["dir"]).strip("/").split("/")[0])
    return {name.strip("/") for name in names if name}


# ---------------------------------------------------------------- git, sandboxed or not

def _sandbox(env_stack: Any) -> Any:
    return getattr(env_stack, "sandbox", None)


def _git_work(layer: Any, state: Any, env_stack: Any, args: list[str], step: str,
              check: bool = False) -> subprocess.CompletedProcess[str]:
    """git in the run's worktree: through gitsafe when sandboxed, else `git -C <worktree>`."""
    sb = _sandbox(env_stack)
    if sb is not None:
        return gitsafe.run(sb, args, step, check=check)
    from .agents import work_dir

    return _git_plain(work_dir(layer, state), args, step, check)


def _git_app(layer: Any, env_stack: Any, args: list[str], step: str) -> subprocess.CompletedProcess[str]:
    sb = _sandbox(env_stack)
    if sb is not None:
        return gitsafe.run_app(sb, args, step, check=False)
    return _git_plain(Path(layer.app_root), args, step, False)


def _git_plain(where: Path, args: list[str], step: str, check: bool) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(["git", "-C", str(where), *args], capture_output=True, text=True,
                                timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        if check:
            raise StepFailed(step, f"git {' '.join(args)} failed: {exc}") from exc
        return subprocess.CompletedProcess(args, 1, "", str(exc))
    if check and result.returncode != 0:
        raise StepFailed(step, f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}")
    return result


# ---------------------------------------------------------------- before and after

@dataclass
class Snapshot:
    head: str = ""
    head_ref: str = ""                       # symbolic ref of HEAD, "" when detached
    refs: dict[str, str] = field(default_factory=dict)
    main_status: list[str] = field(default_factory=list)
    layer_entries: set[str] = field(default_factory=set)


def _under(path: str, prefix: str) -> bool:
    prefix = prefix.strip("/")
    return bool(prefix) and (path == prefix or path.startswith(prefix + "/"))


def _status_path(line: str) -> str:
    path = line[3:]
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip('"')


def snapshot(layer: Any, state: Any, env_stack: Any, step: str = "") -> Snapshot:
    snap = Snapshot()
    head = _git_work(layer, state, env_stack, ["rev-parse", "HEAD"], step)
    snap.head = head.stdout.strip() if head.returncode == 0 else ""
    ref = _git_work(layer, state, env_stack, ["symbolic-ref", "-q", "HEAD"], step)
    snap.head_ref = ref.stdout.strip() if ref.returncode == 0 else ""
    refs = _git_app(layer, env_stack, ["for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags"],
                    step)
    for line in refs.stdout.splitlines() if refs.returncode == 0 else []:
        name, _, sha = line.partition(" ")
        if name:
            snap.refs[name] = sha.strip()
    if not getattr(env_stack, "agent_inside", False):
        status = _git_plain(Path(layer.app_root), ["status", "--porcelain=v1", "-b", "--untracked-files=normal"],
                            step, False)
        in_repo = str(getattr(layer, "in_repo_dir", "") or "")
        snap.main_status = [line for line in status.stdout.splitlines()
                            if line and not (in_repo and not line.startswith("##") and _under(_status_path(line), in_repo))]
    with contextlib.suppress(OSError):
        snap.layer_entries = {p.name for p in Path(layer.layer_dir).iterdir()}
    return snap


def _short(sha: str) -> str:
    return sha[:7] if sha else "(none)"


def _ignored_ref(layer: Any, state: Any, ref: str) -> bool:
    """Refs other runs move while this step runs: other runs' branches (an 8-hex token that is not
    this run's id), the default branch, al/ship/* and every declared stage branch."""
    if any(token.lower() != state.adw_id.lower() for token in _HEX8.findall(ref)):
        return True
    if not ref.startswith("refs/heads/"):
        return False
    branch = ref[len("refs/heads/"):]
    if branch in (layer.default_branch, getattr(layer, "codebase_default_branch", "")) or branch.startswith("al/ship/"):
        return True
    for stage in layer.ir.get("stages") or []:
        pattern = str((stage or {}).get("branch") or "")
        if pattern and re.fullmatch(".+".join(re.escape(part) for part in _PLACEHOLDER.split(pattern)), branch):
            return True
    return False


def _is_ancestor(layer: Any, state: Any, env_stack: Any, older: str, newer: str, step: str) -> bool:
    if not older or not newer:
        return False
    return _git_work(layer, state, env_stack, ["merge-base", "--is-ancestor", older, newer], step).returncode == 0


def compare(layer: Any, state: Any, step_name: str, before: Snapshot, after: Snapshot,
            env_stack: Any = None) -> list[dict[str, str]]:
    """The violations of step `step_name`: [{kind, text}] (empty when it stayed in its step)."""
    violations: list[dict[str, str]] = []
    if after.head != before.head:
        if _is_ancestor(layer, state, env_stack, before.head, after.head, step_name):
            violations.append({"kind": "committed", "text": f"the {step_name} step committed: HEAD moved from "
                                                            f"{_short(before.head)} to {_short(after.head)}"})
        else:
            violations.append({"kind": "head-moved", "text": f"the {step_name} step moved HEAD from "
                                                             f"{_short(before.head)} to {_short(after.head)}"})
    if after.head_ref != before.head_ref:
        violations.append({"kind": "head-moved", "text": f"the {step_name} step switched from "
                                                         f"{before.head_ref or '(detached)'} to {after.head_ref or '(detached)'}"})
    own = f"refs/heads/{state.branch}" if getattr(state, "branch", "") else ""
    for ref in sorted(set(before.refs) | set(after.refs)):
        old, new = before.refs.get(ref), after.refs.get(ref)
        if old == new or _ignored_ref(layer, state, ref):
            continue
        if ref == own and new == after.head:
            continue  # the run's own branch followed HEAD: reported as `committed`
        # The repository is shared: people and other sessions create, move and delete branches while
        # this step runs. A ref change is this step's doing only when it is the run's own branch, or
        # when the ref now points at work that grew out of this run's HEAD (a commit made here).
        if ref != own and not (new and new != before.head
                               and _is_ancestor(layer, state, env_stack, before.head, new, step_name)):
            continue
        if old is None:
            violations.append({"kind": "ref-created", "text": f"the {step_name} step created ref {ref} at {_short(new or '')}"})
        elif new is None:
            violations.append({"kind": "ref-deleted", "text": f"the {step_name} step deleted ref {ref}"})
        else:
            violations.append({"kind": "ref-moved", "text": f"the {step_name} step moved ref {ref} from "
                                                            f"{_short(old)} to {_short(new)}"})
    if after.main_status != before.main_status:
        new_lines = [line for line in after.main_status if line not in before.main_status]
        gone = [line for line in before.main_status if line not in after.main_status]
        parts = ([f"new: {', '.join(new_lines)}"] if new_lines else []) + ([f"vanished: {', '.join(gone)}"] if gone else [])
        violations.append({"kind": "main-checkout-changed",
                           "text": f"the {step_name} step changed the main checkout ({'; '.join(parts)})"})
    for name in sorted(after.layer_entries - before.layer_entries - run_state_entries(layer)):
        violations.append({"kind": "layer-entry", "text": f"the {step_name} step left {name} at the top of the layer directory"})
    return violations


def restore(layer: Any, state: Any, env_stack: Any, step_name: str, before: Snapshot,
            violations: list[dict[str, str]], after: Snapshot | None = None) -> tuple[list[str], list[str]]:
    """Undo what can be undone safely. Returns (restored, not_restored)."""
    restored: list[str] = []
    not_restored: list[str] = []
    kinds = {v["kind"] for v in violations}
    if kinds & {"committed", "head-moved"}:
        if not getattr(state, "worktree", ""):
            not_restored.append("HEAD: the main checkout is never touched")
        elif not before.head:
            not_restored.append("HEAD: its value before the step is unknown")
        else:
            sandboxed = _sandbox(env_stack) is not None
            ok = True
            if after is not None and after.head_ref != before.head_ref and before.head_ref and not sandboxed:
                ok = _git_work(layer, state, env_stack, ["symbolic-ref", "HEAD", before.head_ref], step_name).returncode == 0
            if ok:
                ok = _git_work(layer, state, env_stack, ["reset", "-q", before.head], step_name).returncode == 0
            if ok:
                restored.append(f"HEAD reset to {_short(before.head)}; the changes stay in the worktree, uncommitted")
            else:
                not_restored.append(f"HEAD: could not reset it to {_short(before.head)}")
    entries = [v["text"].split(" left ", 1)[1].rsplit(" at the top of the layer directory", 1)[0]
               for v in violations if v["kind"] == "layer-entry"]
    if entries:
        stray = scratch_dir(layer, state) / "stray"
        stray.mkdir(parents=True, exist_ok=True)
        for name in entries:
            source = Path(layer.layer_dir) / name
            if not source.exists() and not source.is_symlink():
                continue
            target, n = stray / name, 1
            while target.exists() or target.is_symlink():
                n += 1
                target = stray / f"{name}.{n}"
            try:
                shutil.move(str(source), str(target))
            except OSError as exc:
                not_restored.append(f"{name}: could not move it ({exc})")
                continue
            restored.append(f"moved {name} to agents/{state.adw_id}/scratch/stray/{target.name}")
    for violation in violations:
        if violation["kind"].startswith("ref-"):
            ref = violation["text"].split(" ref ", 1)[1].split(" ", 1)[0]
            not_restored.append(f"ref {ref}: left for a person")
    if "main-checkout-changed" in kinds:
        not_restored.append("main checkout: left for a person")
    return restored, not_restored


def _record(before: Snapshot, after: Snapshot, violations: list[dict[str, str]], restored: list[str],
            not_restored: list[str]) -> dict[str, Any]:
    return {"ok": not violations, "head_before": before.head, "head_after": after.head,
            "branch_before": before.head_ref, "branch_after": after.head_ref,
            "refs": {"created": sorted(set(after.refs) - set(before.refs)),
                     "moved": sorted(r for r in set(after.refs) & set(before.refs) if after.refs[r] != before.refs[r]),
                     "deleted": sorted(set(before.refs) - set(after.refs))},
            "main_checkout": [line for line in after.main_status if line not in before.main_status],
            "layer_entries": sorted(after.layer_entries - before.layer_entries),
            "violations": [v["kind"] for v in violations], "texts": [v["text"] for v in violations],
            "restored": restored, "not_restored": not_restored}


@contextlib.contextmanager
def watched(layer: Any, state: Any, step: dict[str, Any], env_stack: Any) -> Iterator[None]:
    """Check a `run` step: a step that may commit is cleaned up after; every other one is compared."""
    name = str(step.get("name"))
    if may_commit(layer, str(step.get("command") or "")):
        yield
        clean_after_commit(layer, state, name, env_stack)
        return
    before = snapshot(layer, state, env_stack, name)
    raised: BaseException | None = None
    try:
        yield
    except StepFailed as exc:
        raised = exc
    try:
        after = snapshot(layer, state, env_stack, name)
    except StepFailed:
        if raised is not None:
            raise raised   # e.g. the worktree's git metadata was tampered with: the step's own failure comes first
        raise
    violations = compare(layer, state, name, before, after, env_stack)
    restored, not_restored = restore(layer, state, env_stack, name, before, violations, after) if violations else ([], [])
    record = _record(before, after, violations, restored, not_restored)
    state.step_record(name, "run")["discipline"] = record
    state.save(layer.layer_dir)
    events.emit("step.discipline", step=name, **record)
    if raised is not None:
        raise raised
    if violations:
        texts = "; ".join(v["text"] for v in violations)
        raise StepFailed(name, f"step-discipline: {texts}. This is a step-discipline violation, not a test failure. "
                               f"Restored: {'; '.join(restored) or 'nothing'}. "
                               f"Not restored: {'; '.join(not_restored) or 'nothing'}.")


# ---------------------------------------------------------------- after the commit step

def _porcelain_z(text: str) -> list[tuple[str, str, str]]:
    """(XY, path, old path of a rename) from `status --porcelain=v1 -z`."""
    parts = text.split("\0")
    entries: list[tuple[str, str, str]] = []
    i = 0
    while i < len(parts):
        item = parts[i]
        i += 1
        if len(item) < 4:
            continue
        xy, path, old = item[:2], item[3:], ""
        if xy[0] in "RC":
            old = parts[i] if i < len(parts) else ""
            i += 1
        entries.append((xy, path, old))
    return entries


def clean_after_commit(layer: Any, state: Any, step_name: str, env_stack: Any) -> dict[str, Any]:
    """Restore what the commit step left uncommitted under the in-repo layer directory (unless the
    ADW changes the layer), so a regenerated layer does not count as the run's change."""
    from .review import is_protected

    prefixes: list[str] = []
    in_repo = str(getattr(layer, "in_repo_dir", "") or "")
    try:
        changes_layer = layer.changes_layer(state.adw)
    except (LayerError, AttributeError):
        changes_layer = False
    if in_repo and not changes_layer:
        prefixes.append(in_repo)
    # Only the layer's own generated files are put back. A change an agent made under a protected path
    # of the codebase is left where it is: the gate must see it and hand the run to a person. Putting
    # it back would drop the change silently and let the rest ship without anyone knowing.
    copied = set()
    for option in layer.ir.get("environments") or []:
        if isinstance(option, dict) and option.get("kind") == "worktree":
            copied |= {str(p).strip("/") for p in option.get("copyFiles") or []}
    status = _git_work(layer, state, env_stack, ["status", "--porcelain=v1", "-z", "--untracked-files=all"], step_name)
    entries = _porcelain_z(status.stdout) if status.returncode == 0 else []
    from .agents import work_dir

    work = work_dir(layer, state)
    saved = scratch_dir(layer, state) / "left-out"
    restored: list[str] = []
    still: list[str] = []
    todo: list[tuple[str, bool]] = []      # (path, added)
    for xy, path, old in entries:
        todo.append((path, xy == "??" or xy[0] in "AR" or xy[0] == "C"))
        if old:
            todo.append((old, False))
    for path, added in todo:
        if path.strip("/") in copied:
            continue
        if not prefixes or not is_protected(path, prefixes):
            still.append(path)
            continue
        source = work / path
        if source.is_file():
            target = saved / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                shutil.copy2(source, target)
        if added:
            ok = _git_work(layer, state, env_stack, ["rm", "-q", "--cached", "--ignore-unmatch", "--", path],
                           step_name).returncode == 0
            with contextlib.suppress(OSError):
                if source.is_dir() and not source.is_symlink():
                    shutil.rmtree(source)
                elif source.exists() or source.is_symlink():
                    source.unlink()
        else:
            ok = _git_work(layer, state, env_stack, ["restore", "--source=HEAD", "--staged", "--worktree", "--", path],
                           step_name).returncode == 0
        (restored if ok else still).append(path)
    record = {"restored": restored, "saved_to": f"agents/{state.adw_id}/scratch/left-out", "still_uncommitted": still}
    state.step_record(step_name, "run")["left_out"] = record
    state.save(layer.layer_dir)
    events.emit("step.discipline", step=step_name, ok=True, left_out_restored=restored)
    if restored:
        print(f"[adw {state.adw_id}] step {step_name}: restored {len(restored)} uncommitted path(s) the commit "
              f"left out: {', '.join(restored)}")
    return record
