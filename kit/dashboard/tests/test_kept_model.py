"""The run page's "Kept on a branch" data: what the runtime kept of a run whose workflow has no ship
step (kit/runtime keep.py), with links to the kept files and the diff. No network, no browser."""
from __future__ import annotations

import json
from pathlib import Path

import fixtures
from test_model import model_for

KEPT = "a100000c"
NOTHING = "a100000d"


def _artefact(layer: Path, run_id: str, artefact_id: str, relative: str, text: str) -> dict:
    path = f"artefacts/{run_id}/{artefact_id}/{relative}"
    fixtures.write(layer / path, text)
    return {"id": artefact_id, "type": "file", "step": "keep", "path": path, "bytes": len(text.encode()),
            "sha256": "0" * 64, "url": ""}


def _write_runs(layer: Path) -> None:
    records = [fixtures.rec("answer", "run", "passed", 0, 30, "Wrote the report.")]
    artefacts = [_artefact(layer, KEPT, "kept-files", "docs/report.md", "# Report\n\nfindings\n"),
                 _artefact(layer, KEPT, "kept-diff", "changes.diff", "+++ b/docs/report.md\n+# Report\n")]
    kept = {"changed": True, "workflow_ships": False, "branch": f"chore/{KEPT}-report", "commit": "c" * 40,
            "committed": True, "files": ["docs/report.md", "big.bin"], "count": 2, "left_uncommitted": [],
            "artefacts": 1, "too_large": [{"path": "big.bin", "bytes": 2000000}], "skipped": [],
            "diff": f"artefacts/{KEPT}/kept-diff/changes.diff"}
    state = fixtures.state(KEPT, "ask", "Write the report", "passed", 0, 30, records, artefacts=artefacts, kept=kept)
    fixtures.write(layer / "agents" / KEPT / "state.json", json.dumps(state, indent=2))
    fixtures.write(layer / "agents" / KEPT / "run.log", "=== ADW summary ===\nstatus:    passed\n")
    nothing = fixtures.state(NOTHING, "ask", "Look around", "passed", 0, 30, records,
                             kept={"changed": False, "workflow_ships": False})
    fixtures.write(layer / "agents" / NOTHING / "state.json", json.dumps(nothing, indent=2))
    fixtures.write(layer / "agents" / NOTHING / "run.log", "=== ADW summary ===\nstatus:    passed\n")


def test_kept_run_shows_branch_files_and_diff(fx):
    _write_runs(fx["layer"])
    model = model_for(fx)
    detail = model.run_detail("demo-shop", KEPT)
    kept = detail["kept"]
    assert kept["changed"] is True
    assert kept["branch"] == f"chore/{KEPT}-report" and kept["commit"] == "c" * 12
    assert kept["count"] == 2
    assert kept["files"] == [{"path": "docs/report.md", "artefact": f"artefacts/{KEPT}/kept-files/docs/report.md"},
                             {"path": "big.bin", "artefact": ""}]
    assert kept["diff"] == f"artefacts/{KEPT}/kept-diff/changes.diff"
    assert kept["too_large"] == [{"path": "big.bin", "bytes": 2000000}]
    documents = {doc["path"] for doc in detail["documents"]}
    assert f"artefacts/{KEPT}/kept-files/docs/report.md" in documents
    assert f"artefacts/{KEPT}/kept-diff/changes.diff" in documents

    assert model.run_detail("demo-shop", NOTHING)["kept"]["changed"] is False
    assert model.run_detail("demo-shop", "a1000002")["kept"] is None
