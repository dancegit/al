"""Declarations that compile but have no effect are reported as `warning:` lines on stderr by
`al compile`, `al gen` and `al check`; the compile still succeeds and stdout stays the IR."""
from __future__ import annotations

import json

from conftest import LAYER_TEMPLATE, al

ENVIRONMENTS = ('  (environment worktree)\n'
                '  (environment worktree #:id spare)\n'
                '  (environment podman #:id box #:agent-inside #f #:credentials (claude))\n')


def test_compile_and_gen_print_warnings_and_succeed(app) -> None:
    layer = app.parent / "notes.al"
    layer.mkdir()
    source = LAYER_TEMPLATE.format(root="../notes", placement="(layer sibling)")
    source = source.replace("  (command plan ", ENVIRONMENTS + "  (command plan ", 1)
    source = source.replace('(adw change #:description "Plan, build, test; a person reviews"',
                            '(adw change #:description "Plan, build, test; a person reviews" #:environment (worktree box)')
    (layer / "agentic.rkt").write_text(source)
    cp = al("compile", layer)
    ir = json.loads(cp.stdout)                                      # stdout is only the IR
    assert [e["id"] for e in ir["environments"]] == ["worktree", "spare", "box"]
    assert "al: warning: environment `spare` is used by no workflow" in cp.stderr.splitlines(), cp.stderr
    assert ("al: warning: #:credentials on (environment podman #:id box) has no effect without #:agent-inside #t"
            in cp.stderr.splitlines()), cp.stderr
    assert cp.stderr.count("warning:") == 2
    cp = al("gen", layer)
    assert cp.stderr.count("warning:") == 2 and "used by no workflow" in cp.stderr
    assert (layer / "env" / "Containerfile").is_file()
    cp = al("check", layer)
    assert "used by no workflow" in cp.stderr and "check: ok" in cp.stdout


def test_voice_helper_on_a_codex_agent_warns(app) -> None:
    layer = app.parent / "notes.al"
    layer.mkdir()
    source = LAYER_TEMPLATE.format(root="../notes", placement="(layer sibling)")
    source = source.replace("  (command plan ", "  (agent codex)\n  (voice kokoro)\n  (command plan ", 1)
    (layer / "agentic.rkt").write_text(source)
    cp = al("compile", layer)
    assert json.loads(cp.stdout)["voice"]["dashboardHelper"] is True
    assert ("al: warning: (voice kokoro): the dashboard helper needs an agent that runs with no tools "
            "(claude-code or mock); agent `codex` is codex, so the panel will speak and listen but cannot answer. "
            "Set #:helper-agent or #:dashboard-helper #f") in cp.stderr.splitlines(), cp.stderr
    (layer / "agentic.rkt").write_text(source.replace("(voice kokoro)", "(voice kokoro #:dashboard-helper #f)"))
    cp = al("compile", layer)
    assert "dashboard helper" not in cp.stderr, cp.stderr
