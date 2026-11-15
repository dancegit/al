from __future__ import annotations

import contextlib
import re
import subprocess
import textwrap
from pathlib import Path
from typing import Any

from . import envs, gitsafe, review, steps
from .errors import LayerError, StepFailed

GIT_TIMEOUT = 300
IDENTITY = ["-c", "user.name=adw", "-c", "user.email=adw@localhost"]


def _git(repo: Path, args: list[str], sandbox: gitsafe.Sandbox | None = None) -> str:
    """git -C <repo> …, raising StepFailed with stderr on any failure. With a sandbox, the
    worktree is only ever touched through gitsafe (integrity check + pinned GIT_DIR)."""
    if sandbox is not None:
        return gitsafe.run(sandbox, args, "ship", timeout=GIT_TIMEOUT).stdout
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                                timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StepFailed("ship", f"git {' '.join(args)} failed: {exc}") from exc
    if result.returncode != 0:
        raise StepFailed("ship", f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def _ref_exists(repo: Path, ref: str, sandbox: gitsafe.Sandbox | None = None) -> bool:
    if sandbox is not None:
        return gitsafe.run_app(sandbox, ["rev-parse", "--verify", "--quiet", ref], "ship", check=False).returncode == 0
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
                            capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False)
    return result.returncode == 0


def _title(state: Any) -> str:
    first = next((line.strip() for line in state.prompt.splitlines() if line.strip()), "adw changes")
    return f"{state.klass}: {first}"


def commit_pending(worktree: Path, state: Any, never_commit: list[str] | None = None,
                   sandbox: gitsafe.Sandbox | None = None, message: str = "") -> str:
    """Commit anything uncommitted in the worktree; returns HEAD. `never_commit` lists files
    copied in from the app (copyFiles, e.g. .env): they are unstaged again so they cannot ship."""
    _git(worktree, ["add", "-A"], sandbox)
    for relative in never_commit or []:
        if sandbox is not None:
            gitsafe.run(sandbox, ["reset", "-q", "--", relative], "ship", check=False)
        else:
            subprocess.run(["git", "-C", str(worktree), "reset", "-q", "--", relative], capture_output=True,
                           text=True, timeout=GIT_TIMEOUT, check=False)
    if _git(worktree, ["diff", "--cached", "--name-only"], sandbox).strip():
        _git(worktree, [*IDENTITY, "commit", "-m", message or f"{_title(state)} [adw {state.adw_id}]"], sandbox)
    return _git(worktree, ["rev-parse", "HEAD"], sandbox).strip()


def _merge(worktree: Path, ref: str, sandbox: gitsafe.Sandbox | None = None) -> None:
    """Merge `ref` into the worktree branch; on conflict abort so the worktree stays clean."""
    try:
        _git(worktree, [*IDENTITY, "merge", "--no-edit", ref], sandbox)
    except StepFailed:
        if sandbox is not None:
            gitsafe.run(sandbox, ["merge", "--abort"], "ship", check=False)
        else:
            subprocess.run(["git", "-C", str(worktree), "merge", "--abort"], capture_output=True, text=True,
                           timeout=GIT_TIMEOUT, check=False)
        raise


def _sections(state: Any) -> dict[str, str]:
    """The standard sections of a run's pull request description, by name."""
    verdict = state.review.get("verdict", {})
    checks = "\n".join(f"- `{name}`: exit {data.get('exit')} — log `{data.get('log', '')}`"
                       for name, data in state.checks.items()) or "- none"
    issues = "\n".join(f"- **{issue['severity']}** {issue['description']} → {issue['resolution']}"
                       for issue in verdict.get("issues", [])) or "- none"
    links = "\n".join(f"- {item.get('id')} ({item.get('type')}): {item.get('url')}" for item in state.artefacts) or "- none"
    reasons = "\n".join(f"- {reason}" for reason in state.ship.get("gate", {}).get("reasons", [])) or "- none"
    return {
        "summary": f"## Summary\n\n{state.prompt.strip()}\n\nADW `{state.adw}` run `{state.adw_id}`, class `{state.klass}`, branch `{state.branch}`.",
        "plan": f"## Plan\n\n`{state.plan or 'none'}`",
        "checks": f"## Checks\n\n{checks}",
        "review": f"## Review verdict\n\n{'PASS' if verdict.get('success') else 'FAIL'} ({verdict.get('mode', 'n/a')}): {verdict.get('summary', '')}\n\n{issues}",
        "why": f"## Why a human is needed\n\n{reasons}",
        "artefacts": f"## Artefacts\n\n{links}",
    }


def pr_body(state: Any, message: str) -> str:
    sections = _sections(state)
    parts = [sections[name] for name in ("summary", "plan", "checks", "review", "why", "artefacts")]
    if message:
        parts.append(f"## Notes\n\n{message}")
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------- proof on the pull request

MAX_PROOF_IMAGES = 8


def _spread(items: list[Any], count: int) -> list[Any]:
    """`count` evenly spaced items, first and last included."""
    if count <= 0:
        return []
    if len(items) <= count:
        return list(items)
    if count == 1:
        return [items[len(items) // 2]]
    return [items[round(index * (len(items) - 1) / (count - 1))] for index in range(count)]


def proof_items(artefacts: list[dict[str, Any]], limit: int = MAX_PROOF_IMAGES) -> list[dict[str, str]]:
    """What the Proof comment shows: screenshots inline, each video as a link plus its keyframes
    inline. At most `limit` inline images; with a video, up to half the slots go to its
    keyframes (evenly spread) and slots one kind leaves unused go to the other."""
    images = [item for item in artefacts if item.get("type") == "image" and item.get("path")]
    videos = [item for item in artefacts if item.get("type") == "video" and item.get("path")]
    frames = [(video, frame) for video in videos for frame in video.get("keyframes") or [] if frame.get("path")]
    shots = images[: max(0, limit - min(len(frames), limit // 2))]
    frames = _spread(frames, limit - len(shots))
    items = [{"kind": "image", "path": str(item["path"]), "caption": Path(str(item["path"])).name} for item in shots]
    for video in videos:
        name = Path(str(video["path"])).name
        items.append({"kind": "video", "path": str(video["path"]), "caption": name})
        items += [{"kind": "keyframe", "path": str(frame["path"]),
                   "caption": f"{Path(str(frame['path'])).name} (keyframe of {name})"}
                  for owner, frame in frames if owner is video]
    return items


def proof_comment(adw_id: str, rows: list[dict[str, str]]) -> str:
    """Markdown of the Proof comment; `rows` are proof_items with the attachment `url` ("" = failed)."""
    def image(row: dict[str, str]) -> str:
        if not row["url"]:
            return f"{row['caption']} (could not be attached)"
        return f"**{row['caption']}**\n\n![{row['caption']}]({row['url']})"

    parts = [f"Proof from run `{adw_id}`: what the run's checks captured (screenshots and video keyframes, "
             "taken in a real browser)."]
    shots = [row for row in rows if row["kind"] == "image"]
    if shots:
        parts.append("### Screenshots")
        parts += [image(row) for row in shots]
    videos = [row for row in rows if row["kind"] == "video"]
    if videos:
        parts.append("### Video")
    for video in videos:
        link = f"[{video['caption']}]({video['url']})" if video["url"] else f"{video['caption']} (could not be attached)"
        frames = [row for row in rows if row["kind"] == "keyframe" and row["caption"].endswith(f"(keyframe of {video['caption']})")]
        parts.append(f"{link}" + (" — keyframes:" if frames else ""))
        parts += [image(row) for row in frames]
    return "\n\n".join(parts) + "\n"


def post_proof(layer: Any, state: Any, client: Any, pr: dict[str, Any]) -> dict[str, Any]:
    """Attach the run's image/video artefacts to the PR and post ONE Proof comment embedding them.
    Never raises: a forge that cannot host files, or any failure, is a warning in state.ship["proof"]."""
    result: dict[str, Any] = {"posted": False, "attached": 0, "warnings": []}
    items = proof_items(list(getattr(state, "artefacts", []) or []))
    if not getattr(client, "can_attach", False) or not items:
        # GitHub / none: no file hosting, so the PR body's artefact list stays the only reference
        result["reason"] = "forge does not support attachments" if items else "no image or video artefacts"
        state.ship["proof"] = result
        return result

    def warn(text: str) -> None:
        result["warnings"].append(text)
        print(f"[adw {state.adw_id}] warning: proof: {text}")

    try:
        # The files belong to the Proof comment (listed under it, not under the PR description):
        # post it, attach to it, then edit it so it embeds what was attached.
        comment_id = client.comment(pr["number"], f"Proof from run `{state.adw_id}`: attaching {len(items)} file(s)…")
        if comment_id is None:
            raise RuntimeError("the forge returned no comment id")
        rows = []
        for item in items:
            try:
                url = client.attach(pr["number"], Path(layer.layer_dir) / item["path"], comment_id=comment_id)
            except Exception as exc:  # noqa: BLE001 — one file the forge refuses must not lose the rest
                warn(f"could not attach {Path(item['path']).name}: {_short_reason(exc)}")
                url = ""
            result["attached"] += 1 if url else 0
            rows.append({**item, "url": url})
        client.edit_comment(comment_id, proof_comment(state.adw_id, rows))
        result["posted"] = True
        print(f"[adw {state.adw_id}] ship: proof comment on PR #{pr['number']} ({result['attached']} file(s) attached)")
    except Exception as exc:  # noqa: BLE001 — proof is a courtesy to the reviewer; shipping goes on
        warn(f"proof comment failed: {_short_reason(exc)}")
    state.ship["proof"] = result
    return result


def _forge_client(layer: Any) -> Any | None:
    if layer.singleton("forge")["kind"] == "none":
        return None
    from . import forge as forge_module  # written by the forge integration; imported only when needed

    return forge_module.get_client(layer)


def _push_branch(state: Any, worktree: Path, sandbox: gitsafe.Sandbox | None, step: str = "ship") -> None:
    if sandbox is not None:  # pushed from the app repository: the host published refs/heads/<branch>
        gitsafe.run_app(sandbox, ["push", "--set-upstream", "origin",
                                  f"refs/heads/{state.branch}:refs/heads/{state.branch}"], step)
    else:
        _git(worktree, ["push", "--set-upstream", "origin", f"HEAD:refs/heads/{state.branch}"])


def _open_pr(layer: Any, state: Any, client: Any, worktree: Path, body: str,
             sandbox: gitsafe.Sandbox | None = None) -> dict[str, Any]:
    option = layer.option("reviews", layer.adw(state.adw)["review"])
    _push_branch(state, worktree, sandbox)
    # draft / requested reviewers exist only on `human` reviews; agent/auto `reviewers` is a count of review agents.
    human = option["kind"] == "human"
    pr = client.create_pr(state.branch, layer.default_branch, _title(state), body,
                          draft=bool(option.get("draft")) if human else False,
                          reviewers=tuple(option.get("reviewers", [])) if human else ())
    if not isinstance(pr, dict) or not isinstance(pr.get("number"), int):
        raise StepFailed("ship", f"forge create_pr returned an unexpected value: {pr!r}")
    print(f"[adw {state.adw_id}] ship: PR #{pr['number']} {pr.get('url', '')}")
    return pr


def _default_checked_out(app: Path, default: str, sandbox: gitsafe.Sandbox | None = None) -> bool:
    if sandbox is not None:
        listing = gitsafe.run_app(sandbox, ["worktree", "list", "--porcelain"], "ship").stdout
    else:
        listing = _git(app, ["worktree", "list", "--porcelain"])
    return any(line.strip() == f"branch refs/heads/{default}" for line in listing.splitlines())


def _merge_via_remote(worktree: Path, remote: str, default: str, sandbox: gitsafe.Sandbox | None = None,
                      commit: str = "") -> str:
    # the remote's default branch was already merged in by _integrate before the gate
    if sandbox is not None:
        gitsafe.run_app(sandbox, ["push", remote, f"{commit}:refs/heads/{default}"], "ship")
    else:
        _git(worktree, ["push", remote, f"HEAD:refs/heads/{default}"])
    return f"{remote}/{default}"


def _merge_locally(layer: Any, state: Any, worktree: Path, sandbox: gitsafe.Sandbox | None = None,
                   commit: str = "") -> tuple[bool, str]:
    """Pure-local merge. Never checks anything out in the app's main working tree."""
    default = layer.default_branch  # already merged in by _integrate before the gate
    if not _default_checked_out(Path(layer.app_root), default, sandbox):
        if sandbox is not None:
            gitsafe.update_ref(sandbox, f"refs/heads/{default}", commit, "ship", f"adw {state.adw_id}: ship")
        else:
            _git(worktree, ["push", ".", f"HEAD:refs/heads/{default}"])  # fast-forward only; git refuses otherwise
        return True, default
    staging = f"refs/heads/al/ship/{default}"
    if sandbox is not None:
        gitsafe.update_ref(sandbox, staging, commit, "ship", f"adw {state.adw_id}: ship")
    else:
        _git(worktree, ["push", ".", f"HEAD:{staging}"])
    state.ship["note"] = (f"{default} is checked out in a working tree, so the result is on al/ship/{default}; "
                          f"run `git merge --ff-only al/ship/{default}` in the app to take it")
    print(f"[adw {state.adw_id}] ship: {state.ship['note']}")
    return False, f"al/ship/{default}"


def _integrate(layer: Any, worktree: Path, client: Any, sandbox: gitsafe.Sandbox | None = None) -> str:
    """Merge the branch the result will land on into HEAD and return that ref, so the gate
    judges the tree that actually ships (not the pre-merge tree). Raises StepFailed on conflict."""
    default = layer.default_branch
    remote = "origin" if client is not None else str(layer.singleton("forge").get("remote") or "")
    if remote:
        # sandboxed: fetched objects land in the quarantine, so no remote-tracking ref may point at them
        _git(worktree, ["fetch", *(["--refmap="] if sandbox else []), remote, default], sandbox)
        base = _git(worktree, ["rev-parse", "FETCH_HEAD"], sandbox).strip()
        _merge(worktree, base, sandbox)
        return base
    # sandboxed: full ref names, so nothing in the container-writable admin dir can shadow them
    local = f"refs/heads/{default}" if sandbox else default
    _merge(worktree, local, sandbox)
    staging = f"refs/heads/al/ship/{default}"
    if _default_checked_out(Path(layer.app_root), default, sandbox) and _ref_exists(worktree, staging, sandbox):
        _merge(worktree, staging, sandbox)  # stack on earlier unmerged shipments instead of clobbering them
        return staging
    return local


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
    sandbox = getattr(env_stack, "sandbox", None)
    base, extra, shipping = None, [], ""
    # Sandboxed: the container stays frozen for this whole block, and the commit judged by the
    # gate and shipped is pinned here; its objects move into the app now, so the gate and every
    # ref update read them from the app's store, which the container cannot write.
    with gitsafe.session(sandbox) if sandbox is not None else contextlib.nullcontext():
        commit_pending(worktree, state, _never_commit(layer, state), sandbox)
        client = _forge_client(layer)
        option = layer.option("reviews", layer.adw(state.adw)["review"])
        if option["kind"] == "auto":
            reviewed = state.review.get("tree")
            if reviewed and reviewed != _git(worktree, ["rev-parse", "HEAD^{tree}"], sandbox).strip():
                extra.append("changed-after-review")  # code was modified after the verdict was given
            try:
                base = _integrate(layer, worktree, client, sandbox)
            except StepFailed as exc:
                extra.append(f"merge-conflict:{exc}"[:200])
        if sandbox is not None:
            shipping = gitsafe.import_objects(sandbox, "HEAD", "ship")
    decision = review.gate(layer, state, env_stack, base, extra, head=shipping or None)
    if sandbox is not None:  # the run's branch exists only from here on, created by the host
        envs.check_run_branch(state.branch, state.adw_id)
        gitsafe.update_ref(sandbox, f"refs/heads/{state.branch}", shipping, "ship", f"adw {state.adw_id}: run branch")
    action = decision["action"]
    reasons = list(decision["reasons"])
    message = str(step.get("message") or "")
    body = pr_body(state, message)
    pr: dict[str, Any] | None = None
    shipped, target = False, ""
    if action == "pr":
        pr, target = _human_path(layer, state, client, worktree, body, sandbox)
    elif action in {"merge", "deploy"}:
        try:
            if client is not None:
                pr = _open_pr(layer, state, client, worktree, body, sandbox)
                post_proof(layer, state, client, pr)  # before the merge, so the proof sits on the PR's history
                client.merge_pr(pr["number"])
                shipped, target = True, f"{layer.default_branch} via PR #{pr['number']}"
            elif layer.singleton("forge").get("remote"):
                shipped, target = True, _merge_via_remote(worktree, str(layer.singleton("forge")["remote"]),
                                                          layer.default_branch, sandbox, shipping)
            else:
                shipped, target = _merge_locally(layer, state, worktree, sandbox, shipping)
        except Exception as exc:  # noqa: BLE001 — every failure after the decision has the same safe answer
            if isinstance(exc, StepFailed) and exc.step == gitsafe.INTEGRITY:
                raise  # a tampered sandbox is never papered over with a PR
            reason = _short_reason(exc)
            print(f"[adw {state.adw_id}] ship: {action} failed ({reason}); falling back to a human PR")
            note = (f"The gate decided `{action}`, but shipping failed: {reason}\n\n"
                    f"Nothing was merged or deployed. The run's commits are on `{state.branch}`.")
            pr, target = _fallback(layer, state, client, worktree, sandbox, pr_body(state, "\n\n".join(
                part for part in (message, note) if part)), pr, note, exc)
            state.ship["fallback"] = {"decided": action, "error": str(exc)[:2000]}
            if action == "deploy":
                state.ship["deploy"] = {"skipped": "not deployed: shipping fell back to a human PR"}
            action, shipped = "pr", False
            reasons.append(f"ship-failed:{reason}")
        else:
            if action == "deploy" and shipped:
                _deploy(layer, state, env_stack)  # a failing deploy after a successful merge is a real failure
            elif action == "deploy":
                state.ship["deploy"] = {"skipped": f"not deployed: change is staged on {target}, not yet on {layer.default_branch}"}
                print(f"[adw {state.adw_id}] ship: {state.ship['deploy']['skipped']}")
    elif action != "none":
        raise LayerError(f"Unknown ship action: {action!r}")
    commit = shipping or _git(worktree, ["rev-parse", "HEAD"]).strip()
    state.ship.update({"action": action, "shipped": shipped, "target": target, "commit": commit,
                       "pr": pr, "reasons": reasons})
    state.save(layer.layer_dir)
    return state.ship


def _short_reason(exc: BaseException | str, limit: int = 160) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _human_path(layer: Any, state: Any, client: Any, worktree: Path, body: str,
                sandbox: gitsafe.Sandbox | None) -> tuple[dict[str, Any] | None, str]:
    """A human decides: a PR on the forge, or PR.md in the run's directory with the branch kept."""
    if client is not None:
        pr = _open_pr(layer, state, client, worktree, body, sandbox)
        post_proof(layer, state, client, pr)
        return pr, str(pr.get("url", ""))
    path = Path(layer.layer_dir) / "agents" / state.adw_id / "PR.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {_title(state)}\n\n{body}", encoding="utf-8")
    target = path.relative_to(layer.layer_dir).as_posix()
    print(f"[adw {state.adw_id}] ship: no forge; PR description written to {target}, branch {state.branch} kept")
    return None, target


def _fallback(layer: Any, state: Any, client: Any, worktree: Path, sandbox: gitsafe.Sandbox | None, body: str,
              pr: dict[str, Any] | None, note: str, cause: BaseException) -> tuple[dict[str, Any] | None, str]:
    """Shipping failed after the gate said merge/deploy: hand the run to a human instead. A PR
    that was already opened stays open (with a note); otherwise one is opened, or PR.md written.
    The run's commits stay on refs/heads/<branch> either way (sandboxed: published by the host
    before the decision was acted on; otherwise the worktree's own branch). Only when even this
    fails is the ship step a failure."""
    try:
        if pr is not None:
            try:
                client.comment(pr["number"], note)
            except Exception as exc:  # noqa: BLE001 — the note is a courtesy; the open PR is what matters
                print(f"[adw {state.adw_id}] ship: could not comment on PR #{pr['number']}: {exc}")
            print(f"[adw {state.adw_id}] ship: PR #{pr['number']} left open for a human")
            return pr, str(pr.get("url", ""))
        return _human_path(layer, state, client, worktree, body, sandbox)
    except Exception as exc:  # noqa: BLE001
        raise StepFailed("ship", f"shipping failed ({_short_reason(cause)}) and the fallback to a human PR "
                                 f"failed too: {_short_reason(exc)}") from exc


# ---------------------------------------------------------------- handoff of a stopped run
#
# A run that stops (a check still failing after its resolve attempts, a failing agent or review
# step) has often done most of the work, and may have stopped precisely because the rest needs a
# person (a protected path). Ending it as "failed" with the work in a local worktree hides it from
# everyone on the forge. So the runner hands it over: the work is committed and published on the
# run's branch, and a pull request marked `[needs human]` (PR.md without a forge) says why the run
# stopped and what the agent asks a human to do. The run still ends `failed`.

HANDOFF_TITLE = "[needs human]"
MARKER = "REQUIRES HUMAN"
LOG_TAIL_LINES = 30
MAX_REQUESTS = 30
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")


def _fence_blocks(lines: list[str]) -> list[tuple[int, int]]:
    """(first, last) line index of every fenced code block; an unclosed fence runs to the end."""
    blocks, index = [], 0
    while index < len(lines):
        opening = _FENCE.match(lines[index])
        if not opening:
            index += 1
            continue
        token, end = opening.group(1), len(lines) - 1
        for later in range(index + 1, len(lines)):
            closing = lines[later].strip()
            if closing.startswith(token) and set(closing) == {token[0]}:
                end = later
                break
        blocks.append((index, end))
        index = end + 1
    return blocks


def human_requests(text: str) -> list[str]:
    """Every paragraph (or list item) of markdown `text` that contains REQUIRES HUMAN, with the
    fenced code block it introduces (the next block, at most one blank line below it), so exact
    contents such as SQL travel with the request. A marker inside a code block yields the block."""
    lines = text.splitlines()
    blocks = _fence_blocks(lines)

    def block_at(index: int) -> tuple[int, int] | None:
        return next((block for block in blocks if block[0] <= index <= block[1]), None)

    def boundary(line: str) -> bool:
        return not line.strip() or bool(_ITEM.match(line) or _HEADING.match(line) or _FENCE.match(line))

    found, taken = [], -1
    for index, line in enumerate(lines):
        if index <= taken or MARKER not in line:
            continue
        inside = block_at(index)
        if inside:
            start, end = inside
        else:
            start = index
            while (start > 0 and not _ITEM.match(lines[start]) and not boundary(lines[start - 1])
                   and not block_at(start - 1)):
                start -= 1
            end = index
            while end + 1 < len(lines) and not boundary(lines[end + 1]):
                end += 1
            following = end + 1 + int(end + 1 < len(lines) and not lines[end + 1].strip())
            introduced = block_at(following) if following < len(lines) else None
            if introduced and introduced[0] == following:
                end = introduced[1]
        found.append(textwrap.dedent("\n".join(lines[start:end + 1])).strip("\n"))
        taken = end
    return found


def _layer_relative(layer: Any, path: Path) -> str:
    path = path if path.is_absolute() else Path(layer.layer_dir) / path
    try:
        return path.resolve().relative_to(Path(layer.layer_dir).resolve()).as_posix()
    except ValueError:
        return str(path)


def _request_sources(layer: Any, state: Any) -> list[tuple[str, str]]:
    """(label, text) to search for REQUIRES HUMAN: the run's plan file, then every agent output."""
    sources: list[tuple[str, str]] = []
    if state.plan:
        plan = Path(state.plan)
        plan = plan if plan.is_absolute() else Path(layer.layer_dir) / plan
        if plan.is_file():
            sources.append((f"the plan (`{_layer_relative(layer, plan)}`)",
                            plan.read_text(encoding="utf-8", errors="replace")))
    for name, output in (state.outputs or {}).items():
        if isinstance(output, str) and output.strip() != str(state.plan).strip():
            sources.append((f"step `{name}`", output))
    return sources


def _fenced(text: str, info: str = "text") -> str:
    longest = max((len(run) for run in re.findall(r"`{3,}", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{info}\n{text}\n{fence}"


def stopped_at(state: Any, adw: dict[str, Any], failure: BaseException) -> tuple[str, str, str]:
    """(step name, step kind, reason) of the step that stopped the run."""
    kinds = {str(step.get("name")): str(step.get("kind", "")) for step in adw.get("steps", [])}
    for name in kinds:
        record = next((item for item in state.steps if item.get("name") == name and item.get("status") == "failed"), None)
        if record is not None:
            detail = str(record.get("detail") or failure)
            return name, kinds[name], detail.removeprefix(f"{name}: ")
    if isinstance(failure, StepFailed):
        return failure.step, "", failure.reason
    return "run", "", f"{type(failure).__name__}: {failure}"


def handoff_body(layer: Any, state: Any, step: str, kind: str, reason: str) -> str:
    """Why this stopped · What the agent asks a human to do · then the usual sections."""
    stopped = [f"The run stopped at step `{step}`{f' ({kind})' if kind else ''}: {_short_reason(reason, 300)}",
               f"Nothing was merged or deployed. The work so far is on branch `{state.branch}`; the run ended `failed`."]
    log = str(state.checks.get(step, {}).get("log") or "") if kind == "check" else ""
    if log and (Path(layer.layer_dir) / log).is_file():
        tail = (Path(layer.layer_dir) / log).read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_TAIL_LINES:]
        stopped.append(f"Last {len(tail)} lines of `{log}`:\n\n" + _fenced("\n".join(tail)))
    asks: list[str] = []
    seen: set[str] = set()
    total = 0
    for label, text in _request_sources(layer, state):
        items = []
        for item in human_requests(text):
            key = " ".join(item.split())
            if key in seen:
                continue
            seen.add(key)
            total += 1
            if total <= MAX_REQUESTS:
                items.append(item)
        if items:
            asks.append(f"From {label}:\n\n" + "\n\n".join(items))
    if total > MAX_REQUESTS:
        asks.append(f"({total - MAX_REQUESTS} more `{MARKER}` item(s) are in the run's records.)")
    if not asks:
        asks = [f"No `{MARKER}` line in the plan or in the agents' reports. Start from why the run stopped, above."]
    sections = _sections(state)
    if state.plan:  # relative to the layer: a local absolute path means nothing to a reader on the forge
        sections["plan"] = f"## Plan\n\n`{_layer_relative(layer, Path(state.plan))}`"
    parts = ["## Why this stopped\n\n" + "\n\n".join(stopped),
             "## What the agent asks a human to do\n\n" + "\n\n".join(asks),
             sections["summary"], sections["plan"], sections["checks"]]
    if state.review.get("verdict"):
        parts.append(sections["review"])
    parts.append(sections["artefacts"])
    return "\n\n".join(parts) + "\n"


def _skip(state: Any, why: str) -> None:
    state.ship["handoff_skipped"] = why
    print(f"[adw {state.adw_id}] handoff: none: {why}")


def handoff(layer: Any, state: Any, adw: dict[str, Any], env_stack: Any, failure: BaseException) -> dict[str, Any] | None:
    """Hand the work of a stopped run to a human. Never raises: a handoff that fails is a
    warning in state.ship["handoff_error"] and the original failure stands. Never merges, never
    deploys. Not for: ADWs without a ship step (they do not publish), a failing ship step (it has
    its own fallback to a human PR), git-integrity failures (a tampered sandbox is left for
    inspection), runs that changed nothing. In --dry-run a real forge is not used: PR.md only."""
    if not any(item.get("kind") == "ship" for item in adw.get("steps", [])):
        return None
    step, kind, reason = stopped_at(state, adw, failure)
    if kind == "ship":
        return None
    if (isinstance(failure, StepFailed) and failure.step == gitsafe.INTEGRITY) or reason.startswith(f"{gitsafe.INTEGRITY}:"):
        _skip(state, f"{gitsafe.INTEGRITY} failure: the worktree is left for inspection, never turned into a PR")
        return None
    if env_stack.read_only or not state.worktree or not Path(state.worktree).is_dir():
        _skip(state, "the run has no worktree: nothing to hand over")
        return None
    worktree = Path(state.worktree)
    sandbox = getattr(env_stack, "sandbox", None)
    reasons = [f"run-stopped:{step}: {_short_reason(reason)}"]
    title = f"{HANDOFF_TITLE} {_title(state)}"
    try:
        # Sandboxed: the container stays frozen and the git metadata verified while the host
        # commits, and the run's branch is created host-side from imported objects only.
        with gitsafe.session(sandbox) if sandbox is not None else contextlib.nullcontext():
            commit = commit_pending(worktree, state, _never_commit(layer, state), sandbox,
                                    message=f"{_title(state)} (stopped at {step}; needs a human) [adw {state.adw_id}]")
            changed = _git(worktree, ["diff", "--name-only", f"refs/heads/{layer.default_branch}...HEAD"], sandbox)
            if not changed.strip():
                _skip(state, f"nothing changed relative to {layer.default_branch}: nothing to hand over")
                return None
            if sandbox is not None:
                commit = envs.publish_run_branch(sandbox, state.branch, state.adw_id, "handoff")
    except Exception as exc:  # noqa: BLE001 — the handoff must never mask the failure that stopped the run
        return _handoff_failed(state, exc, {"action": "handoff", "shipped": False, "reasons": reasons,
                                            "pr": None, "target": "", "branch": state.branch})
    state.ship.update({"action": "handoff", "shipped": False, "reasons": reasons, "pr": None, "target": "",
                       "commit": commit, "branch": state.branch, "stopped_at": step})
    try:
        body = handoff_body(layer, state, step, kind, reason)
        client = _forge_client(layer)
        if client is not None and state.dry_run:
            print(f"[adw {state.adw_id}] handoff: dry run, so no pull request on the forge; writing PR.md")
            client = None
        if client is None:
            path = Path(layer.layer_dir) / "agents" / state.adw_id / "PR.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {title}\n\n{body}", encoding="utf-8")
            state.ship["target"] = path.relative_to(layer.layer_dir).as_posix()
            print(f"[adw {state.adw_id}] handoff: {state.ship['target']} written, branch {state.branch} kept")
        else:
            _push_branch(state, worktree, sandbox, "handoff")
            try:
                pr = client.create_pr(state.branch, layer.default_branch, title, body, draft=True, reviewers=())
            except Exception:  # noqa: BLE001 — GitHub refuses drafts on some plans: a plain PR then
                if getattr(client, "kind", "") != "github":
                    raise
                pr = client.create_pr(state.branch, layer.default_branch, title, body, draft=False, reviewers=())
            if not isinstance(pr, dict) or not isinstance(pr.get("number"), int):
                raise StepFailed("handoff", f"forge create_pr returned an unexpected value: {pr!r}")
            url = str(pr.get("url", ""))
            state.ship.update({"pr": pr, "target": url, "url": url})
            print(f"[adw {state.adw_id}] handoff: PR #{pr['number']} {url} needs a human")
            post_proof(layer, state, client, pr)
    except Exception as exc:  # noqa: BLE001
        return _handoff_failed(state, exc, {})
    state.save(layer.layer_dir)
    return state.ship


def _handoff_failed(state: Any, exc: BaseException, record: dict[str, Any]) -> dict[str, Any]:
    state.ship.update(record)
    state.ship["handoff_error"] = str(exc)[:2000] or type(exc).__name__
    print(f"[adw {state.adw_id}] warning: handoff failed, the work stays on branch {state.branch or '(none)'}"
          f" and in the worktree: {_short_reason(exc)}")
    return state.ship
