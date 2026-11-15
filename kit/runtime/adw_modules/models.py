"""Model tiers: which model each agent call runs on, and why.

An agent declares named tiers in the IR (`models: [{tier, model, effort}]`; `base` and `heavy`
always exist for claude-code, codex and custom agents). Every agent call asks for a tier; this
module turns that into a model id, applying the run's overrides:

  1. `--model-set TIER` / AL_MODEL_SET           every agent call of the run uses TIER      (override)
  2. the request's tier (task tag, issue keyword)  run steps without their own #:model-set  (request)
  3. the run step's #:model-set                                                               (step)
  4. the command's / hop's #:model-set (base for commands, heavy for hops when not written)   (command)

and then, which model a tier means (first match wins):

  --model TIER=MODEL_ID > AL_MODELS > local setting (agents/settings/models.json, read when a run
  starts) > the layer definition > built-in default

The first two are this run's overrides (model_from "override"), the local setting is a per-layer,
never committed file written by `al models LAYER --set TIER=ID` or the dashboard (model_from
"local"); the last two are what the compiled IR says (model_from "agent"). Flags win over
environment variables. A run records the source of each tier's model in state.json (`models`).

A layer compiled before tiers existed has `modelBase` / `modelHeavy` on its agents; those are read
as the tiers base and heavy. The mock agent (and an agent with no model information) accepts every
tier and has no model id.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import LayerError

TIER_RX = re.compile(r"^[a-z][a-z0-9-]*$")
ENV_TIER = "AL_MODEL_SET"
ENV_MODELS = "AL_MODELS"
ENV_MAX_COST = "AL_MAX_COST"
TIER_FROM = ("override", "request", "step", "command")
SETTINGS_FILE = Path("agents") / "settings" / "models.json"   # the layer's local model setting, never committed
KNOWN_MODELS = ("claude-opus-5-5", "claude-fable-5-2", "claude-sonnet-5-5", "claude-haiku-5-5", "gpt-5.4",
                "gpt-5.4-mini")
# mirrors the defaults of racket/al/private/vocab.rkt; tests/tools/test_generated_references.py holds them equal
BUILTIN_TIERS = {"claude-code": {"base": "claude-opus-5-5", "heavy": "claude-opus-5-5"},
                 "codex": {"base": "gpt-5.4-mini", "heavy": "gpt-5.4"}}
MODEL_ID_RX = re.compile(r"^[^\s\x00-\x1f\x7f]{1,200}$")   # non-empty, no whitespace, no control characters
SOURCES = ("--model", "AL_MODELS", "local setting", "layer", "built-in default")


def agent_tiers(agent: Mapping[str, Any]) -> dict[str, dict[str, str]] | None:
    """tier -> {"model", "effort"}; None when the agent takes any tier (mock, or no model information)."""
    models = agent.get("models")
    if isinstance(models, list):
        return {str(m.get("tier")): {"model": str(m.get("model") or ""), "effort": str(m.get("effort") or "default")}
                for m in models if isinstance(m, Mapping) and m.get("tier")}
    if "modelBase" in agent or "modelHeavy" in agent:  # IR compiled before named tiers
        return {"base": {"model": str(agent.get("modelBase") or ""), "effort": "default"},
                "heavy": {"model": str(agent.get("modelHeavy") or ""), "effort": "default"}}
    return None


def declared_tiers(layer: Any) -> list[str]:
    """Every tier any agent of the layer declares, in declaration order (base and heavy at least)."""
    seen: list[str] = []
    for agent in layer.ir.get("agents") or []:
        for tier in (agent_tiers(agent) or {"base": {}, "heavy": {}}):
            if tier not in seen:
                seen.append(tier)
    return seen or ["base", "heavy"]


def parse_models(text: str, where: str) -> dict[str, str]:
    """"heavy=claude-x,base=claude-y" -> {"heavy": "claude-x", "base": "claude-y"}."""
    out: dict[str, str] = {}
    for item in (part.strip() for part in text.split(",")):
        if not item:
            continue
        tier, sep, model = item.partition("=")
        tier, model = tier.strip(), model.strip()
        if not sep or not TIER_RX.match(tier) or not model:
            raise ValueError(f"{where}: expected TIER=MODEL_ID (e.g. heavy=claude-fable-5-2), got {item!r}")
        out[tier] = model
    return out


def check_model_id(model: str) -> str | None:
    """Raises ValueError for an id that cannot be a model id; returns a note for one al does not know."""
    if not isinstance(model, str) or not MODEL_ID_RX.fullmatch(model):
        raise ValueError(f"model id must be a non-empty string without whitespace, got {model!r}")
    if model not in KNOWN_MODELS:
        return f"{model} is not a known model id (known: {', '.join(KNOWN_MODELS)}); it is passed to the agent as is"
    return None


def check_setting(tier: str, model: str, known_tiers: list[str]) -> str | None:
    """A local setting TIER=MODEL: the tier must be declared by an agent of the layer."""
    if not isinstance(tier, str) or not TIER_RX.fullmatch(tier) or tier not in known_tiers:
        raise ValueError(f"no agent of this layer has a tier `{tier}`; declared: {', '.join(known_tiers)}")
    return check_model_id(model)


def render_settings(mapping: Mapping[str, str]) -> str:
    """The one format of agents/settings/models.json (the dashboard writes it byte for byte alike)."""
    return json.dumps({"version": 1, "models": dict(sorted(mapping.items()))}, indent=2, sort_keys=True) + "\n"


def read_settings(layer_dir: Path) -> dict[str, str]:
    """The layer's local setting, {tier: model id}; {} when there is none."""
    path = Path(layer_dir) / SETTINGS_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise LayerError(f"{path}: cannot read it ({exc.strerror}); fix or remove it (al models LAYER --reset)") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LayerError(f"{path}: not JSON ({exc.msg}); fix or remove it (al models LAYER --reset)") from exc
    mapping = data.get("models") if isinstance(data, dict) else None
    if not isinstance(mapping, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
        raise LayerError(f'{path}: expected {{"version": 1, "models": {{TIER: MODEL_ID}}}}; '
                         "fix or remove it (al models LAYER --reset)")
    for tier, model in mapping.items():
        try:
            if not TIER_RX.fullmatch(tier):
                raise ValueError(f"{tier!r} is not a tier name")
            check_model_id(model)
        except ValueError as exc:
            raise LayerError(f"{path}: {exc}; fix or remove it (al models LAYER --reset)") from exc
    return dict(mapping)


def write_settings(layer_dir: Path, mapping: Mapping[str, str]) -> Path:
    """Write the local setting atomically; an empty mapping removes the file."""
    path = Path(layer_dir) / SETTINGS_FILE
    if not mapping:
        path.unlink(missing_ok=True)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False,
                                         prefix=".models.", suffix=".tmp")
    try:
        with handle:
            handle.write(render_settings(mapping))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return path


def local_settings(layer: Any) -> tuple[dict[str, str], list[str]]:
    """The local setting's entries for tiers the layer declares, and notes about the ignored others."""
    known = declared_tiers(layer)
    settings, notes = {}, []
    for tier, model in read_settings(layer.layer_dir).items():
        if tier in known:
            settings[tier] = model
        else:
            notes.append(f"local setting for tier `{tier}` ignored: no agent declares it")
    return settings, notes


def parse_cost(text: str, where: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{where}: expected an amount in US dollars, e.g. 5 or 2.50, got {text!r}") from None
    if not value > 0 or value != value or value == float("inf"):
        raise ValueError(f"{where}: must be greater than 0, got {text!r}")
    return value


def overrides(model_set: str | None, models: list[str] | None, max_cost: str | None,
              environ: Mapping[str, str] | None = None, settings: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The run's overrides from command-line flags and environment variables (flags win), on top of
    the layer's local setting (`settings`, see local_settings). `models` is the tier remap and
    `models_from` names the source of each of its entries. Raises ValueError with a message for
    the person who typed it."""
    environ = os.environ if environ is None else environ
    result: dict[str, Any] = {"tier": "", "models": {}, "models_from": {}, "max_cost": None, "from": {}}
    tier = (model_set or "").strip()
    if tier:
        result["from"]["tier"] = "--model-set"
    elif environ.get(ENV_TIER, "").strip():
        tier = environ[ENV_TIER].strip()
        result["from"]["tier"] = ENV_TIER
    if tier and not TIER_RX.match(tier):
        raise ValueError(f"{result['from']['tier']}: {tier!r} is not a tier name (lowercase letters, digits, dashes)")
    result["tier"] = tier
    remap = {str(k): str(v) for k, v in (settings or {}).items()}
    models_from = {k: "local setting" for k in remap}
    environment_models = parse_models(environ.get(ENV_MODELS, ""), ENV_MODELS)
    if environment_models:
        result["from"]["models"] = ENV_MODELS
        remap.update(environment_models)
        models_from.update({k: ENV_MODELS for k in environment_models})
    flagged = {}
    for item in models or []:
        flagged.update(parse_models(item, "--model"))
    if flagged:
        given = {**environment_models, **flagged}
        result["from"]["models"] = "--model" if len(flagged) == len(given) else f"--model and {ENV_MODELS}"
        remap.update(flagged)
        models_from.update({k: "--model" for k in flagged})
    result["models"], result["models_from"] = remap, models_from
    if max_cost:
        result["max_cost"], result["from"]["max_cost"] = parse_cost(max_cost, "--max-cost"), "--max-cost"
    elif environ.get(ENV_MAX_COST, "").strip():
        result["max_cost"] = parse_cost(environ[ENV_MAX_COST].strip(), ENV_MAX_COST)
        result["from"]["max_cost"] = ENV_MAX_COST
    return result


def check_overrides(layer: Any, adw: Mapping[str, Any], over: Mapping[str, Any]) -> None:
    """A forced tier must exist on every agent the workflow uses; a remapped tier on at least one."""
    tier = str(over.get("tier") or "")
    if tier:
        for agent_id in agents_used(layer, adw):
            tiers = agent_tiers(layer.option("agents", agent_id))
            if tiers is not None and tier not in tiers:
                raise LayerError(f"{over.get('from', {}).get('tier', '--model-set')} {tier}: agent `{agent_id}` "
                                 f"(used by ADW `{adw.get('name')}`) has no tier `{tier}`; it declares: "
                                 f"{', '.join(tiers) or '(none)'}")
    known = declared_tiers(layer)
    for name in (over.get("models") or {}):
        if name not in known:
            raise LayerError(f"{over.get('from', {}).get('models', '--model')}: no agent of this layer has a tier "
                             f"`{name}`; declared: {', '.join(known)}")


def agents_used(layer: Any, adw: Mapping[str, Any]) -> list[str]:
    """The ADW's agent, every run step's own agent and the review option's agent."""
    used = [str(adw.get("agent") or "")]
    for step in adw.get("steps") or []:
        if step.get("kind") == "run" and step.get("agent"):
            used.append(str(step["agent"]))
    review = layer.option("reviews", adw["review"]) if adw.get("review") else {}
    if review.get("kind") in ("agent", "auto") and review.get("agent"):
        used.append(str(review["agent"]))
    return [a for i, a in enumerate(used) if a and a not in used[:i]]


@dataclass
class Choice:
    """What an agent call runs on and why."""
    tier: str
    tier_from: str      # override | request | step | command
    model: str
    model_from: str     # agent | local | override
    effort: str         # default | low | medium | high | xhigh | max

    def as_record(self) -> dict[str, str]:
        return {"tier": self.tier, "tier_from": self.tier_from, "model": self.model,
                "model_from": self.model_from, "effort": self.effort}

    def why(self) -> str:
        text = f"tier {self.tier} ({self.tier_from})"
        if self.model_from == "override":
            return text + " with its model overridden for this run"
        if self.model_from == "local":
            return text + " with its model from the layer's local setting"
        return text


def choose(agent: Mapping[str, Any], tier: str, tier_from: str, over: Mapping[str, Any] | None) -> Choice:
    """Apply the run's overrides to the tier a call asks for and find its model on `agent`."""
    over = over or {}
    if over.get("tier"):
        tier, tier_from = str(over["tier"]), "override"
    tiers = agent_tiers(agent)
    if tiers is not None and tier not in tiers:
        raise LayerError(f"agent `{agent.get('id')}` has no model tier `{tier}` (asked for by {tier_from}); "
                         f"it declares: {', '.join(tiers) or '(none)'}")
    spec = (tiers or {}).get(tier, {"model": "", "effort": "default"})
    model, model_from = spec["model"], "agent"
    remap = over.get("models") or {}
    if tier in remap:
        local = (over.get("models_from") or {}).get(tier) == "local setting"   # a state from before has none
        model, model_from = str(remap[tier]), "local" if local else "override"
    return Choice(tier, tier_from, model, model_from, spec.get("effort") or "default")


def resolve_tiers(layer: Any, agent_id: str, over: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """{tier: {"model", "source"}} for every tier of `agent_id` ({} for the mock or an agent without
    model information). The IR does not say whether a model was written or defaulted: a model equal
    to the built-in default of the agent's kind is reported as `built-in default`."""
    try:
        agent = layer.option("agents", agent_id)
    except LayerError:
        return {}
    remap, sources = over.get("models") or {}, over.get("models_from") or {}
    out: dict[str, dict[str, str]] = {}
    for tier, spec in (agent_tiers(agent) or {}).items():
        if tier in remap:
            out[tier] = {"model": str(remap[tier]), "source": str(sources.get(tier) or "--model")}
        else:
            default = BUILTIN_TIERS.get(str(agent.get("kind") or ""), {}).get(tier)
            out[tier] = {"model": spec["model"], "source": "built-in default" if spec["model"] == default else "layer"}
    return out


def has_tier(agent: Mapping[str, Any], tier: str) -> bool:
    tiers = agent_tiers(agent)
    return tiers is None or tier in tiers


def environment(over: Mapping[str, Any]) -> dict[str, str]:
    """The overrides as environment variables, for runs this run starts (fan-out candidates). The
    whole remap, local setting included, goes into AL_MODELS: a candidate keeps its parent's models
    even when the local setting changes meanwhile (its recorded source is then AL_MODELS)."""
    env: dict[str, str] = {}
    if over.get("tier"):
        env[ENV_TIER] = str(over["tier"])
    if over.get("models"):
        env[ENV_MODELS] = ",".join(f"{k}={v}" for k, v in over["models"].items())
    if over.get("max_cost"):
        env[ENV_MAX_COST] = str(over["max_cost"])
    return env
