from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import budget, discipline, events, gitsafe, models, slots, storage, usage
from .errors import LayerError, StepFailed
from .templates import render

CLASSES = ("chore", "bug", "feature", "patch")
SEVERITIES = ("blocker", "tech_debt", "skippable")
ISSUE_KINDS = ("fixable", "needs-environment", "needs-declaration", "needs-person")  # convergence.KINDS
GIT_TIMEOUT = 120
_STRIPPED_ENV = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"}
_MOCK_KEYS = {"output", "files", "raw_files", "layer_files", "delete", "commit", "cost", "fail", "events"}
_FENCED_JSON = re.compile(r"```json\s*(.*?)```", re.IGNORECASE | re.DOTALL)


# ---------------------------------------------------------------- variables

def template_variables(layer: Any, state: Any) -> dict[str, str]:
    """The ${name} variables shared by command/hop templates and mock scripts."""
    return {
        "adw_id": state.adw_id,
        "layer_dir": str(layer.layer_dir),
        "specs_dir": str(Path(layer.layer_dir) / "specs"),
        "worktree": str(work_dir(layer, state)),
        "artefacts_dir": str(storage.artefacts_root(layer) / state.adw_id),
        "branch": state.branch,
        "default_branch": layer.default_branch,
        "class": state.klass,
        "prompt": state.prompt,
        "plan": state.plan,
        "workflow_steps": discipline.workflow_steps(layer, state),
        "scratch_dir": str(discipline.scratch_dir(layer, state)),
        "approvals": ", ".join(str(item.get("what")) for item in getattr(state, "approvals", None) or []) or "none",
        "proof_dir": ".al-proof",
    }


def work_dir(layer: Any, state: Any) -> Path:
    """The worktree of the run, or the app root for host-only stacks."""
    return Path(state.worktree).resolve() if state.worktree else Path(layer.app_root)


def step_dir(layer: Any, state: Any, step_name: str) -> Path:
    path = Path(layer.layer_dir) / "agents" / state.adw_id / step_name
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------- subprocess agents

def _kill_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return  # already exited between the timeout and the kill


def _reap(process: subprocess.Popen[Any]) -> None:
    """Collect a killed child without letting a second signal or a slow exit mask the first error."""
    try:
        process.wait(timeout=10)
    except BaseException:  # noqa: BLE001 — best effort; the caller re-raises what stopped it
        pass


LEFTOVER_GRACE_SECONDS = 30.0
GRACE_ENV = "AL_AGENT_GRACE_SECONDS"
CALL_TAG_ENV = "AL_AGENT_CALL"   # every process of one agent call carries it, also one that called setsid


def grace_seconds() -> float:
    """How long an agent CLI may run on after its final `result` event (AL_AGENT_GRACE_SECONDS, default 30)."""
    try:
        value = float(os.environ.get(GRACE_ENV, ""))
    except ValueError:
        return LEFTOVER_GRACE_SECONDS
    return value if value >= 0 and value == value and value != float("inf") else LEFTOVER_GRACE_SECONDS


def _feed_stdin(process: subprocess.Popen[str], text: str) -> None:
    """Write the prompt and close stdin from a thread, so a prompt larger than the pipe never blocks the wait."""
    def feed() -> None:
        stdin = process.stdin
        if stdin is None:
            return
        try:
            stdin.write(text)
        except (BrokenPipeError, OSError):
            pass
        try:
            stdin.close()
        except (BrokenPipeError, OSError):
            pass
    threading.Thread(target=feed, name="al-agent-stdin", daemon=True).start()


def _has_result(chunk: bytes) -> bool:
    for line in chunk.splitlines():
        if b'"result"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return True
    return False


def _cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()[:200]


def _host_leftovers(process: subprocess.Popen[Any], tag: str) -> list[int]:
    """Pids on this machine that carry the call's tag or are in the CLI's process group, minus the CLI."""
    marker = f"{CALL_TAG_ENV}={tag}".encode()
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == process.pid or pid == os.getpid():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            group = int(stat[stat.rindex(")") + 2:].split()[2])
        except (OSError, ValueError, IndexError):
            continue
        hit = group == process.pid
        if not hit and tag:
            try:
                hit = marker in (entry / "environ").read_bytes().split(b"\0")
            except OSError:
                hit = False
        if hit:
            found.append(pid)
    return found


_CONTAINER_SWEEP = (
    "for d in /proc/[0-9]*; do p=${d#/proc/}; [ \"$p\" = \"$$\" ] && continue; "
    "if tr '\\0' '\\n' < \"$d/environ\" 2>/dev/null | grep -qx '%s'; then "
    "c=$(tr '\\0' ' ' < \"$d/cmdline\" 2>/dev/null | cut -c1-200); "
    "printf '%%s\\t%%s\\n' \"$p\" \"$c\"; kill -KILL \"$p\" 2>/dev/null; fi; done; true"
)


def _stop_leftovers(process: subprocess.Popen[Any], tag: str, env_stack: Any) -> list[dict[str, Any]]:
    """Stop every process of an agent call: in the run's container (by tag), on the host (by tag and by
    process group), then the CLI and its group. → one {pid, command, where} per process stopped."""
    stopped: list[dict[str, Any]] = []
    if tag and getattr(env_stack, "agent_inside", False):
        try:
            done = subprocess.run(env_stack.wrap(_CONTAINER_SWEEP % f"{CALL_TAG_ENV}={tag}"), capture_output=True,
                                  text=True, timeout=30, check=False)
            for line in done.stdout.splitlines():
                pid, _, command = line.partition("\t")
                if pid.strip().isdigit():
                    stopped.append({"pid": int(pid), "command": command.strip()[:200], "where": "container"})
        except Exception as exc:  # noqa: BLE001 — stopping leftovers never fails a finished step
            print(f"[agent] could not stop leftover commands in the container: {exc}", file=sys.stderr, flush=True)
    for pid in _host_leftovers(process, tag):
        command = _cmdline(pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            continue
        stopped.append({"pid": pid, "command": command, "where": "host"})
    _kill_group(process)
    return stopped


def _run_process(argv: list[str], *, cwd: Path, env: dict[str, str], stdin_text: str,
                 timeout: int, step: str, raw_path: Path, result_tag: str = "", env_stack: Any = None) -> str:
    """Run an agent CLI in its own process group, streaming stdout to raw_path; returns stdout.

    With `result_tag` (a stream-json CLI whose processes carry that tag), the call also ends when the
    CLI has written its final `result` event and has not exited a grace period later: the runtime then
    stops it and whatever it left running, and records `agent.leftovers`."""
    stderr_path = raw_path.with_name("stderr.log")
    leftovers = False
    with raw_path.open("w", encoding="utf-8") as raw, stderr_path.open("w", encoding="utf-8") as err:
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=raw,
                                       stderr=err, text=True, start_new_session=True)
        except OSError as exc:
            raise StepFailed(step, f"could not start agent {argv[0]!r}: {exc}") from exc
        try:
            _feed_stdin(process, stdin_text)
            deadline = time.monotonic() + max(1, timeout)
            grace = grace_seconds()
            offset, pending, finished_at = 0, b"", None
            while True:
                try:
                    process.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    pass
                now = time.monotonic()
                if now >= deadline:
                    _kill_group(process)
                    process.wait(timeout=30)
                    raise StepFailed(step, f"agent timed out after {timeout}s (process group killed)")
                if not result_tag:
                    continue
                if finished_at is None:
                    try:
                        with raw_path.open("rb") as handle:
                            handle.seek(offset)
                            chunk = handle.read()
                    except OSError:
                        chunk = b""
                    offset += len(chunk)
                    complete, _, pending = (pending + chunk).rpartition(b"\n")
                    if complete and _has_result(complete):
                        finished_at = now
                elif now - finished_at >= grace:
                    stopped = _stop_leftovers(process, result_tag, env_stack)
                    _reap(process)
                    events.emit("agent.leftovers", step=step, grace=grace, stopped=stopped)
                    leftovers = True
                    break
        except BaseException:  # the run was stopped (RunStopped) or interrupted: never leave the agent running
            _kill_group(process)
            _reap(process)
            raise
    stdout = raw_path.read_text(encoding="utf-8", errors="replace")
    if not leftovers and process.returncode != 0:   # a CLI we stopped after a finished turn is not a failure
        detail = (stderr_path.read_text(encoding="utf-8", errors="replace") or stdout)[-1000:]
        raise StepFailed(step, f"agent exited {process.returncode}: {detail.strip()}")
    return stdout


def parse_stream_json_result(stdout: str, step: str) -> str:
    """Final text of a `claude -p --output-format stream-json` run: the last result event."""
    last: dict[str, Any] | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # stream-json may interleave partial/non-JSON diagnostics; only result events matter
        if isinstance(event, dict) and event.get("type") == "result":
            last = event
    if last is None:
        raise StepFailed(step, "claude-code output had no result event")
    if last.get("is_error") is True:
        raise StepFailed(step, f"claude-code reported is_error: {str(last.get('result', ''))[:500]}")
    result = last.get("result")
    if not isinstance(result, str):
        raise StepFailed(step, "claude-code result event has no string result")
    return result


ISOLATED_ENV = "AL_AGENT_ISOLATED"   # "1" (set by `al improve`): no personal settings, hooks, CLAUDE.md or plugins
ISOLATED_ARGV = ["--setting-sources", "project", "--strict-mcp-config"]
CREDENTIALS = ".credentials.json"


def isolated() -> bool:
    return os.environ.get(ISOLATED_ENV) == "1"


def clean_config_dir(directory: Path) -> Path:
    """`directory`/claude-config: an empty Claude Code configuration folder that holds only a link to the
    account's credentials file (when there is one), so the CLI logs in but reads no personal setup."""
    config = Path(directory) / "claude-config"
    if config.is_symlink() or config.is_file():
        config.unlink()
    elif config.is_dir():
        shutil.rmtree(config)
    config.mkdir(parents=True)
    credentials = Path.home() / ".claude" / CREDENTIALS
    if credentials.is_file():
        (config / CREDENTIALS).symlink_to(credentials)
    return config


def _claude_env(env_stack: Any, step_directory: Path | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _STRIPPED_ENV}
    env.update(env_stack.env)
    if isolated() and step_directory is not None:
        env["CLAUDE_CONFIG_DIR"] = str(clean_config_dir(step_directory))
    return env


LOGIN_REFUSED = ("authentication_error", "OAuth token revoked", '"api_error_status":401')
LOGIN_RETRY_SECONDS = 15.0     # the shortest pause before another attempt
LOGIN_ATTEMPTS = 4
LOGIN_WAIT_SECONDS = 900.0     # how long to wait for the machine's login to be renewed before trying anyway


def _login_stamp() -> tuple[float, int] | None:
    """(mtime, size) of the agent CLI's credential file on this machine, None without one."""
    try:
        info = (Path.home() / ".claude" / CREDENTIALS).stat()
    except OSError:
        return None
    return info.st_mtime, info.st_size


def _wait_for_new_login(before: tuple[float, int] | None) -> float | None:
    """Wait until the credential file differs from `before` (it was renewed), at least
    LOGIN_RETRY_SECONDS and at most LOGIN_WAIT_SECONDS. → seconds waited, or None when it never changed."""
    started = time.monotonic()
    time.sleep(LOGIN_RETRY_SECONDS)
    while time.monotonic() - started < LOGIN_WAIT_SECONDS:
        if _login_stamp() != before:
            return time.monotonic() - started
        time.sleep(min(10.0, max(LOGIN_RETRY_SECONDS, 0.01)))
    return None


def claude_argv(layer: Any, agent: dict[str, Any], choice: models.Choice, cap: float | None) -> list[str]:
    """The `claude -p` command line. --effort and --max-budget-usd are the CLI's own flags. With
    AL_AGENT_ISOLATED=1 the account's settings, hooks and MCP servers are not loaded."""
    argv = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    if choice.model:
        argv += ["--model", choice.model]
    if choice.effort and choice.effort != "default":
        argv += ["--effort", choice.effort]
    if cap is not None:
        argv += ["--max-budget-usd", f"{max(cap, 0.0001):.4f}"]
    argv += ["--permission-mode", str(agent["permissionMode"]),
             "--max-turns", str(agent["maxTurns"]), "--add-dir", str(layer.layer_dir)]
    if agent.get("allowedTools"):
        argv += ["--allowedTools", ",".join(agent["allowedTools"])]
    if agent.get("safeMode"):
        argv.append("--safe-mode")
    if isolated():
        argv += ISOLATED_ARGV
    return argv


def _run_claude(layer: Any, agent: dict[str, Any], prompt: str, choice: models.Choice, cwd: Path,
                env_stack: Any, step: str, raw_path: Path, cap: float | None = None) -> str:
    # The prompt goes in on stdin: templates start with `---` front matter, which the CLI would
    # parse as an option, and long prompts would hit the argv size limit.
    tag = f"{step}-{uuid.uuid4().hex[:12]}"
    # `env` reaches the CLI on the host, under nix and inside the container alike
    argv = ["env", f"{CALL_TAG_ENV}={tag}", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1",
            *claude_argv(layer, agent, choice, cap)]
    for attempt in range(1, LOGIN_ATTEMPTS + 1):
        login_before = _login_stamp()
        try:
            stdout = _run_process(env_stack.wrap_agent(argv), cwd=cwd, env=_claude_env(env_stack, raw_path.parent),
                                  stdin_text=prompt,
                                  timeout=int(agent["timeout"]), step=step, raw_path=raw_path,
                                  result_tag=tag, env_stack=env_stack)
            return parse_stream_json_result(stdout, step)
        except StepFailed as exc:
            # The login was revoked while this call ran: the work is not wrong, the login is stale.
            # The machine's own login is renewed only when something on it next asks for one, which has
            # been seen to take ten minutes. So wait for the login on this machine to change, then try
            # again; give up after LOGIN_ATTEMPTS with the reason in plain words.
            if not any(mark in str(exc) for mark in LOGIN_REFUSED):
                raise
            if attempt == LOGIN_ATTEMPTS:
                raise StepFailed(step, f"the agent's login was refused {LOGIN_ATTEMPTS} times (authentication error): "
                                       "log in again on this machine with the agent CLI, then resume the run") from exc
            waited = _wait_for_new_login(login_before)
            print(f"[agent] {step}: the agent's login was refused; "
                  + (f"the login on this machine was renewed after {waited:.0f}s, trying again" if waited is not None
                     else f"no renewed login after {LOGIN_WAIT_SECONDS:.0f}s, trying again anyway"), flush=True)
    raise AssertionError("unreachable")


def codex_argv(agent: dict[str, Any], choice: models.Choice, cwd: Path) -> list[str]:
    """`codex exec`: it has no flag for reasoning effort, so effort goes through its config
    override `-c model_reasoning_effort=…`; it has no budget cap and reports no cost."""
    argv = ["codex", "exec"]
    if choice.model:
        argv += ["--model", choice.model]
    if choice.effort and choice.effort != "default":
        argv += ["-c", f"model_reasoning_effort={choice.effort}"]
    return argv + ["--sandbox", str(agent["sandbox"]), "--cd", str(cwd), "-"]


def _run_codex(agent: dict[str, Any], prompt: str, choice: models.Choice, cwd: Path, env_stack: Any,
               step: str, raw_path: Path) -> str:
    return _run_process(env_stack.wrap_agent(codex_argv(agent, choice, cwd)), cwd=cwd, env=env_stack.process_env(),
                        stdin_text=prompt, timeout=int(agent["timeout"]), step=step, raw_path=raw_path)


def _run_custom(agent: dict[str, Any], prompt: str, choice: models.Choice, cwd: Path, env_stack: Any,
                step: str, raw_path: Path) -> str:
    template = str(agent.get("command") or "")
    if not template:
        raise LayerError(f"custom agent {agent.get('id')!r} has an empty command")
    shell_command = template.replace("{model}", choice.model).replace("{cwd}", str(cwd))
    return _run_process(env_stack.wrap_agent(["bash", "-c", shell_command]), cwd=cwd,
                        env=env_stack.process_env(), stdin_text=prompt, timeout=int(agent["timeout"]),
                        step=step, raw_path=raw_path)


# ---------------------------------------------------------------- mock agent

def find_mock_script(layer: Any, state: Any, agent: dict[str, Any], step_name: str) -> Path | None:
    directory = Path(layer.layer_dir) / str(agent.get("scriptDir") or "mock")
    for candidate in (directory / state.adw / f"{step_name}.json", directory / f"{step_name}.json"):
        if candidate.is_file():
            return candidate
    return None


def _load_mock_script(script: Path, step: str) -> dict[str, Any]:
    try:
        data = json.loads(script.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StepFailed(step, f"invalid mock script {script}: {exc}") from exc
    if not isinstance(data, dict):
        raise StepFailed(step, f"mock script {script} must be a JSON object")
    unknown = set(data) - _MOCK_KEYS
    if unknown:
        raise StepFailed(step, f"mock script {script} has unknown keys {sorted(unknown)}")
    if not isinstance(data.get("output", ""), str):
        raise StepFailed(step, "mock output must be a string")
    for key in ("files", "raw_files", "layer_files"):
        mapping = data.get(key, {})
        if not isinstance(mapping, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
            raise StepFailed(step, f"mock {key} must map relative paths to string content")
    delete = data.get("delete", [])
    if not isinstance(delete, list) or not all(isinstance(item, str) for item in delete):
        raise StepFailed(step, "mock delete must be a list of relative paths")
    if not isinstance(data.get("commit", False), bool):
        raise StepFailed(step, "mock commit must be a boolean")
    cost = data.get("cost")
    if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0):
        raise StepFailed(step, "mock cost must be a number of US dollars >= 0")
    if not isinstance(data.get("fail", ""), str):
        raise StepFailed(step, "mock fail must be a string (the failure reason)")
    replay = data.get("events", [])
    if not isinstance(replay, list) or not all(isinstance(item, dict) for item in replay):
        raise StepFailed(step, "mock events must be a list of JSON objects (stream-json events)")
    return data


def _inside(root: Path, relative: str, step: str) -> Path:
    root = root.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or target == root:
        raise StepFailed(step, f"mock path {relative!r} escapes {root}")
    return target


def _write_files(root: Path, files: dict[str, str], variables: dict[str, str], args: list[str], step: str,
                 verbatim: bool = False) -> None:
    for relative, content in files.items():
        target = _inside(root, relative, step)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content if verbatim else render(content, variables, args), encoding="utf-8")


def _delete_paths(root: Path, paths: list[str], step: str) -> None:
    for relative in paths:
        target = _inside(root, relative, step)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()


def _git(worktree: Path, args: list[str], step: str) -> None:
    result = subprocess.run(["git", "-C", str(worktree), "-c", "user.name=adw", "-c", "user.email=adw@localhost", *args],
                            capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False)
    if result.returncode != 0:
        raise StepFailed(step, f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}")


@contextmanager
def _host_agent_guard(env_stack: Any) -> Iterator[None]:
    """An agent that runs on the HOST in a sandboxed run's worktree runs its own git there and
    trusts the worktree's `.git` file, which code in the container can rewrite. So before the
    agent starts, the git metadata is verified, and the container stays frozen until it is done."""
    sandbox = getattr(env_stack, "sandbox", None)
    if sandbox is None or getattr(env_stack, "agent_inside", False):
        yield
        return
    with gitsafe.session(sandbox):
        yield


def _commit_all(worktree: Path, step: str, env_stack: Any = None) -> None:
    sandbox = getattr(env_stack, "sandbox", None)
    if sandbox is not None:                      # sandboxed run: never trust the worktree's .git file
        gitsafe.run(sandbox, ["add", "-A"], step)
        if gitsafe.run(sandbox, ["status", "--porcelain"], step).stdout.strip():
            gitsafe.run(sandbox, ["-c", "user.name=adw", "-c", "user.email=adw@localhost", "commit", "-m", f"adw: {step}"], step)
        return
    _git(worktree, ["add", "-A"], step)
    status = subprocess.run(["git", "-C", str(worktree), "status", "--porcelain"], capture_output=True,
                            text=True, timeout=GIT_TIMEOUT, check=True)
    if status.stdout.strip():
        _git(worktree, ["commit", "-m", f"adw: {step}"], step)


def _synthesise(layer: Any, state: Any, step: str, command: dict[str, Any]) -> str:
    output = command.get("output")
    template = command.get("template")
    if output == "path":
        plan = Path(layer.layer_dir) / "specs" / f"plan-{state.adw_id}-{step}.md"
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text(f"# Plan ({step})\n\nMock plan for adw {state.adw_id}.\n\n## Task\n\n{state.prompt}\n",
                        encoding="utf-8")
        return str(plan)
    if output == "json" and template == "classify":
        return json.dumps({"class": "chore", "adw": "", "model_set": "base", "slug": "mock-task"})
    if output == "json" and template == "review":
        return json.dumps({"success": True, "summary": "mock review: no issues", "issues": []})
    if output == "json" and template == "decide":
        return json.dumps({"action": "ask-human", "summary": "mock decider: no decision", "explanation": "mock",
                           "instructions": "", "from_step": ""})
    if template == "resolve-conflicts":
        # a dry run without a mock script resolves nothing: markers that remain then fail the step
        report = json.dumps({"resolved": [], "regenerated": [], "undecidable": []})
        return f"mock: {step}: nothing resolved\n\n```json\n{report}\n```\n"
    return f"mock: {step} ok"


def _run_mock(layer: Any, state: Any, agent: dict[str, Any], step: str, command: dict[str, Any],
              args: list[str], raw_path: Path, env_stack: Any = None) -> str:
    script = find_mock_script(layer, state, agent, step)
    if script is None:
        output = _synthesise(layer, state, step, command)
        raw_path.write_text(json.dumps({"synthesised": True, "output": output}) + "\n", encoding="utf-8")
        return output
    data = _load_mock_script(script, step)
    lines = [json.dumps({"script": str(script), **{k: v for k, v in data.items() if k != "events"}})]
    # scripted stream-json events (e.g. the CLI's init and rate_limit_event lines) go in as they are
    lines += [json.dumps(event) for event in data.get("events", [])]
    if data.get("cost") is not None:
        # a scripted cost is reported the way Claude Code reports it: in a final `result` event
        lines.append(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                 "total_cost_usd": data["cost"], "num_turns": 1, "result": data.get("output", "")}))
    raw_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    variables = template_variables(layer, state)
    worktree = work_dir(layer, state)
    _write_files(worktree, data.get("files", {}), variables, args, step)
    _write_files(worktree, data.get("raw_files", {}), {}, [], step, verbatim=True)  # e.g. a template: no ${…} rendering
    _write_files(Path(layer.layer_dir), data.get("layer_files", {}), variables, args, step)
    _delete_paths(worktree, data.get("delete", []), step)
    if data.get("commit"):
        _commit_all(worktree, step, env_stack)
    if data.get("fail"):
        raise StepFailed(step, str(data["fail"]))
    return render(data.get("output", f"mock: {step} ok"), variables, args)


# ---------------------------------------------------------------- output handling

def extract_json(output: str, step: str) -> dict[str, Any]:
    """The JSON object of an agent's answer: the last ```json fenced block; when that does not parse,
    the text from the last ```json to the last fence (a string inside the object may itself contain
    a code fence, which ends the short match too early); else the outermost {...} of the output."""
    blocks = _FENCED_JSON.findall(output)
    candidates: list[str] = [blocks[-1].strip()] if blocks else []
    opening = max(output.rfind("```json"), output.rfind("```JSON"))
    closing = output.rfind("```")
    if 0 <= opening < closing:
        candidates.append(output[opening + len("```json"):closing].strip())
    candidates.append(output[output.find("{"): output.rfind("}") + 1])
    candidates = [candidate for candidate in dict.fromkeys(candidates) if candidate]
    if not candidates:
        raise StepFailed(step, "agent output contains no JSON object")
    first_error: json.JSONDecodeError | None = None
    value: Any = None
    for candidate in candidates:
        try:
            value = json.loads(candidate, strict=False)   # strict=False: a raw line break inside a string is accepted
            first_error = None
            break
        except json.JSONDecodeError as exc:
            first_error = first_error or exc
    if first_error is not None:
        exc = first_error
        raise StepFailed(step, f"agent output JSON is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise StepFailed(step, "agent output JSON must be an object")
    return value


def validate_review_verdict(obj: dict[str, Any], step: str) -> None:
    if not isinstance(obj.get("success"), bool):
        raise StepFailed(step, "review JSON requires boolean 'success'")
    if not isinstance(obj.get("summary"), str):
        raise StepFailed(step, "review JSON requires string 'summary'")
    issues = obj.get("issues")
    if not isinstance(issues, list):
        raise StepFailed(step, "review JSON requires list 'issues'")
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("severity") not in SEVERITIES:
            raise StepFailed(step, f"review issue needs severity in {SEVERITIES}: {issue!r}")
        if not isinstance(issue.get("description"), str) or not isinstance(issue.get("resolution"), str):
            raise StepFailed(step, f"review issue needs string description and resolution: {issue!r}")
        if "criterion" in issue and not isinstance(issue["criterion"], str):
            raise StepFailed(step, f"review issue criterion must be a string: {issue!r}")
        if "kind" in issue and issue["kind"] not in ISSUE_KINDS:
            raise StepFailed(step, f"review issue kind must be one of {ISSUE_KINDS}: {issue!r}")


def _validate_json(obj: dict[str, Any], template: str, step: str) -> None:
    if template == "classify" and obj.get("class") not in CLASSES:
        raise StepFailed(step, f"classify JSON requires 'class' in {CLASSES}, got {obj.get('class')!r}")
    if template == "review":
        validate_review_verdict(obj, step)


def _resolve_path_output(layer: Any, state: Any, output: str, step: str) -> str:
    last = next((line.strip() for line in reversed(output.splitlines()) if line.strip()), "")
    if not last:
        raise StepFailed(step, "path output is empty")
    for base in (Path(layer.layer_dir), work_dir(layer, state)):
        candidate = (base / last).resolve()
        if candidate.is_file():
            return str(candidate)
    raise StepFailed(step, f"path output does not name an existing file: {last!r}")


def process_output(layer: Any, state: Any, command: dict[str, Any], output: str, step: str) -> str:
    kind = command.get("output", "text")
    if kind == "json":
        obj = extract_json(output, step)
        _validate_json(obj, str(command.get("template", "")), step)
        return json.dumps(obj, separators=(",", ":"))
    if kind == "path":
        return _resolve_path_output(layer, state, output, step)
    if kind == "text":
        return output
    raise LayerError(f"command {command.get('name')!r} has unknown output type {kind!r}")


# ---------------------------------------------------------------- entry point

def _render_prompt(layer: Any, state: Any, command_name: str, command: dict[str, Any], args: list[str]) -> str:
    folder = "hops" if command["isHop"] else "commands"
    path = Path(layer.layer_dir) / folder / f"{command_name}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StepFailed(command_name, f"cannot read template {path}: {exc}") from exc
    return render(text, template_variables(layer, state), args)


NO_BACKGROUND_RULE = """## This step ends when your turn ends

Nothing runs after your final message, and no later turn reads what a background command prints. \
Run every command in the foreground with a timeout; never start one in the background (`&`, `nohup`, \
`run_in_background`). Leave long test suites to the run's own checks, which run after you. Commands \
still running when you finish are stopped.
"""
_FRONT_MATTER = re.compile(r"^---\n.*?\n---\n", re.DOTALL)


def with_rules(prompt: str) -> str:
    """`prompt` with NO_BACKGROUND_RULE right after its front matter, or at its start without one."""
    match = _FRONT_MATTER.match(prompt)
    at = match.end() if match else 0
    return prompt[:at] + NO_BACKGROUND_RULE + "\n" + prompt[at:]


def run_agent(layer: Any, state: Any, step_name: str, command_name: str, args: list[str],
              model_set: str, env_stack: Any, agent_id: str = "", tier_from: str = "command") -> str:
    """Render the command/hop, run it on the ADW's agent (mock when dry-run) and return the processed output.

    `model_set` is the tier the call asks for and `tier_from` why (step / request / command); the
    run's overrides may replace both (see models.py). Spending limits are checked before and after
    the call (see budget.py), and so is what the CLI says about subscription usage (see usage.py)."""
    command = layer.command(command_name)
    prompt = _render_prompt(layer, state, command_name, command, args)
    return run_prompt(layer, state, step_name, command, prompt, model_set, env_stack, agent_id, tier_from, args)


def run_prompt(layer: Any, state: Any, step_name: str, command: dict[str, Any], prompt: str, model_set: str,
               env_stack: Any, agent_id: str = "", tier_from: str = "command", args: list[str] | None = None,
               directory: Path | None = None) -> str:
    """Run an already rendered `prompt` on the ADW's agent (mock when dry-run) and return the
    processed output. `command` gives the output type and the template (a declared command, or one
    the runtime holds itself, such as the decider's: see decide.py). Budget, usage, events and the
    transcript are exactly those of run_agent. `directory` is where prompt.md, raw.jsonl and
    output.txt go (default: the step's directory in the run), for a call that has no run."""
    args = list(args or [])
    if directory is None:
        directory = step_dir(layer, state, step_name)
    else:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
    prompt = with_rules(prompt)  # prompt.md and the agent's stdin are the same text
    (directory / "prompt.md").write_text(prompt, encoding="utf-8")
    raw_path = directory / "raw.jsonl"
    # A step may name its own agent (`#:agent`), e.g. a cheaper model for a commit step or a
    # different vendor for an independent look; otherwise the ADW's agent runs it.
    agent = layer.option("agents", agent_id or layer.adw(state.adw)["agent"])
    kind = "mock" if state.dry_run else agent["kind"]
    choice = models.choose(agent, model_set, tier_from, state.overrides)
    used = {"agent": str(agent["id"]), "agent_kind": kind, "model": "" if state.dry_run else choice.model}
    why = {"tier": choice.tier, "tier_from": choice.tier_from, "model_from": choice.model_from,
           "effort": choice.effort}
    owner = next((r for r in reversed(state.steps) if r.get("status") == "running"), None)
    if owner is not None:
        owner.update(used, **why)  # the step record names the agent, the tier and the model actually used
    span = {"step": step_name, "owner": str(owner.get("name")) if owner else "", **used, "model_set": choice.tier, **why}
    usage.before_call(layer, state, step_name)  # stops or waits at the layer's subscription usage threshold
    cap = budget.before_call(layer, state, step_name)  # raises when today's limit is reached: no call starts
    state.budget["cap_from"] = budget.cap_source(state, layer) if cap is not None else ""
    call = {"step": step_name, "owner": span["owner"], **used, **why, "status": "running", "cost": None}
    state.agent_calls.append(call)
    events.emit("agent.start", **span)
    started = time.monotonic()
    try:
        result = _call_agent(layer, state, agent, kind, step_name, command, prompt, args, choice, env_stack,
                             raw_path, directory, cap)
    except BaseException as exc:
        totals = transcript_totals(raw_path)
        found = usage.record(layer, state, step_name, span["owner"], raw_path)
        call.update(status="failed", cost=totals.get("cost"), billing=found["billing"])
        events.emit("agent.end", **span, status="failed", seconds=round(time.monotonic() - started, 3),
                    error=str(exc)[:500], billing=found["billing"], **totals)
        if isinstance(exc, (StepFailed, LayerError)):
            hit = usage.rejection(found)
            if hit is not None:
                # the CLI refused the call or cut it off at a subscription limit: that, not the
                # agent error it ended with, is the reason a person needs to see
                budget.add_cost(state, agent, totals.get("cost"), found["billing"])
                raise usage.reached(state, step_name, hit) from exc
            # a call stopped by the CLI's own budget cap, or one that went over a limit and then
            # failed, is a budget stop: that is the reason a person needs to see
            budget.after_call(state, step_name, agent, totals.get("cost"), failed=True,
                              native_stop=_stopped_by_cap(raw_path, cap), billing=found["billing"])
        raise
    totals = transcript_totals(raw_path)
    found = usage.record(layer, state, step_name, span["owner"], raw_path)
    call.update(status="passed", cost=totals.get("cost"), billing=found["billing"])
    events.emit("agent.end", **span, status="passed", seconds=round(time.monotonic() - started, 3),
                billing=found["billing"], **totals)
    budget.after_call(state, step_name, agent, totals.get("cost"), billing=found["billing"])
    return result


def _stopped_by_cap(raw_path: Path, cap: float | None) -> bool:
    """True when the agent's final result event says the CLI's own budget cap ended the call."""
    if cap is None:
        return False
    subtype = str(transcript_totals(raw_path).get("subtype") or "")
    return "budget" in subtype.lower()


def transcript_totals(raw_path: Path) -> dict[str, Any]:
    """cost / turns / duration_ms from the last `result` event of a stream-json transcript (if any)."""
    totals: dict[str, Any] = {}
    try:
        with raw_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 262144))
            lines = handle.read().splitlines()
    except OSError:
        return totals
    for line in reversed(lines):
        if b'"result"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            for key, name in (("total_cost_usd", "cost"), ("num_turns", "turns"), ("duration_ms", "duration_ms")):
                if isinstance(event.get(key), (int, float)) and not isinstance(event.get(key), bool):
                    totals[name] = event[key]
            if isinstance(event.get("subtype"), str) and event.get("subtype") != "success":
                totals["subtype"] = event["subtype"]
            break
    return totals


def _call_agent(layer: Any, state: Any, agent: dict[str, Any], kind: str, step_name: str, command: dict[str, Any],
                prompt: str, args: list[str], choice: models.Choice, env_stack: Any, raw_path: Path, directory: Path,
                cap: float | None = None) -> str:
    cwd = work_dir(layer, state)
    if kind == "mock":
        output = _run_mock(layer, state, agent, step_name, command, args, raw_path, env_stack)
    elif kind == "claude-code":
        with slots.slot(layer, "agents", f"adw {state.adw_id} step {step_name}"), _host_agent_guard(env_stack):
            output = _run_claude(layer, agent, prompt, choice, cwd, env_stack, step_name, raw_path, cap)
    elif kind == "codex":
        with slots.slot(layer, "agents", f"adw {state.adw_id} step {step_name}"), _host_agent_guard(env_stack):
            output = _run_codex(agent, prompt, choice, cwd, env_stack, step_name, raw_path)
    elif kind == "custom":
        with slots.slot(layer, "agents", f"adw {state.adw_id} step {step_name}"), _host_agent_guard(env_stack):
            output = _run_custom(agent, prompt, choice, cwd, env_stack, step_name, raw_path)
    else:
        raise LayerError(f"Unknown agent kind: {kind!r}")
    result = process_output(layer, state, command, output, step_name)
    (directory / "output.txt").write_text(result, encoding="utf-8")
    return result
