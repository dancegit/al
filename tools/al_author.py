"""`al author LAYER`: have the layer definition written (GUIDE Step 4 as a command).

It hands meta/commands/author-layer.md to the agent CLI with the layer's LLM.txt, exactly as a person
does by hand, then compiles the result. It never overwrites an existing agentic.rkt and writes nothing
into the application checkout. `--agent mock --mock FILE` writes fixed files instead (tests, no LLM):

    {"files": {"agentic.rkt": "#lang al … #:root \\"${root}\\" …"}, "output": "text the agent printed"}

`${name}` (the application's directory name), `${root}` (the application relative to the layer),
`${forge}` (the forge clause the preferences name), `${includes}` (`(include "adopted/layer.rkt")` when
the layer has an adopted/ folder, else empty) and `${codebase_includes}` (`(include
"adopted/codebase.rkt")`, for inside `(codebase …)`) are substituted; every path must stay inside the layer.

Before any agent (or the mock) runs, the prerequisites are checked (racket, and the agent's CLI) through
tools/al_tools.py: a missing one stops it at once with exit 4. A compiler that cannot be started is never
blamed on the file. `--accept-existing` (a dashboard retry) takes an agentic.rkt that is already there
when it compiles, and asks no agent.

What it prints while the agent works: one progress line per message, tool call and error, and the agent's
report once at the end (with its Decisions and Left out lists), condensed from Claude Code's event stream by
the dashboard kit's AgentJournal (kit/dashboard/dashboard.py, the parser a dashboard job uses). `--raw`
prints the event stream itself, line by line, as before; a dashboard job runs `al author … --raw` and
condenses it in its runner.

The preferences handed to the agent are built from the repository's facts (LAYER/scout.json, written
by `al init` and completed by `al scout --into`), not from a fixed sentence: the forge the remote and
the CI folders show, the input and triggers that go with it, podman when it is found, the delivery
style the history shows. `--choice KEY=VALUE` overrides one (CHOICES lists the keys and values). Every
decision, with its fact or the default it fell back to, is written to agents/author/choices.json, and
the whole prompt to agents/author/prompt.txt. A token is never read: the forge clause names the
variable that will hold it. The agent runs isolated (tools/al_tools.py `agent_isolation`): without
the account's personal settings, and allowed to write only inside the layer. Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import al_delivery
import al_tools   # tools/ is on sys.path: tools/al loads this module from there

ROOT = Path(__file__).resolve().parent.parent
VOCAB = ROOT / "racket" / "al" / "private" / "vocab.rkt"
AUTHOR_DIR = Path("agents") / "author"
INCLUDE_LAYER = '(include "adopted/layer.rkt")'
INCLUDE_CODEBASE = '(include "adopted/codebase.rkt")'
SILENT = "facts are silent"
REPAIR_HINT = ("the file is kept: repair it with meta/commands/repair-layer.md (the compiler error above names the clause) "
               "or delete it and run `al author` again")
INCLUDE_SENTENCE = ('The layer folder has an adopted/ folder (written by `al adopt`): put (include "adopted/codebase.rkt") '
                    'inside (codebase …) and (include "adopted/layer.rkt") at the top level.')


class AuthorError(Exception):
    pass


# ------------------------------------------------------------------------------------------ choices

@dataclass(frozen=True)
class Choice:
    """What one key of `--choice KEY=VALUE` accepts: one of `values` (a comma-separated list of them
    with `multi`), or a string matching `pattern`. The dashboard's CHOICE_RULES mirror these."""
    values: tuple[str, ...] = ()
    multi: bool = False
    pattern: str = ""


CHOICES: dict[str, Choice] = {
    "forge": Choice(("none", "github", "forgejo")),
    "host": Choice(pattern=r"https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?"),
    "repo": Choice(pattern=r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"),
    "input": Choice(("tasklist", "issue")),
    "trigger": Choice(("manual", "cron", "webhook", "forge-actions"), multi=True),
    "environment": Choice(("worktree", "podman", "nix-devshell"), multi=True),
    "review": Choice(("human", "agent")),
    "delivery": Choice(tuple(dict.fromkeys(al_delivery.PRESET.values())) + ("none",)),
}


def choice_problem(key: str, value: Any) -> str:
    """Why KEY=VALUE is not a valid choice, or ""."""
    if key not in CHOICES:
        return f"unknown choice {key!r}; the choices are: {', '.join(CHOICES)}"
    rule = CHOICES[key]
    if not isinstance(value, str):
        return f"the choice {key} must be a string"
    if rule.pattern:
        return "" if re.fullmatch(rule.pattern, value) else f"{key}={value!r} is not a valid {key}"
    parts = [v.strip() for v in value.split(",")] if rule.multi else [value]
    bad = [v for v in parts if v not in rule.values]
    if bad or not parts:
        return f"{key}={value!r}: {'each of ' if rule.multi else ''}one of {', '.join(rule.values)}"
    return ""


def parse_choices(pairs: list[str] | None) -> dict[str, str]:
    """`--choice KEY=VALUE` arguments as a dict; AuthorError naming the allowed keys or values."""
    out: dict[str, str] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        key = key.strip()
        if not sep:
            raise AuthorError(f"--choice {pair}: expected KEY=VALUE; the choices are: {', '.join(CHOICES)}")
        problem = choice_problem(key, value.strip())
        if problem:
            raise AuthorError(f"--choice {pair}: {problem}")
        out[key] = ",".join(dict.fromkeys(v.strip() for v in value.split(","))) if CHOICES[key].multi else value.strip()
    return out


def token_envs(vocab: Path = VOCAB) -> dict[str, str]:
    """forge kind → the default of its #:token-env, read from the language's vocabulary."""
    text = vocab.read_text(encoding="utf-8")
    out = {}
    for m in re.finditer(r"\(define-kind forge (\S+)(.*?)(?=\(define-|\Z)", text, re.S):
        token = re.search(r'\[token-env string "([^"]*)"', m.group(2))
        if token:
            out[m.group(1)] = token.group(1)
    return out


@dataclass
class Preferences:
    text: str                                   # PREFERENCES ($3 of author-layer.md)
    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)   # KEY → value, source, because, options
    token_env: str = ""                         # the variable that holds the forge token ("" without a forge)


def _decision(key: str, value: str, source: str, because: str) -> dict[str, Any]:
    rule = CHOICES[key]
    return {"value": value, "source": source, "because": because, "options": list(rule.values),
            "multi": rule.multi}


def _rkt(text: str) -> str:
    return json.dumps(text)


def detected(facts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every choice as the facts show it (no user choice): KEY → {value, source fact|default, because}."""
    kind = str(facts.get("forge") or "none")
    evidence = "; ".join(facts.get("forge_evidence") or []) or SILENT
    out: dict[str, dict[str, Any]] = {}
    if kind in ("github", "forgejo"):
        out["forge"] = _decision("forge", kind, "fact", evidence)
    elif kind == "gitlab":
        out["forge"] = _decision("forge", "none", "default", f"gitlab is planned in the language, not available yet, "
                                 f"so (forge none) ({evidence})")
    elif kind == "unknown":
        out["forge"] = _decision("forge", "none", "default", f"the forge behind the remote is unknown, so (forge none) ({evidence})")
    else:
        out["forge"] = _decision("forge", "none", "default", SILENT)
    host = str(facts.get("forge_host") or "")
    host_value = host if kind == "forgejo" or (kind == "github" and host not in ("", "https://github.com")) else ""
    out["host"] = _decision("host", host_value, "fact" if host_value else "default",
                            f"the origin remote's host is {host}" if host_value else
                            ("github.com, the forge's own default" if kind == "github" else SILENT))
    repo = str(facts.get("repo") or "") if kind in ("github", "forgejo") else ""
    out["repo"] = _decision("repo", repo, "fact" if repo else "default",
                            f"the origin remote names {repo}" if repo else SILENT)
    return out


def _follow_forge(out: dict[str, dict[str, Any]], facts: dict[str, Any]) -> None:
    """input, trigger, environment, review and delivery, given the (possibly chosen) forge."""
    forge = out["forge"]["value"]
    why = f"follows the forge ({forge}{', chosen by the user' if out['forge']['source'] == 'user' else ''})"
    if forge != "none":
        out["input"] = _decision("input", "issue", "fact", why)
        out["trigger"] = _decision("trigger", "webhook,manual", "fact", why)
    else:
        out["input"] = _decision("input", "tasklist", "default", SILENT)
        out["trigger"] = _decision("trigger", "cron,manual", "default", SILENT)
    if facts.get("container") == "podman":
        out["environment"] = _decision("environment", "worktree,podman", "fact", "podman was found on this machine")
    else:
        out["environment"] = _decision("environment", "worktree", "default", SILENT)
    out["review"] = _decision("review", "human", "default", SILENT)
    delivery = facts.get("delivery") or {}
    if delivery.get("preset") and not delivery.get("unverified"):
        out["delivery"] = _decision("delivery", str(delivery["preset"]), "fact",
                                    "; ".join((delivery.get("evidence") or [])[:2]) or "the branch history")
    else:
        out["delivery"] = _decision("delivery", "none", "default", SILENT)


def _choose(out: dict[str, dict[str, Any]], key: str, value: str) -> None:
    before = out.get(key) or {}
    was = before.get("value", "")
    out[key] = _decision(key, value, "user", "chosen by the user" + (
        f" (detected: {was or 'nothing'}, because {before.get('because')})" if before and was != value else ""))


def decide(facts: dict[str, Any], choices: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """The decisions: what the facts show, with the user's choices applied (forge first: input and
    triggers follow it). AuthorError when a choice does not fit the others."""
    choices = dict(choices or {})
    for key, value in choices.items():
        problem = choice_problem(key, value)
        if problem:
            raise AuthorError(problem)
    out = detected(facts)
    for key in ("forge", "host", "repo"):
        if key in choices:
            _choose(out, key, choices[key])
    forge = out["forge"]["value"]
    if forge == "none":
        for key in ("host", "repo"):
            if out[key]["source"] != "user":
                out[key] = _decision(key, "", "default", "no forge")
    elif not out["repo"]["value"]:
        raise AuthorError(f"forge {forge} needs a repository: add --choice repo=OWNER/NAME")
    if forge == "forgejo" and not out["host"]["value"]:
        raise AuthorError("forge forgejo needs its host: add --choice host=https://forgejo.example.org")
    if forge == "github" and out["host"]["source"] != "user" and "github" not in out["host"]["value"]:
        out["host"] = _decision("host", "", "default", "github.com, the forge's own default")
    _follow_forge(out, facts)
    for key in ("input", "trigger", "environment", "review", "delivery"):
        if key in choices:
            _choose(out, key, choices[key])
    if forge == "none":
        if out["input"]["value"] == "issue":
            raise AuthorError("input issue needs a forge: add --choice forge=github or forge=forgejo")
        if "forge-actions" in out["trigger"]["value"].split(","):
            raise AuthorError("trigger forge-actions needs a forge: add --choice forge=github or forge=forgejo")
    return out


def forge_clause(decisions: dict[str, dict[str, Any]]) -> str:
    forge = decisions["forge"]["value"]
    host, repo = decisions["host"]["value"], decisions["repo"]["value"]
    if forge == "forgejo":
        return f"(forge forgejo #:host {_rkt(host)} #:repo {_rkt(repo)})"
    if forge == "github":
        hostname = re.sub(r"^https?://", "", host)
        return f"(forge github #:repo {_rkt(repo)}" + (f" #:host {_rkt(hostname)}" if hostname and hostname != "github.com" else "") + ")"
    return "(forge none)"


def preferences_from_facts(facts: dict[str, Any], app_rel: str, layer: Path,
                           choices: dict[str, str] | None = None) -> Preferences:
    """PREFERENCES for author-layer.md from the repository's facts: each decision as a DSL clause with
    the fact behind it, or the default it falls back to (named, so the agent reports it)."""
    d = decide(facts, choices)
    forge = d["forge"]["value"]
    token_env = token_envs().get(forge, "") if forge != "none" else ""
    envs = []
    for env in d["environment"]["value"].split(","):
        if env == "nix-devshell":
            packages = " ".join(facts.get("nix") or [])
            envs.append(f"(environment nix-devshell #:packages ({packages}))")
        else:
            envs.append(f"(environment {env})")
    delivery = d["delivery"]["value"]
    proposal = str((facts.get("delivery") or {}).get("proposal") or "")
    if delivery == "none":
        delivery_clause = "no (delivery …) clause: runs use the default branch"
    elif proposal.startswith(f"(delivery {delivery}") and proposal[len(f"(delivery {delivery}"):][:1] in (" ", ")"):
        delivery_clause = proposal
    else:
        delivery_clause = f"(delivery {delivery})"
    clauses = {
        "forge": forge_clause(d),
        "input": f"(prompt-input {d['input']['value']})",
        "trigger": " ".join(f"(trigger {t})" for t in d["trigger"]["value"].split(",")),
        "environment": " ".join(envs),
        "review": f"(review {d['review']['value']})",
        "delivery": delivery_clause,
    }
    lines = ["These choices were made by `al author` from the repository's facts (scout.json). They win over the "
             "Decision Table of author-layer.md; follow the Decision Table only where they are silent. In your report, "
             "list every choice marked `default` as a default you relied on.", ""]
    for key in ("forge", "input", "trigger", "environment", "review", "delivery"):
        how = d[key]["source"]
        label = "fact" if how == "fact" else "chosen by the user" if how == "user" else "default"
        reason = d[key]["because"] if how != "user" else d[key]["because"].removeprefix("chosen by the user").strip(" ()") or "-"
        lines.append(f"- {key}: {clauses[key]}  [{label}: {reason}]")
    lines.append("")
    if token_env:
        lines.append(f"The forge token is read at run time from the environment variable {token_env} (the "
                     f"#:token-env default); never write a token into any file.")
    if app_rel:
        lines.append(f"The codebase root, relative to the layer folder, is {app_rel}.")
    if (Path(layer) / "adopted").is_dir():
        lines.append(INCLUDE_SENTENCE)
    return Preferences("\n".join(lines).rstrip() + "\n", d, token_env)


# ------------------------------------------------------------------------------------------ the agent

def author_prompt(layer: Path, preferences: str, root: Path = ROOT) -> str:
    """The whole prompt: author-layer.md inlined, its variables bound to the layer's own files (the layer's
    copy of the language reference, so the agent needs nothing outside the layer)."""
    command = (root / "meta" / "commands" / "author-layer.md").read_text(encoding="utf-8")
    return (f"Follow the instructions of meta/commands/author-layer.md, given in full below, with these values:\n\n"
            f"- LLM_TXT ($1) = {layer}/LLM.txt\n- OUTPUT ($2) = {layer}/agentic.rkt\n"
            f"- DSL_REFERENCE = {layer}/AL-DSL.llms.txt (the layer's own copy of docs/AL-DSL.llms.txt)\n"
            f"- COMPILE = {root}/tools/al compile {layer} >/dev/null\n- PREFERENCES ($3): below\n\n"
            f"You may write only inside {layer}.\n\nPREFERENCES:\n\n{preferences}\n"
            f"--- meta/commands/author-layer.md ---\n\n{command}")


AUTHOR_TOOLS = ["Read", "Grep", "Glob"]


def claude_argv(root: Path, layer: Path, claude: str = "claude", isolation: al_tools.Isolation | None = None) -> list[str]:
    """The agent CLI's argv: a fixed list, no shell; the prompt goes on stdin and the run's cwd is the layer.
    `claude` is the program: run_claude passes the path tools/al_tools.py found. `isolation` adds the flags
    that keep the account's personal setup out and limit writing to the layer."""
    return [claude, "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "default",
            *(isolation.argv_extra if isolation is not None else [])]


def author_isolation(layer: Path, root: Path = ROOT) -> al_tools.Isolation:
    return al_tools.agent_isolation(layer, layer, [*AUTHOR_TOOLS, f"Bash({root}/tools/al compile:*)"])


def run_agent_cli(layer: Path, argv_of, isolation: al_tools.Isolation, prompt: str, say,
                  raw: bool = False, kit: Any = None) -> None:
    """Run `claude` (found through al_tools) in `layer` with `isolation`, the prompt on stdin; the
    isolation's folder is removed afterwards, whatever happens. With `raw`, `say` each line of its event
    stream and then its result text; else `say` the condensed lines of `kit.AgentJournal` (the dashboard
    kit, loaded by tools/al) and, at the end, the report once with its Decisions and Left out lists."""
    try:
        found = al_tools.find("claude", layer)
        if not found.path:
            raise al_tools.ToolMissing([found])
        env = {**os.environ, **isolation.env}
        try:
            proc = subprocess.Popen(argv_of(found.path), cwd=layer, stdin=subprocess.PIPE, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, shell=False)
        except OSError as exc:
            raise AuthorError(f"{al_tools.missing_sentence(found)} ({found.path} could not be started: "
                              f"{exc.strerror or exc}; or use --agent mock --mock FILE)") from exc
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(prompt)
        proc.stdin.close()
        if not raw:
            journal = kit.AgentJournal(kit.Shortener([(str(layer), "layer:")]))
            report: dict[str, Any] | None = None
            for line in proc.stdout:
                for event in journal.feed_line(line):
                    shown = kit.journal_line(event)
                    if shown is not None:
                        say(shown)
                    if event.get("kind") == "report":
                        report = event
            code = proc.wait()
            for event in journal.finish():
                report = event if event.get("kind") == "report" else report
            if report is not None:
                say(report["text"])
                for title, key in (("Decisions:", "decisions"), ("Left out and why:", "left_out")):
                    if report.get(key):
                        say(title)
                        for item in report[key]:
                            say(f"  - {item}")
            if code != 0:
                raise AuthorError(f"the agent exited {code}")
            return
        result = ""
        for line in proc.stdout:
            say(line.rstrip("\n"))
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                result = str(event.get("result") or "")
        code = proc.wait()
        if result:
            say(result)
        if code != 0:
            raise AuthorError(f"the agent exited {code}")
    finally:
        isolation.cleanup()


def run_claude(layer: Path, prompt: str, say, raw: bool = False, kit: Any = None) -> None:
    isolation = author_isolation(layer)
    run_agent_cli(layer, lambda claude: claude_argv(ROOT, layer, claude, isolation), isolation, prompt, say,
                  raw=raw, kit=kit)


def run_mock(layer: Path, mock: Path, values: dict[str, str]) -> str:
    """Write the mock's files into the layer (every path inside it) and return its output text."""
    try:
        spec: Any = json.loads(Path(mock).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuthorError(f"cannot read the mock file {mock}: {exc}") from exc
    files = spec.get("files") if isinstance(spec, dict) else None
    if not isinstance(files, dict):
        raise AuthorError(f"{mock}: expected an object with \"files\"")
    base = layer.resolve()
    targets = {}
    for rel, text in files.items():
        target = (base / str(rel)).resolve()
        if target == base or base not in target.parents:
            raise AuthorError(f"mock path {rel} is outside the layer {layer}")
        targets[target] = str(text)
    for target, text in targets.items():
        for key, value in values.items():
            text = text.replace("${" + key + "}", value)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return str(spec.get("output") or "")


def never_compiled(layer: Path) -> str:
    return (f"the file is kept and was not checked: it was never compiled; when the compiler is found, "
            f"run al author {layer} --accept-existing")


def layer_facts(al: Any, layer: Path, app: Path | None) -> dict[str, Any]:
    """LAYER/scout.json, else a fresh scout of APP (written as scout.json), else nothing."""
    try:
        facts = json.loads((layer / "scout.json").read_text(encoding="utf-8"))
        if isinstance(facts, dict):
            return facts
    except (OSError, ValueError):
        pass
    if app is None:
        return {}
    facts = al.scout(app)
    al.write_scout_json(layer, facts)
    return al.facts_for_layer(facts)


def write_author_files(layer: Path, prefs: Preferences, prompt: str) -> None:
    folder = layer / AUTHOR_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "choices.json").write_text(json.dumps(prefs.decisions, indent=2) + "\n", encoding="utf-8")
    (folder / "prompt.txt").write_text(prompt, encoding="utf-8")


def author(al: Any, layer: Path, agent: str, mock: Path | None, preferences: str | None, say=print,
           accept_existing: bool = False, choices: dict[str, str] | None = None, raw: bool = False) -> int:
    layer = layer.resolve()
    definition = layer / "agentic.rkt"
    if definition.exists() and not accept_existing:
        raise AuthorError(f"{definition} already exists; it is never overwritten")
    if not (layer / "LLM.txt").is_file():
        raise AuthorError(f"{layer} has no LLM.txt: create the layer with `al init APP` first")
    app = layer.with_name(layer.name.removesuffix(".al")) if layer.name.endswith(".al") else None
    if app is not None and not (app / ".git").exists():
        app = None
    if app is None and not preferences:
        raise AuthorError(f"cannot tell where the application is (no git checkout {layer.name.removesuffix('.al')} "
                          "beside the layer): say it with --preferences, e.g. \"The codebase root, relative to the "
                          "layer folder, is ../myapp.\"")
    app_rel = os.path.relpath(app, layer) if app is not None else ""
    if choices:
        for key, value in choices.items():
            problem = choice_problem(key, value)
            if problem:
                raise AuthorError(problem)
    tools = al.tools_module()
    try:   # before any agent or mock runs: a missing compiler or agent CLI costs no agent time
        tools.need(("racket",) + tools.agent_bin(agent), layer)
    except tools.ToolMissing as exc:
        if definition.exists():
            exc.hint = never_compiled(layer)
        raise
    if definition.exists():   # accept_existing: take it when it compiles, ask no agent
        try:
            al.compile_layer(layer)
        except al.CompilerUnavailable as exc:
            raise al.CompilerUnavailable(f"{exc}\n{never_compiled(layer)}") from exc
        except tools.ToolMissing as exc:
            exc.hint = never_compiled(layer)
            raise
        except Exception as exc:  # noqa: BLE001 — AlError of tools/al: the compiler's own text
            raise AuthorError(f"{exc}\n{REPAIR_HINT}") from exc
        say(f"agentic.rkt already exists and compiles; accepted (no agent was asked); next: al gen {layer}")
        return 0
    if agent not in ("mock", "claude-code"):
        raise AuthorError(f"unknown agent {agent}")
    if agent == "mock" and mock is None:
        raise AuthorError("--agent mock needs --mock FILE")
    prefs = preferences_from_facts(layer_facts(al, layer, app), app_rel, layer, choices)
    prompt = author_prompt(layer, preferences or prefs.text)
    write_author_files(layer, prefs, prompt)
    for key, d in prefs.decisions.items():
        if d["source"] == "default" and d["value"]:
            say(f"default: {key} = {d['value']} ({d['because']})")
    if prefs.token_env:
        say(f"set {prefs.token_env} in the environment (or {layer}/local.env) before a run talks to the forge; "
            "al never reads or stores it")
    if agent == "mock":
        assert mock is not None
        name = app.name if app is not None else layer.name.removesuffix(".al")
        adopted = (layer / "adopted").is_dir()
        output = run_mock(layer, mock, {"name": name, "root": app_rel, "forge": forge_clause(prefs.decisions),
                                        "includes": INCLUDE_LAYER if adopted else "",
                                        "codebase_includes": INCLUDE_CODEBASE if adopted else ""})
        if output:
            say(output)
    else:
        run_claude(layer, prompt, say, raw=raw, kit=None if raw else al.dashboard_kit_module())
    if not definition.is_file():
        raise AuthorError(f"the agent did not write {definition}")
    try:
        al.compile_layer(layer)
    except al.CompilerUnavailable as exc:   # the file was never checked: not its fault
        raise al.CompilerUnavailable(f"{exc}\n{never_compiled(layer)}") from exc
    except al.tools_module().ToolMissing as exc:
        exc.hint = never_compiled(layer)
        raise
    except Exception as exc:  # noqa: BLE001 — AlError of tools/al: the compiler's own text
        raise AuthorError(f"{exc}\n{REPAIR_HINT}") from exc
    say(f"agentic.rkt compiles; next: al gen {layer}")
    return 0
