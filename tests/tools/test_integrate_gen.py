"""What `al gen`, `al check`, `al eject` and `al explain` do with the kit workflow `integrate`: every
generated layer has `adws/integrate.py` and `commands/resolve-conflicts.md`, although the
definition declares neither."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys

from conftest import AL, KIT, ROOT, al, manifest


def _tool():
    """tools/al loaded as a module (it has no .py suffix), for its constants."""
    loader = importlib.machinery.SourceFileLoader("al_tool_for_integrate", str(AL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def test_gen_writes_integrate_script_and_resolve_prompt(sibling) -> None:
    script = sibling / "adws" / "integrate.py"
    prompt = sibling / "commands" / "resolve-conflicts.md"
    assert script.is_file() and os.access(script, os.X_OK)
    assert script.read_text().startswith("#!/usr/bin/env -S uv run --quiet --script\n")
    assert "integrate.main(Path(__file__).resolve().parent.parent, sys.argv[1:])" in script.read_text()
    assert (sibling / "adws" / "adw_modules" / "integrate.py").is_file()
    text = prompt.read_text()
    assert "{{" not in text and "description: resolve-conflicts for notes" in text
    assert "`resolve-conflicts` in layer `notes`" in text
    assert "## Your step" in text and "${workflow_steps}" in text and "### State of the worktree" in text
    assert "BRIEF: $1\nCONFLICTS: $2" in text and "${default_branch}" in text
    assert "- `test.sh`" in text, "the layer's protected paths are named"
    assert '{"resolved": [{"file": "", "sides": "", "resolution": ""}]' in text
    for rel in ("adws/integrate.py", "commands/resolve-conflicts.md"):
        assert rel in manifest(sibling)["files"], rel
    assert "ok" in al("check", sibling).stdout.lower()
    before = {p: p.read_bytes() for p in sibling.rglob("*") if p.is_file() and ".git" not in p.parts}
    al("gen", sibling)
    after = {p: p.read_bytes() for p in sibling.rglob("*") if p.is_file() and ".git" not in p.parts}
    assert after == before, "a second al gen changes nothing"
    explained = sibling.parent / "explained.md"
    al("explain", sibling, "--md", explained)
    assert "### Kit workflow: integrate (every layer has it)" in explained.read_text()


def test_check_reports_missing_integrate_script(sibling) -> None:
    (sibling / "adws" / "integrate.py").unlink()
    (sibling / "commands" / "resolve-conflicts.md").unlink()
    cp = al("check", sibling, ok=False)
    assert cp.returncode != 0
    out = cp.stdout + cp.stderr
    assert "missing adws/integrate.py" in out and "missing commands/resolve-conflicts.md" in out
    al("gen", sibling)
    al("check", sibling)


def test_eject_resolve_conflicts_template_is_used(sibling) -> None:
    listing = al("eject", sibling, "--list").stdout
    row = next(line for line in listing.splitlines() if line.startswith("commands/resolve-conflicts.md"))
    assert "integrate (kit workflow)" in row and "kit" in row
    out = al("eject", sibling, "command", "resolve-conflicts").stdout
    copy = sibling / "templates" / "commands" / "resolve-conflicts.md"
    assert copy.read_text() == (KIT / "templates/commands/resolve-conflicts.md").read_text()      # unrendered
    assert "integrate (kit workflow)" in out
    copy.write_text(copy.read_text().replace("## Relevant Files", "- LAYER RULE: migrations are never merged by hand.\n\n## Relevant Files"))
    al("gen", sibling)
    text = (sibling / "commands" / "resolve-conflicts.md").read_text()
    assert "LAYER RULE: migrations are never merged by hand." in text and "{{" not in text
    al("check", sibling)
    row = next(line for line in al("eject", sibling, "--list").stdout.splitlines()
               if line.startswith("commands/resolve-conflicts.md"))
    assert "edited in the layer" in row


def test_layer_command_of_that_name_wins(sibling) -> None:
    source = sibling / "agentic.rkt"
    (sibling / "prompts").mkdir()
    (sibling / "prompts" / "resolve.md").write_text("# Our own way to resolve conflicts\n\nBRIEF: $1\n")
    source.write_text(source.read_text().replace(
        "  (command commit #:template commit)",
        '  (command commit #:template commit)\n  (command resolve-conflicts #:template custom #:file "prompts/resolve.md")'))
    al("gen", sibling)
    text = (sibling / "commands" / "resolve-conflicts.md").read_text()
    assert "Our own way to resolve conflicts" in text and "# Resolve Conflicts" not in text
    al("check", sibling)
    listing = al("eject", sibling, "--list").stdout
    assert "integrate (kit workflow)" not in listing
    cp = al("eject", sibling, "command", "resolve-conflicts", ok=False)
    assert cp.returncode == 1 and "already uses a layer file" in cp.stderr


def test_start_layer_script_equals_generators() -> None:
    sys.path.insert(0, str(ROOT / "kit" / "dashboard" / "tests"))
    try:
        import start_layer
    finally:
        sys.path.pop(0)
    tool = _tool()
    assert start_layer.INTEGRATE_SCRIPT == tool.INTEGRATE_SCRIPT
    assert tool.INTEGRATE_SCRIPT.startswith('"""Integration of a handed-over run')
    assert "Generated by `al gen` for every layer — see adw_modules/integrate.py" in tool.INTEGRATE_SCRIPT
