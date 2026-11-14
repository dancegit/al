from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any

from . import storage
from . import slots
from .errors import LayerError, StepFailed
from .templates import render

CLASSES = ("chore", "bug", "feature", "patch")
SEVERITIES = ("blocker", "tech_debt", "skippable")
GIT_TIMEOUT = 120
_STRIPPED_ENV = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"}
_MOCK_KEYS = {"output", "files", "layer_files", "delete", "commit"}
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


def _run_process(argv: list[str], *, cwd: Path, env: dict[str, str], stdin_text: str,
                 timeout: int, step: str, raw_path: Path) -> str:
    """Run an agent CLI in its own process group, streaming stdout to raw_path; returns stdout."""
    stderr_path = raw_path.with_name("stderr.log")
    with raw_path.open("w", encoding="utf-8") as raw, stderr_path.open("w", encoding="utf-8") as err:
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=raw,
                                       stderr=err, text=True, start_new_session=True)
        except OSError as exc:
            raise StepFailed(step, f"could not start agent {argv[0]!r}: {exc}") from exc
        try:
            process.communicate(stdin_text, timeout=max(1, timeout))
        except subprocess.TimeoutExpired as exc:
            _kill_group(process)
            process.wait(timeout=30)
            raise StepFailed(step, f"agent timed out after {timeout}s (process group killed)") from exc
    stdout = raw_path.read_text(encoding="utf-8", errors="replace")
    if process.returncode != 0:
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


def _claude_env(env_stack: Any) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _STRIPPED_ENV}
    env.update(env_stack.env)
    return env


def _model(agent: dict[str, Any], model_set: str) -> str:
    return str(agent.get("modelHeavy" if model_set == "heavy" else "modelBase") or "")


def _run_claude(layer: Any, agent: dict[str, Any], prompt: str, model_set: str, cwd: Path,
                env_stack: Any, step: str, raw_path: Path) -> str:
    # The prompt goes in on stdin: templates start with `---` front matter, which the CLI would
    # parse as an option, and long prompts would hit the argv size limit.
    argv = ["claude", "-p", "--output-format", "stream-json", "--verbose",
            "--model", _model(agent, model_set), "--permission-mode", str(agent["permissionMode"]),
            "--max-turns", str(agent["maxTurns"]), "--add-dir", str(layer.layer_dir)]
    if agent.get("allowedTools"):
        argv += ["--allowedTools", ",".join(agent["allowedTools"])]
    if agent.get("safeMode"):
        argv.append("--safe-mode")
    stdout = _run_process(env_stack.wrap_agent(argv), cwd=cwd, env=_claude_env(env_stack), stdin_text=prompt,
                          timeout=int(agent["timeout"]), step=step, raw_path=raw_path)
    return parse_stream_json_result(stdout, step)


def _run_codex(agent: dict[str, Any], prompt: str, model_set: str, cwd: Path, env_stack: Any,
               step: str, raw_path: Path) -> str:
    argv = ["codex", "exec", "--model", _model(agent, model_set), "--sandbox", str(agent["sandbox"]),
            "--cd", str(cwd), "-"]
    return _run_process(env_stack.wrap_agent(argv), cwd=cwd, env=env_stack.process_env(), stdin_text=prompt,
                        timeout=int(agent["timeout"]), step=step, raw_path=raw_path)


def _run_custom(agent: dict[str, Any], prompt: str, model_set: str, cwd: Path, env_stack: Any,
                step: str, raw_path: Path) -> str:
    template = str(agent.get("command") or "")
    if not template:
        raise LayerError(f"custom agent {agent.get('id')!r} has an empty command")
    shell_command = template.replace("{model}", _model(agent, model_set)).replace("{cwd}", str(cwd))
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
    for key in ("files", "layer_files"):
        mapping = data.get(key, {})
        if not isinstance(mapping, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
            raise StepFailed(step, f"mock {key} must map relative paths to string content")
    delete = data.get("delete", [])
    if not isinstance(delete, list) or not all(isinstance(item, str) for item in delete):
        raise StepFailed(step, "mock delete must be a list of relative paths")
    if not isinstance(data.get("commit", False), bool):
        raise StepFailed(step, "mock commit must be a boolean")
    return data


def _inside(root: Path, relative: str, step: str) -> Path:
    root = root.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or target == root:
        raise StepFailed(step, f"mock path {relative!r} escapes {root}")
    return target


def _write_files(root: Path, files: dict[str, str], variables: dict[str, str], args: list[str], step: str) -> None:
    for relative, content in files.items():
        target = _inside(root, relative, step)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render(content, variables, args), encoding="utf-8")


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


def _commit_all(worktree: Path, step: str) -> None:
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
    return f"mock: {step} ok"


def _run_mock(layer: Any, state: Any, agent: dict[str, Any], step: str, command: dict[str, Any],
              args: list[str], raw_path: Path) -> str:
    script = find_mock_script(layer, state, agent, step)
    if script is None:
        output = _synthesise(layer, state, step, command)
        raw_path.write_text(json.dumps({"synthesised": True, "output": output}) + "\n", encoding="utf-8")
        return output
    data = _load_mock_script(script, step)
    raw_path.write_text(json.dumps({"script": str(script), **data}) + "\n", encoding="utf-8")
    variables = template_variables(layer, state)
    worktree = work_dir(layer, state)
    _write_files(worktree, data.get("files", {}), variables, args, step)
    _write_files(Path(layer.layer_dir), data.get("layer_files", {}), variables, args, step)
    _delete_paths(worktree, data.get("delete", []), step)
    if data.get("commit"):
        _commit_all(worktree, step)
    return render(data.get("output", f"mock: {step} ok"), variables, args)


# ---------------------------------------------------------------- output handling

def extract_json(output: str, step: str) -> dict[str, Any]:
    """The last ```json fenced block, else the outermost {...} of the output."""
    blocks = _FENCED_JSON.findall(output)
    candidate = blocks[-1].strip() if blocks else output[output.find("{"): output.rfind("}") + 1]
    if not candidate:
        raise StepFailed(step, "agent output contains no JSON object")
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
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


def run_agent(layer: Any, state: Any, step_name: str, command_name: str, args: list[str],
              model_set: str, env_stack: Any) -> str:
    """Render the command/hop, run it on the ADW's agent (mock when dry-run) and return the processed output."""
    command = layer.command(command_name)
    prompt = _render_prompt(layer, state, command_name, command, args)
    directory = step_dir(layer, state, step_name)
    (directory / "prompt.md").write_text(prompt, encoding="utf-8")
    raw_path = directory / "raw.jsonl"
    agent = layer.option("agents", layer.adw(state.adw)["agent"])
    kind = "mock" if state.dry_run else agent["kind"]
    cwd = work_dir(layer, state)
    if kind == "mock":
        output = _run_mock(layer, state, agent, step_name, command, args, raw_path)
    elif kind == "claude-code":
        with slots.slot(layer, "agents", f"adw {state.adw_id} step {step_name}"):
            output = _run_claude(layer, agent, prompt, model_set, cwd, env_stack, step_name, raw_path)
    elif kind == "codex":
        with slots.slot(layer, "agents", f"adw {state.adw_id} step {step_name}"):
            output = _run_codex(agent, prompt, model_set, cwd, env_stack, step_name, raw_path)
    elif kind == "custom":
        with slots.slot(layer, "agents", f"adw {state.adw_id} step {step_name}"):
            output = _run_custom(agent, prompt, model_set, cwd, env_stack, step_name, raw_path)
    else:
        raise LayerError(f"Unknown agent kind: {kind!r}")
    result = process_output(layer, state, command, output, step_name)
    (directory / "output.txt").write_text(result, encoding="utf-8")
    return result
