"""Files generated from the vocabulary are committed; they must never go stale. The schema and the
language reference are regenerated from racket/al (cli.rkt), and the runtime's full example IR is
recompiled from racket/al/examples/full.rkt; each must equal the committed file byte for byte."""
from __future__ import annotations

import json
import os
import shutil
import sys

import pytest

from conftest import ROOT, al, sh

pytestmark = pytest.mark.skipif(shutil.which("racket") is None, reason="racket is not on PATH")

REGENERATE = "regenerate it: racket racket/al/cli.rkt {what} > {path} (with PLTCOLLECTS=\"$PWD/racket:\")"


@pytest.mark.parametrize("what, path", [("schema", "schemas/agentic-layer-v1.schema.json"),
                                        ("reference", "docs/AL-DSL.llms.txt")])
def test_committed_file_equals_what_the_vocabulary_generates(what, path) -> None:
    env = {**os.environ, "PLTCOLLECTS": f"{ROOT / 'racket'}:"}
    cp = sh(["racket", ROOT / "racket" / "al" / "cli.rkt", what], cwd=ROOT, env=env)
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout == (ROOT / path).read_text(), f"{path} is stale; " + REGENERATE.format(what=what, path=path)


def test_the_runtime_s_full_example_ir_equals_the_compiled_full_example(tmp_path) -> None:
    shutil.copy(ROOT / "racket" / "al" / "examples" / "full.rkt", tmp_path / "agentic.rkt")
    compiled = al("compile", tmp_path).stdout
    committed = (ROOT / "kit" / "runtime" / "tests" / "full.layer.json").read_text()
    assert compiled == committed, ("kit/runtime/tests/full.layer.json is stale; regenerate it: copy "
                                   "racket/al/examples/full.rkt to DIR/agentic.rkt, then tools/al compile DIR > "
                                   "kit/runtime/tests/full.layer.json")


VOCABULARY_LAYER = """#lang al
(agentic-layer #:name "defaults"
  (codebase #:root "../app" #:languages (python) (cmd test "true"))
  (agent claude-code)
  (agent codex #:id cx)
  (command plan #:template plan-chore #:model-set heavy #:output path)
  (adw plan (steps (run plan #:command plan #:args ("$prompt")))))
"""


def _builtin_tiers() -> list[tuple[str, str, str]]:
    if str(ROOT / "kit" / "runtime") not in sys.path:
        sys.path.insert(0, str(ROOT / "kit" / "runtime"))
    from adw_modules import models
    return [(kind, tier, model) for kind, tiers in models.BUILTIN_TIERS.items() for tier, model in tiers.items()]


@pytest.mark.parametrize("kind, tier, model", _builtin_tiers())
def test_runtime_builtin_tiers_equal_the_vocabulary_defaults(tmp_path, kind, tier, model) -> None:
    """The runtime tells `built-in default` from `layer` by comparing with BUILTIN_TIERS, so it must
    equal what the vocabulary gives an agent that names no models."""
    (tmp_path / "agentic.rkt").write_text(VOCABULARY_LAYER)
    ir = json.loads(al("compile", tmp_path).stdout)
    agent = next(a for a in ir["agents"] if a["kind"] == kind)
    compiled = {m["tier"]: m["model"] for m in agent["models"]}
    assert compiled[tier] == model, (f"{kind} {tier}: the vocabulary says {compiled[tier]!r}, "
                                     f"kit/runtime/adw_modules/models.py BUILTIN_TIERS says {model!r}")
