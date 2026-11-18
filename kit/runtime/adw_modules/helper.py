"""The dashboard voice helper's agent call (`adws/dispatch.py --helper`): one prompt in, one answer out.

The dashboard owns the conversation, the prompt and its fixed, read-only tools; this module only asks
the layer's own agent for the next answer, with nothing it could act with:

  claude-code  `claude -p` with every built-in tool disabled (`--tools ""`), no MCP server
               (`--strict-mcp-config`), one turn, default permissions, never `--add-dir` or
               `--allowedTools`, in a fresh empty temporary directory, without the dashboard's token
  mock         the rules of mock/helper.json (see load_mock): the first rule whose `when` matches the
               prompt's latest block answers
  codex/custom unavailable: they have no mode without tools

The agent is the voice's #:helper-agent (else the layer's first agent), its tier #:helper-model-set,
and its model comes from the tier exactly as for runs, the layer's local setting included. Every call
appends one line to agents/voice/helper-calls.jsonl (time, agent, tier, model, cost, status, seconds;
never the prompt or the answer), so what the helper costs stays visible.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from . import models
from .agents import _STRIPPED_ENV, parse_stream_json_result
from .errors import LayerError, StepFailed
from .state import now

TIMEOUT = 90                                   # seconds one helper answer may take
CALL_LOG = Path("agents") / "voice" / "helper-calls.jsonl"
MOCK_FILE = "helper.json"
LATEST = "### Latest\n"                        # the last block of the dashboard's helper prompt
# the IR's (voice kokoro …) defaults, for a layer compiled before the helper existed
VOICE_DEFAULTS = {"dashboardHelper": True, "helperAgent": "", "helperModelSet": "base"}
TOOLLESS = ("claude-code", "mock")
_HIDDEN_ENV = {"AL_DASHBOARD_TOKEN", "AL_VOICE_TOKEN"}
_MOCK_KEYS = {"rules", "default"}
_RULE_KEYS = {"when", "reply"}
STEP = "helper"


def voice(layer: Any) -> dict[str, Any] | None:
    """The layer's `voice` with the helper's defaults filled in; None without (voice …)."""
    data = layer.ir.get("voice")
    if not isinstance(data, dict):
        return None
    return {**VOICE_DEFAULTS, **{k: v for k, v in data.items() if v is not None}}


def helper_agent(layer: Any) -> dict[str, Any]:
    """The agent the helper thinks with: #:helper-agent, else the layer's first agent."""
    wanted = str((voice(layer) or VOICE_DEFAULTS).get("helperAgent") or "")
    if wanted:
        return layer.option("agents", wanted)
    agents = [a for a in layer.ir.get("agents") or [] if isinstance(a, dict)]
    if not agents:
        raise LayerError("the layer declares no agent")
    return agents[0]


def unavailable_reason(agent: dict[str, Any]) -> str:
    """The compile warning's sentence for an agent that cannot run without tools."""
    return (f"(voice kokoro): the dashboard helper needs an agent that runs with no tools (claude-code or mock); "
            f"agent `{agent.get('id')}` is {agent.get('kind')}, so the panel will speak and listen but cannot "
            "answer. Set #:helper-agent or #:dashboard-helper #f")


def helper_argv(agent: dict[str, Any], choice: models.Choice) -> list[str]:
    """`claude -p` with no tools at all: no built-in tool, no MCP server, one turn."""
    argv = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    if choice.model:
        argv += ["--model", choice.model]
    argv += ["--tools", "", "--strict-mcp-config", "--permission-mode", "default", "--max-turns", "1"]
    if choice.effort and choice.effort != "default":
        argv += ["--effort", choice.effort]
    if agent.get("safeMode"):
        argv.append("--safe-mode")
    return argv


def helper_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The agent's environment: this one without Claude Code's nesting markers and without the
    dashboard's or the voice service's token."""
    environ = dict(os.environ if environ is None else environ)
    return {k: v for k, v in environ.items() if k not in _STRIPPED_ENV and k not in _HIDDEN_ENV}


def _run(argv: list[str], *, cwd: Path, env: dict[str, str], stdin_text: str, timeout: int) -> tuple[int, str, str]:
    """Run argv in its own process group; (exit, stdout, stderr). A timeout kills the group (exit 124)."""
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = process.communicate(stdin_text, timeout=timeout)
    except BaseException as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = process.communicate()
        if isinstance(exc, subprocess.TimeoutExpired):
            return 124, out or "", (err or "") + f"\nthe helper's agent took longer than {timeout} s"
        raise
    return int(process.returncode or 0), out or "", err or ""


def _cost(stdout: str) -> float | None:
    """total_cost_usd of the last stream-json result event, if it reports one."""
    cost = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            value = event.get("total_cost_usd")
            cost = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return cost


def load_mock(path: Path) -> dict[str, Any]:
    """mock/helper.json: {"rules": [{"when": REGEX, "reply": OBJECT | TEXT}], "default": OBJECT | TEXT}.
    Unknown keys are refused, as in the mock agent's step scripts."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise StepFailed(STEP, f"the mock helper has no script: {path}") from None
    except (OSError, ValueError) as exc:
        raise StepFailed(STEP, f"invalid mock helper script {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise StepFailed(STEP, f"mock helper script {path} must be a JSON object")
    unknown = set(data) - _MOCK_KEYS
    if unknown:
        raise StepFailed(STEP, f"mock helper script {path} has unknown keys {sorted(unknown)}")
    rules = data.get("rules", [])
    if not isinstance(rules, list):
        raise StepFailed(STEP, "mock helper rules must be a list")
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) - _RULE_KEYS:
            raise StepFailed(STEP, f"a mock helper rule has exactly the keys when and reply: {rule!r}")
        if not isinstance(rule.get("when"), str) or not isinstance(rule.get("reply"), (dict, str)):
            raise StepFailed(STEP, f"a mock helper rule needs a string when and an object reply: {rule!r}")
        try:
            re.compile(rule["when"])
        except re.error as exc:
            raise StepFailed(STEP, f"mock helper rule when {rule['when']!r} is not a regular expression: {exc}") from exc
    if "default" in data and not isinstance(data["default"], (dict, str)):
        raise StepFailed(STEP, "mock helper default must be an object")
    return data


def latest_block(prompt: str) -> str:
    """The text after the prompt's last `### Latest` heading (the whole prompt without one)."""
    at = prompt.rfind(LATEST)
    return prompt[at + len(LATEST):] if at >= 0 else prompt


def mock_reply(script: dict[str, Any], prompt: str) -> str:
    block = latest_block(prompt)
    for rule in script.get("rules") or []:
        if re.search(rule["when"], block, re.IGNORECASE):
            chosen = rule["reply"]
            break
    else:
        if "default" not in script:
            raise StepFailed(STEP, "no mock helper rule matches and the script has no default")
        chosen = script["default"]
    return chosen if isinstance(chosen, str) else json.dumps(chosen)


def _log_call(layer: Any, record: dict[str, Any]) -> None:
    path = Path(layer.layer_dir) / CALL_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass  # the log is a courtesy; the answer still goes back


def reply(layer: Any, prompt: str, run: Callable[..., tuple[int, str, str]] = _run) -> dict[str, Any]:
    """Ask the helper's agent for one answer to `prompt`. → {"status": "ok", "reply", "cost", "model"}
    or {"status": "unavailable" | "failed", "error"}; every call is logged (never its text)."""
    started = time.monotonic()
    settings = voice(layer)
    if settings is None:
        return {"status": "unavailable", "error": "this layer declares no (voice …)"}
    if not settings.get("dashboardHelper"):
        return {"status": "unavailable", "error": "the dashboard helper is off (#:dashboard-helper #f)"}
    try:
        agent = helper_agent(layer)
    except LayerError as exc:
        return {"status": "failed", "error": str(exc)}
    kind = str(agent.get("kind") or "")
    if kind not in TOOLLESS:
        return {"status": "unavailable", "error": unavailable_reason(agent)}
    record: dict[str, Any] = {"t": now(), "agent": str(agent.get("id") or ""), "kind": kind,
                              "tier": str(settings.get("helperModelSet") or "base"), "model": "", "cost": None}
    try:
        local, _notes = models.local_settings(layer)
        over = models.overrides(None, [], None, environ={}, settings=local)
        choice = models.choose(agent, record["tier"], "voice", over)
        if kind == "mock":
            directory = Path(layer.layer_dir) / str(agent.get("scriptDir") or "mock")
            record["model"] = model = "mock"
            text, cost = mock_reply(load_mock(directory / MOCK_FILE), prompt), None
        else:
            model = choice.model
            with tempfile.TemporaryDirectory(prefix="al-helper-") as empty:
                code, out, err = run(helper_argv(agent, choice), cwd=Path(empty), env=helper_env(),
                                     stdin_text=prompt, timeout=TIMEOUT)
            cost = _cost(out)
            record.update(model=model, cost=cost)
            if code != 0:
                raise StepFailed(STEP, f"the helper's agent exited {code}: {(err or out).strip()[-500:]}")
            text = parse_stream_json_result(out, STEP)
    except (LayerError, StepFailed, ValueError, OSError) as exc:
        reason = exc.reason if isinstance(exc, StepFailed) else str(exc)
        _log_call(layer, {**record, "status": "failed", "seconds": round(time.monotonic() - started, 3)})
        return {"status": "failed", "error": reason}
    _log_call(layer, {**record, "model": model, "cost": cost, "status": "ok",
                      "seconds": round(time.monotonic() - started, 3)})
    return {"status": "ok", "reply": text, "cost": cost, "model": model}
