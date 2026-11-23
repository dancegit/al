"""The gate (adw_modules/review.py) applies the local ship-without-review limit and logs the limit
it decides with. Skipped until review.py, a protected path, declares READS_LOCAL_LIMIT."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from adw_modules import review, reviewpolicy

pytestmark = pytest.mark.skipif(
    not reviewpolicy.gate_reads_limit(),
    reason="REQUIRES HUMAN: review.py does not read the local limit yet (READS_LOCAL_LIMIT)")

OPTION = {"id": "zero-touch", "kind": "auto", "ship": "merge", "maxDiffLines": 2000, "allowedClasses": ["chore"],
          "requireChecks": [], "blockProtectedPaths": True}


class StubLayer:
    def __init__(self, layer_dir: Path) -> None:
        self.layer_dir = layer_dir
        self.default_branch = "main"

    def option(self, group: str, option_id: str) -> dict[str, Any]:
        assert (group, option_id) == ("reviews", "zero-touch")
        return dict(OPTION)

    def adw(self, name: str) -> dict[str, Any]:
        return {"review": "zero-touch", "steps": []}


def facts(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {"changes": "codebase", "layer_protected": [], "check_exits": {}, "require_exits": {},
            "verdict": {"success": True, "issues": []}, "klass": "chore", "diff_lines": 2500,
            "changed_files": ["src/app.py"], "protected": []}


@pytest.fixture
def gated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(review, "gather_facts", facts)
    state = SimpleNamespace(adw="zte", adw_id="abcd1234", klass="chore", ship={}, worktree=str(tmp_path))
    return StubLayer(tmp_path), state


def test_gate_uses_the_local_override_and_logs_it(gated, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    layer, state = gated
    decision = review.gate(layer, state, None)
    assert decision["ship"] is False and decision["reasons"] == ["diff-too-large:2500>2000"]

    reviewpolicy.write_setting(tmp_path, "zero-touch", 3000, who="Ada", at="2026-11-23T10:00:00Z")
    capsys.readouterr()
    decision = review.gate(layer, state, None)
    assert decision == {"action": "merge", "ship": True, "reasons": []}
    assert state.ship["gate"]["limit"] == {
        "option": "zero-touch", "limit": 3000, "source": "local override", "declared": 2000}
    assert "[adw abcd1234] gate: max-diff-lines 3000 (local override; declared 2000)" in capsys.readouterr().out


def test_gate_without_an_override_logs_the_declared_limit(gated, capsys: pytest.CaptureFixture[str]) -> None:
    layer, state = gated
    decision = review.gate(layer, state, None)
    assert decision["reasons"] == ["diff-too-large:2500>2000"]
    assert "[adw abcd1234] gate: max-diff-lines 2000 (declared)" in capsys.readouterr().out
    assert state.ship["gate"]["limit"]["source"] == "declared"
