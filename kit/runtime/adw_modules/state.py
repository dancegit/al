from __future__ import annotations

import json
import re
import secrets
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import LayerError


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ADWState:
    adw_id: str
    adw: str
    prompt: str = ""
    source: str = "manual"
    klass: str = "chore"
    class_fixed: bool = False
    dry_run: bool = False
    model_set: str = ""
    branch: str = ""
    worktree: str = ""
    ports: list[int] = field(default_factory=list)
    plan: str = ""
    outputs: dict[str, str] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    artefacts: list[dict[str, Any]] = field(default_factory=list)
    review: dict[str, Any] = field(default_factory=dict)
    ship: dict[str, Any] = field(default_factory=dict)
    fanout: dict[str, dict[str, Any]] = field(default_factory=dict)  # (fan-out NAME …) → candidates, picked, pick
    status: str = "running"
    started: str = field(default_factory=now)
    ended: str = ""

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-fA-F]{8}", self.adw_id):
            raise LayerError("adw_id must contain exactly 8 hexadecimal characters")

    def step_record(self, name: str, kind: str) -> dict[str, Any]:
        """Return the mutable record of step `name`, creating a pending one on first use."""
        for record in self.steps:
            if record.get("name") == name:
                return record
        record = {"name": name, "kind": kind, "status": "pending", "attempts": 0,
                  "started": "", "ended": "", "detail": ""}
        self.steps.append(record)
        return record

    @property
    def passed_steps(self) -> set[str]:
        return {str(step["name"]) for step in self.steps if step.get("status") == "passed"}

    def save(self, layer_dir: Path) -> Path:
        target = run_dir(layer_dir, self.adw_id) / "state.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
            json.dump(asdict(self), handle, indent=2, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(target)
        return target


def run_dir(layer_dir: Path, adw_id: str) -> Path:
    return Path(layer_dir) / "agents" / adw_id


def new_adw_id() -> str:
    return secrets.token_hex(4)


def load(layer_dir: Path, adw_id: str) -> ADWState:
    path = run_dir(layer_dir, adw_id) / "state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ADWState(**data)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise LayerError(f"Cannot load state for {adw_id}: {exc}") from exc
