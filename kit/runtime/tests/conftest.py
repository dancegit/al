from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

RUNTIME = Path(__file__).resolve().parents[1]
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))


@pytest.fixture
def app_repo(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    app.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(app)], check=True, capture_output=True, text=True, timeout=30)
    (app / "app.py").write_text("print('hello')\n", encoding="utf-8")
    test_script = app / "test.sh"
    test_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    test_script.chmod(0o755)
    subprocess.run(["git", "-C", str(app), "add", "app.py", "test.sh"], check=True, capture_output=True, text=True, timeout=30)
    subprocess.run(["git", "-C", str(app), "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-m", "initial"], check=True, capture_output=True, text=True, timeout=30)
    return app


@pytest.fixture
def make_layer(tmp_path: Path, app_repo: Path):
    def factory(ir_overrides: dict[str, Any] | None = None) -> Path:
        layer = tmp_path / "layer"
        layer.mkdir(exist_ok=True)
        base: dict[str, Any] = {
            "schemaVersion": "agentic-layer-v1", "name": "test-layer", "description": "test",
            "codebase": {"root": str(app_repo), "defaultBranch": "main", "paths": {"protected": []}, "commands": []},
            "agents": [{"id": "mock-agent", "kind": "mock", "scriptDir": "mock", "status": "stable", "timeout": 30}],
            "environments": [{"id": "worktree", "kind": "worktree", "dir": "trees", "copyFiles": [], "portBase": 19000, "portsPerRun": 2, "keep": "never", "status": "stable"}],
            "reviews": [{"id": "auto", "kind": "auto", "status": "stable", "artefacts": [], "command": "review", "reviewers": 1, "maxRetries": 0, "allowedClasses": ["chore"], "blockProtectedPaths": True, "maxDiffLines": 100, "requireChecks": [], "ship": "pr", "deployCmd": ""}],
            "storage": {"id": "local", "kind": "local", "status": "stable", "dir": "artefacts"},
            "voice": None,
            "commands": [{"name": "review", "template": "review", "modelSet": "base", "output": "json", "description": "", "file": "", "focus": [], "fabric": [], "relevant": [], "validation": []}],
            "hops": [], "artefacts": [], "adws": [],
        }
        def merge(target: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
            for key, value in updates.items():
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                    merge(target[key], value)
                else:
                    target[key] = copy.deepcopy(value)
            return target
        ir = merge(base, ir_overrides or {})
        (layer / "agentic-layer.json").write_text(json.dumps(ir, indent=2), encoding="utf-8")
        for name in {item["name"] for item in ir.get("commands", [])}:
            (layer / "commands").mkdir(exist_ok=True)
            (layer / "commands" / f"{name}.md").write_text(f"# {name}\n$ARGUMENTS\n", encoding="utf-8")
        for name in {item["name"] for item in ir.get("hops", [])}:
            (layer / "hops").mkdir(exist_ok=True)
            (layer / "hops" / f"{name}.md").write_text(f"# {name}\n$ARGUMENTS\n", encoding="utf-8")
        return layer
    return factory
