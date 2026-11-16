"""`al scout` detects the branching style a repository uses and proposes a delivery preset, with
its evidence, read-only (fixture histories from delivery_histories.py; no network)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import delivery_histories as histories
from conftest import ROOT, al

sys.path.insert(0, str(ROOT / "tools"))
import al_delivery  # noqa: E402


def _detect(app: Path) -> dict:
    tracked = subprocess.run(["git", "-C", str(app), "ls-files"], capture_output=True, text=True).stdout.split()
    return al_delivery.detect(app, tracked)


def test_detects_trunk(tmp_path):
    d = _detect(histories.trunk(tmp_path / "shop"))
    assert d["style"] == "trunk" and d["proposal"] == "(delivery trunk)" and not d["unverified"]
    assert any("feature/login" in line for line in d["evidence"])
    assert any("CI runs on main" in line for line in d["evidence"])


def test_detects_sprint(tmp_path):
    d = _detect(histories.sprint(tmp_path / "shop"))
    assert d["style"] == "sprint"
    assert d["proposal"] == "(delivery sprint #:length-days 14)"
    assert any("sprint/2026-s01" in line and "sprint/2026-s07" in line for line in d["evidence"])
    assert any("every 14 days" in line for line in d["evidence"])


def test_detects_gitflow(tmp_path):
    d = _detect(histories.gitflow(tmp_path / "shop"))
    assert d["style"] == "gitflow" and d["proposal"] == "(delivery gitflow)"
    joined = " ".join(d["evidence"])
    assert "`develop` branch" in joined and "feature/for-1.0" in joined and "release/1.1" in joined
    assert "CONTRIBUTING.md" in joined


def test_detects_release_branches(tmp_path):
    d = _detect(histories.release_branches(tmp_path / "shop"))
    assert d["style"] == "release-branch" and d["preset"] == "release-train"
    assert d["proposal"] == '(delivery release-train #:length-days 0 #:name "{version}")'
    assert any("v1.0.0" in line for line in d["evidence"])


def test_no_signal_proposes_trunk_unverified(tmp_path):
    repo = histories.Repo(tmp_path / "shop")
    repo.commit("one more")
    d = _detect(repo.path)
    assert d["style"] == "trunk" and d["unverified"] is True
    assert "UNVERIFIED" in al_delivery.render_section(d)


def test_detection_is_read_only(tmp_path):
    app = histories.gitflow(tmp_path / "shop")
    sys.path.insert(0, str(ROOT / "tools"))
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("al_tool", str(ROOT / "tools" / "al"))
    spec = importlib.util.spec_from_loader("al_tool", loader)
    tool = importlib.util.module_from_spec(spec)
    loader.exec_module(tool)
    refs = lambda: subprocess.run(["git", "-C", str(app), "for-each-ref"], capture_output=True, text=True).stdout
    before, fingerprint = refs(), tool.app_fingerprint(app)
    facts = json.loads(al("scout", app, "--json").stdout)
    assert facts["delivery"]["style"] == "gitflow"
    assert refs() == before and tool.app_fingerprint(app) == fingerprint


def test_llm_txt_draft_has_a_delivery_section(tmp_path):
    app = histories.sprint(tmp_path / "shop")
    out = tmp_path / "LLM.txt"
    al("scout", app, "-o", out)
    text = out.read_text()
    assert "## Delivery" in text
    assert "(delivery sprint #:length-days 14)" in text and "What speaks for it" in text
    assert text.index("## Forge") < text.index("## Delivery") < text.index("## Conditional docs")
