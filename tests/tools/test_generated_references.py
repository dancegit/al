"""Files generated from the vocabulary are committed; they must never go stale. The schema and the
language reference are regenerated from racket/al (cli.rkt), and the runtime's full example IR is
recompiled from racket/al/examples/full.rkt; each must equal the committed file byte for byte."""
from __future__ import annotations

import os
import shutil

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
