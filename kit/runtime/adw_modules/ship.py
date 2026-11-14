from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from . import review, steps
from .errors import LayerError, StepFailed

GIT_TIMEOUT = 300
IDENTITY = ["-c", "user.name=adw", "-c", "user.email=adw@localhost"]


def _git(repo: Path, args: list[str]) -> str:
    """git -C <repo> …, raising StepFailed with stderr on any failure."""
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                                timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StepFailed("ship", f"git {' '.join(args)} failed: {exc}") from exc
    if result.returncode != 0:
        raise StepFailed("ship", f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def _ref_exists(repo: Path, ref: str) -> bool:
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
                            capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False)
    return result.returncode == 0


def _title(state: Any) -> str:
    first = next((line.strip() for line in state.prompt.splitlines() if line.strip()), "adw changes")
    return f"{state.klass}: {first}"


def commit_pending(worktree: Path, state: Any, never_commit: list[str] | None = None) -> str:
    """Commit anything uncommitted in the worktree; returns HEAD. `never_commit` lists files
    copied in from the app (copyFiles, e.g. .env): they are unstaged again so they cannot ship."""
    _git(worktree, ["add", "-A"])
    for relative in never_commit or []:
        subprocess.run(["git", "-C", str(worktree), "reset", "-q", "--", relative], capture_output=True,
                       text=True, timeout=GIT_TIMEOUT, check=False)
    if _git(worktree, ["diff", "--cached", "--name-only"]).strip():
        _git(worktree, [*IDENTITY, "commit", "-m", f"{_title(state)} [adw {state.adw_id}]"])
    return _git(worktree, ["rev-parse", "HEAD"]).strip()


def _merge(worktree: Path, ref: str) -> None:
    """Merge `ref` into the worktree branch; on conflict abort so the worktree stays clean."""
    try:
        _git(worktree, [*IDENTITY, "merge", "--no-edit", ref])
    except StepFailed:
        subprocess.run(["git", "-C", str(worktree), "merge", "--abort"], capture_output=True, text=True,
                       timeout=GIT_TIMEOUT, check=False)
        raise


def pr_body(state: Any, message: str) -> str:
    verdict = state.review.get("verdict", {})
    checks = "\n".join(f"- `{name}`: exit {data.get('exit')} — log `{data.get('log', '')}`"
                       for name, data in state.checks.items()) or "- none"
    issues = "\n".join(f"- **{issue['severity']}** {issue['description']} → {issue['resolution']}"
                       for issue in verdict.get("issues", [])) or "- none"
    links = "\n".join(f"- {item.get('id')} ({item.get('type')}): {item.get('url')}" for item in state.artefacts) or "- none"
    reasons = "\n".join(f"- {reason}" for reason in state.ship.get("gate", {}).get("reasons", [])) or "- none"
    parts = [
        f"## Summary\n\n{state.prompt.strip()}\n\nADW `{state.adw}` run `{state.adw_id}`, class `{state.klass}`, branch `{state.branch}`.",
        f"## Plan\n\n`{state.plan or 'none'}`",
        f"## Checks\n\n{checks}",
        f"## Review verdict\n\n{'PASS' if verdict.get('success') else 'FAIL'} ({verdict.get('mode', 'n/a')}): {verdict.get('summary', '')}\n\n{issues}",
        f"## Why a human is needed\n\n{reasons}",
        f"## Artefacts\n\n{links}",
    ]
    if message:
        parts.append(f"## Notes\n\n{message}")
    return "\n\n".join(parts) + "\n"


def _forge_client(layer: Any) -> Any | None:
    if layer.singleton("forge")["kind"] == "none":
        return None
    from . import forge as forge_module  # written by the forge integration; imported only when needed

    return forge_module.get_client(layer)


def _open_pr(layer: Any, state: Any, client: Any, worktree: Path, body: str) -> dict[str, Any]:
    option = layer.option("reviews", layer.adw(state.adw)["review"])
    _git(worktree, ["push", "--set-upstream", "origin", f"HEAD:refs/heads/{state.branch}"])
    # draft / requested reviewers exist only on `human` reviews; agent/auto `reviewers` is a count of review agents.
    human = option["kind"] == "human"
    pr = client.create_pr(state.branch, layer.default_branch, _title(state), body,
                          draft=bool(option.get("draft")) if human else False,
                          reviewers=tuple(option.get("reviewers", [])) if human else ())
    if not isinstance(pr, dict) or not isinstance(pr.get("number"), int):
        raise StepFailed("ship", f"forge create_pr returned an unexpected value: {pr!r}")
    print(f"[adw {state.adw_id}] ship: PR #{pr['number']} {pr.get('url', '')}")
    return pr


def _default_checked_out(app: Path, default: str) -> bool:
    listing = _git(app, ["worktree", "list", "--porcelain"])
    return any(line.strip() == f"branch refs/heads/{default}" for line in listing.splitlines())


def _merge_via_remote(worktree: Path, remote: str, default: str) -> str:
    # the remote's default branch was already merged in by _integrate before the gate
    _git(worktree, ["push", remote, f"HEAD:refs/heads/{default}"])
    return f"{remote}/{default}"


def _merge_locally(layer: Any, state: Any, worktree: Path) -> tuple[bool, str]:
    """Pure-local merge. Never checks anything out in the app's main working tree."""
    default = layer.default_branch  # already merged in by _integrate before the gate
    if not _default_checked_out(Path(layer.app_root), default):
        _git(worktree, ["push", ".", f"HEAD:refs/heads/{default}"])  # fast-forward only; git refuses otherwise
        return True, default
    staging = f"refs/heads/al/ship/{default}"
    _git(worktree, ["push", ".", f"HEAD:{staging}"])
    state.ship["note"] = (f"{default} is checked out in a working tree, so the result is on al/ship/{default}; "
                          f"run `git merge --ff-only al/ship/{default}` in the app to take it")
    print(f"[adw {state.adw_id}] ship: {state.ship['note']}")
    return False, f"al/ship/{default}"


def _integrate(layer: Any, worktree: Path, client: Any) -> str:
    """Merge the branch the result will land on into HEAD and return that ref, so the gate
    judges the tree that actually ships (not the pre-merge tree). Raises StepFailed on conflict."""
    default = layer.default_branch
    remote = "origin" if client is not None else str(layer.singleton("forge").get("remote") or "")
    if remote:
        _git(worktree, ["fetch", remote, default])
        base = _git(worktree, ["rev-parse", "FETCH_HEAD"]).strip()
        _merge(worktree, base)
        return base
    _merge(worktree, default)
    staging = f"refs/heads/al/ship/{default}"
    if _default_checked_out(Path(layer.app_root), default) and _ref_exists(worktree, staging):
        _merge(worktree, staging)  # stack on earlier unmerged shipments instead of clobbering them
        return staging
    return default


def _never_commit(layer: Any, state: Any) -> list[str]:
    files: list[str] = []
    for option_id in layer.adw(state.adw)["environment"]:
        option = layer.option("environments", option_id)
        if option["kind"] == "worktree":
            files += list(option.get("copyFiles", []))
    return files


def _deploy(layer: Any, state: Any, env_stack: Any) -> None:
    option = layer.option("reviews", layer.adw(state.adw)["review"])
    if not option.get("deployCmd"):
        raise StepFailed("ship", "ship = deploy but the review option has no deployCmd")
    command = layer.cmd(str(option["deployCmd"]))
    code, output = steps.run_command(env_stack.wrap(str(command["run"])), env_stack.cwd,
                                     env_stack.process_env(), int(command.get("timeout", 900)))
    log = Path(layer.layer_dir) / "agents" / state.adw_id / "checks" / "deploy.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(output, encoding="utf-8")
    state.ship["deploy"] = {"cmd": command["run"], "exit": code, "log": log.relative_to(layer.layer_dir).as_posix()}
    if code != 0:
        raise StepFailed("ship", f"deploy command {command['name']!r} exited {code}")


def ship(layer: Any, state: Any, adw: dict[str, Any], step: dict[str, Any], env_stack: Any) -> dict[str, Any]:
    """Commit, gate, then open a PR / merge / deploy, all from inside the worktree."""
    del adw  # the ADW is re-read from state so review and ship agree on the option
    if env_stack.read_only or not state.worktree:
        raise StepFailed("ship", "shipping needs a worktree environment")
    worktree = Path(state.worktree)
    commit_pending(worktree, state, _never_commit(layer, state))
    client = _forge_client(layer)
    option = layer.option("reviews", layer.adw(state.adw)["review"])
    base, extra = None, []
    if option["kind"] == "auto":
        reviewed = state.review.get("tree")
        if reviewed and reviewed != _git(worktree, ["rev-parse", "HEAD^{tree}"]).strip():
            extra.append("changed-after-review")  # code was modified after the verdict was given
        try:
            base = _integrate(layer, worktree, client)
        except StepFailed as exc:
            extra.append(f"merge-conflict:{exc}"[:200])
    decision = review.gate(layer, state, env_stack, base, extra)
    action = decision["action"]
    body = pr_body(state, str(step.get("message") or ""))
    pr: dict[str, Any] | None = None
    shipped, target = False, ""
    if action == "pr":
        if client is not None:
            pr = _open_pr(layer, state, client, worktree, body)
            target = str(pr.get("url", ""))
        else:
            path = Path(layer.layer_dir) / "agents" / state.adw_id / "PR.md"
            path.write_text(f"# {_title(state)}\n\n{body}", encoding="utf-8")
            target = path.relative_to(layer.layer_dir).as_posix()
            print(f"[adw {state.adw_id}] ship: no forge; PR description written to {target}, branch {state.branch} kept")
    elif action in {"merge", "deploy"}:
        if client is not None:
            pr = _open_pr(layer, state, client, worktree, body)
            client.merge_pr(pr["number"])
            shipped, target = True, f"{layer.default_branch} via PR #{pr['number']}"
        elif layer.singleton("forge").get("remote"):
            shipped, target = True, _merge_via_remote(worktree, str(layer.singleton("forge")["remote"]), layer.default_branch)
        else:
            shipped, target = _merge_locally(layer, state, worktree)
        if action == "deploy" and shipped:
            _deploy(layer, state, env_stack)
        elif action == "deploy":
            state.ship["deploy"] = {"skipped": f"not deployed: change is staged on {target}, not yet on {layer.default_branch}"}
            print(f"[adw {state.adw_id}] ship: {state.ship['deploy']['skipped']}")
    elif action != "none":
        raise LayerError(f"Unknown ship action: {action!r}")
    commit = _git(worktree, ["rev-parse", "HEAD"]).strip()
    state.ship.update({"action": action, "shipped": shipped, "target": target, "commit": commit,
                       "pr": pr, "reasons": list(decision["reasons"])})
    state.save(layer.layer_dir)
    return state.ship
