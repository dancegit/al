from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import LayerError

IR_FILE = "agentic-layer.json"

_LIST_FAMILIES = {
    "agent": "agents", "agents": "agents",
    "environment": "environments", "environments": "environments",
    "artefact": "artefacts", "artefacts": "artefacts",
    "review": "reviews", "reviews": "reviews",
    "trigger": "triggers", "triggers": "triggers",
    "promptInput": "promptInputs", "promptInputs": "promptInputs", "prompt-input": "promptInputs",
}
_SINGLETON_FAMILIES = ("forge", "storage", "layer", "voice")


def _refuse_planned(family: str, entry: dict[str, Any]) -> None:
    if entry.get("status") == "planned":
        raise LayerError(
            f"{family} option {entry.get('id')!r} (kind {entry.get('kind')!r}) has status 'planned' "
            "and is not implemented by this runtime"
        )


@dataclass(frozen=True)
class Layer:
    ir: dict[str, Any]
    layer_dir: Path
    app_root: Path

    @property
    def name(self) -> str:
        return str(self.ir.get("name") or self.layer_dir.name)

    @property
    def default_branch(self) -> str:
        return str(self.ir["codebase"].get("defaultBranch") or "main")

    @property
    def in_repo_dir(self) -> str:
        """The layer directory relative to the repository root when the layer is checked in with
        the application (`layer in-repo`), else ""."""
        placement = self.ir.get("layer") or {}
        if not isinstance(placement, dict) or placement.get("kind") != "in-repo":
            return ""
        return str(placement.get("dir") or "agentic").strip("/")

    def changes_layer(self, adw_name: str) -> bool:
        """True for a layer-change workflow (`#:changes layer`), e.g. the one `al improve` runs."""
        return str(self.adw(adw_name).get("changes") or "codebase") == "layer"

    def option(self, family_key: str, option_id: str) -> dict[str, Any]:
        """Return the option `option_id` of a family; singletons ignore option_id when it is empty."""
        if family_key in _SINGLETON_FAMILIES:
            return self.singleton(family_key, option_id)
        key = _LIST_FAMILIES.get(family_key)
        if key is None:
            raise LayerError(f"Unknown option family: {family_key}")
        entries = self.ir.get(key, [])
        if not isinstance(entries, list):
            raise LayerError(f"IR family {key!r} must be a list")
        for entry in entries:
            if isinstance(entry, dict) and entry.get("id") == option_id:
                _refuse_planned(key, entry)
                if not isinstance(entry.get("kind"), str):
                    raise LayerError(f"Option {key}:{option_id} has no kind")
                return entry
        raise LayerError(f"Unknown {key} option: {option_id}")

    def singleton(self, family: str, option_id: str = "") -> dict[str, Any]:
        entry = self.ir.get(family)
        if not isinstance(entry, dict):
            raise LayerError(f"IR has no {family} option")
        if option_id and entry.get("id") != option_id:
            raise LayerError(f"Unknown {family} option: {option_id}")
        _refuse_planned(family, entry)
        return entry

    def command(self, name: str) -> dict[str, Any]:
        for key, is_hop in (("commands", False), ("hops", True)):
            for entry in self.ir.get(key, []):
                if isinstance(entry, dict) and entry.get("name") == name:
                    return {**entry, "isHop": is_hop}
        raise LayerError(f"Unknown command or hop: {name}")

    def cmd(self, name: str) -> dict[str, Any]:
        for entry in self.ir["codebase"].get("commands", []):
            if isinstance(entry, dict) and entry.get("name") == name:
                return entry
        raise LayerError(f"Unknown codebase command: {name}")

    def adw(self, name: str) -> dict[str, Any]:
        for entry in self.ir.get("adws", []):
            if isinstance(entry, dict) and entry.get("name") == name:
                return entry
        raise LayerError(f"Unknown ADW: {name}")

    def validate_adw(self, name: str) -> dict[str, Any]:
        """Resolve every option an ADW references so planned or missing options fail before any work."""
        adw = self.adw(name)
        self.option("agents", adw["agent"])
        self.option("reviews", adw["review"])
        for env_id in adw.get("environment", []):
            self.option("environments", env_id)
        for input_id in adw.get("input", []):
            self.option("promptInputs", input_id)
        for step in adw.get("steps", []):
            if step.get("kind") == "run":
                self.command(step["command"])
            elif step.get("kind") in ("check", "script"):
                self.cmd(step["cmd"])
        review = self.option("reviews", adw["review"])
        if review.get("agent"):
            self.option("agents", review["agent"])
        return adw


def load(layer_dir: Path) -> Layer:
    """Load `<layer_dir>/agentic-layer.json` (or an IR file path) and refuse planned singletons."""
    given = Path(layer_dir).resolve()
    root = given.parent if given.is_file() else given
    path = given if given.is_file() else root / IR_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LayerError(f"Cannot load {path}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("codebase"), dict):
        raise LayerError(f"{path}: IR must be an object with a codebase object")
    codebase_root = raw["codebase"].get("root")
    if not isinstance(codebase_root, str) or not codebase_root:
        raise LayerError(f"{path}: codebase.root must be a non-empty path")
    for family in _SINGLETON_FAMILIES:
        entry = raw.get(family)
        if isinstance(entry, dict):
            _refuse_planned(family, entry)
    return Layer(raw, root, app_root(root, codebase_root))


APP_ROOT_ENV = "AL_APP_ROOT"


def app_root(layer_dir: Path, codebase_root: str) -> Path:
    """Where the application checkout is: `codebase.root` resolved against the layer directory,
    unless AL_APP_ROOT names it. The override exists for a layer that is checked out somewhere
    its relative root does not reach, e.g. in a worktree of the layer's own repository during
    `al improve`; the definition is never rewritten for that."""
    override = os.environ.get(APP_ROOT_ENV, "").strip()
    if override:
        path = Path(override).expanduser().resolve()
        if not path.is_dir():
            raise LayerError(f"{APP_ROOT_ENV}={override} is not a directory")
        return path
    return (Path(layer_dir) / codebase_root).resolve()
