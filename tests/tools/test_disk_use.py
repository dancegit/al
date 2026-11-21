"""Bounded disk use: every suite keeps only the temp directories of a failed session (one session), and
`al doctor` shows where the space went (its `disk:` lines and the `disk` object of --json)."""
from __future__ import annotations

import configparser
import json
import os

import fake_podman
from conftest import ROOT, al

SUITES = ("kit/runtime", "kit/dashboard", "tests/tools", "tests/integration")


def test_every_suite_keeps_only_failed_tmp_paths():
    for suite in SUITES:
        path = ROOT / suite / "pytest.ini"
        assert path.is_file(), f"{suite} has no pytest.ini"
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(path, encoding="utf-8")
        assert parser.has_section("pytest"), f"{path} has no [pytest]"
        section = parser["pytest"]
        assert section.get("tmp_path_retention_policy") == "failed", path
        assert section.get("tmp_path_retention_count") == "1", path


def test_al_doctor_prints_disk_lines_and_json(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "podman"
    fake_podman.install(bin_dir, state)
    fake_podman.add_image(state, "al-sandbox-test:v2", {"al.test": "1", "al.test.session": "1-abcdef"})
    fake_podman.add_image(state, "al-orchard:latest", {})
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "AL_PODMAN": ""}
    text = al("doctor", cwd=tmp_path, env=env, ok=False)
    assert text.returncode in (0, 4), text.stdout + text.stderr
    disk = [line for line in text.stdout.splitlines() if line.startswith("disk: ")]
    assert any(line.startswith("disk: images layer 0 (") and "other al 1 (" in line for line in disk), disk
    assert any(line.startswith("disk: test leftovers 0 containers (0 running), 1 images") for line in disk), disk
    assert any(line.startswith("disk: pytest temp ") for line in disk), disk
    assert any(line.startswith("disk: free ") and " on " in line for line in disk), disk
    assert not any(line.startswith("disk: worktrees") for line in disk)       # no layer given
    as_json = al("doctor", "--json", cwd=tmp_path, env=env, ok=False)
    assert as_json.returncode in (0, 4), as_json.stdout + as_json.stderr
    report = json.loads(as_json.stdout)["disk"]
    assert report["free"]["total"] > 0
    assert report["leftovers"]["images"] == 1 and report["images"]["other"]["count"] == 1
    assert report["images"]["test"]["count"] == 1
