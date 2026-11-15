"""Model tiers: which model each agent call runs on, and why.

An agent declares named tiers in the IR (`models: [{tier, model, effort}]`; `base` and `heavy`
always exist for claude-code, codex and custom agents). Every agent call asks for a tier; this
module turns that into a model id, applying the run's overrides:

  1. `--model-set TIER` / AL_MODEL_SET           every agent call of the run uses TIER      (override)
  2. the request's tier (task tag, issue keyword)  run steps without their own #:model-set  (request)
  3. the run step's #:model-set                                                               (step)
  4. the command's / hop's #:model-set (base for commands, heavy for hops when not written)   (command)

and then `--model TIER=MODEL` / AL_MODELS remaps a tier's model id for this run (model_from
"override", else "agent"). Flags win over environment variables.

A layer compiled before tiers existed has `modelBase` / `modelHeavy` on its agents; those are read
as the tiers base and heavy. The mock agent (and an agent with no model information) accepts every
tier and has no model id.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import LayerError

TIER_RX = re.compile(r"^[a-z][a-z0-9-]*$")
ENV_TIER = "AL_MODEL_SET"
ENV_MODELS = "AL_MODELS"
ENV_MAX_COST = "AL_MAX_COST"
TIER_FROM = ("override", "request", "step", "command")


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


def parse_cost(text: str, where: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{where}: expected an amount in US dollars, e.g. 5 or 2.50, got {text!r}") from None
    if not value > 0 or value != value or value == float("inf"):
        raise ValueError(f"{where}: must be greater than 0, got {text!r}")
    return value


def overrides(model_set: str | None, models: list[str] | None, max_cost: str | None,
              environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The run's overrides from command-line flags and environment variables (flags win).
    Raises ValueError with a message for the person who typed it."""
    environ = os.environ if environ is None else environ
    result: dict[str, Any] = {"tier": "", "models": {}, "max_cost": None, "from": {}}
    tier = (model_set or "").strip()
    if tier:
        result["from"]["tier"] = "--model-set"
    elif environ.get(ENV_TIER, "").strip():
        tier = environ[ENV_TIER].strip()
        result["from"]["tier"] = ENV_TIER
    if tier and not TIER_RX.match(tier):
        raise ValueError(f"{result['from']['tier']}: {tier!r} is not a tier name (lowercase letters, digits, dashes)")
    result["tier"] = tier
    remap = parse_models(environ.get(ENV_MODELS, ""), ENV_MODELS)
    if remap:
        result["from"]["models"] = ENV_MODELS
    flagged = {}
    for item in models or []:
        flagged.update(parse_models(item, "--model"))
    if flagged:
        remap.update(flagged)
        result["from"]["models"] = "--model" if len(flagged) == len(remap) else f"--model and {ENV_MODELS}"
    result["models"] = remap
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
    model_from: str     # agent | override
    effort: str         # default | low | medium | high | xhigh | max

    def as_record(self) -> dict[str, str]:
        return {"tier": self.tier, "tier_from": self.tier_from, "model": self.model,
                "model_from": self.model_from, "effort": self.effort}

    def why(self) -> str:
        text = f"tier {self.tier} ({self.tier_from})"
        return text + (" with its model overridden for this run" if self.model_from == "override" else "")


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
        model, model_from = str(remap[tier]), "override"
    return Choice(tier, tier_from, model, model_from, spec.get("effort") or "default")


def has_tier(agent: Mapping[str, Any], tier: str) -> bool:
    tiers = agent_tiers(agent)
    return tiers is None or tier in tiers


def environment(over: Mapping[str, Any]) -> dict[str, str]:
    """The overrides as environment variables, for runs this run starts (fan-out candidates)."""
    env: dict[str, str] = {}
    if over.get("tier"):
        env[ENV_TIER] = str(over["tier"])
    if over.get("models"):
        env[ENV_MODELS] = ",".join(f"{k}={v}" for k, v in over["models"].items())
    if over.get("max_cost"):
        env[ENV_MAX_COST] = str(over["max_cost"])
    return env
