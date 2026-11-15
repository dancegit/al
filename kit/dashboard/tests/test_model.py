"""Unit tests: discovery, aggregation, graphs, transcripts, ANSI, tailing. No network, no browser."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import dashboard as d
import fixtures


def model_for(fx: dict[str, Path], **kwargs) -> d.Model:
    model = d.Model([fx["layer"]], [], **kwargs)
    model.discover()
    model.tick()
    return model


def summary(model: d.Model, run_id: str) -> dict:
    return model.summaries[f"demo-shop/{run_id}"]


# ------------------------------------------------------------------------------ discovery

def test_scan_finds_layers_including_in_repo_ones_and_prunes(fx):
    found = d.find_layers([fx["root"]])
    names = sorted(p.name for p in found)
    assert names == ["demo-shop.al", "other"]  # node_modules/pkg is pruned
    assert all(p.is_absolute() for p in found)


def test_scan_depth_limits_the_walk(fx):
    assert [p.name for p in d.find_layers([fx["root"]], depth=1)] == ["demo-shop.al"]


def test_layer_flag_and_scan_dedupe_and_keys_are_unique(fx, tmp_path):
    twin = tmp_path / "twin"
    twin.mkdir()
    (twin / "agentic-layer.json").write_text(json.dumps({"name": "demo-shop", "adws": []}))
    model = d.Model([fx["layer"], fx["layer"]], [fx["root"], tmp_path])
    model.discover()
    keys = sorted(model.layers)
    assert keys == ["demo-shop", "demo-shop-2", "other-layer"]


def test_layers_appearing_later_are_picked_up(tmp_path):
    model = d.Model([], [tmp_path])
    model.discover()
    assert model.layers == {}
    late = tmp_path / "late.al"
    late.mkdir()
    (late / "agentic-layer.json").write_text(json.dumps({"name": "late", "adws": []}))
    assert model.discover() is True
    assert list(model.layers) == ["late"]


def test_non_run_directories_are_ignored(fx):
    model = model_for(fx)
    ids = {s["id"] for s in model.summaries.values()}
    assert "trigger-logs" not in ids and ".slots" not in ids
    assert len(ids) == 14


# ------------------------------------------------------------------------------ aggregation

def test_passed_run_summary_costs_tokens_turns_duration(fx):
    s = summary(model_for(fx), "a1000001")
    assert s["status"] == "passed" and s["adw"] == "zte" and s["klass"] == "chore"
    assert s["cost"] == pytest.approx(0.0412 + 0.15 + 0.1834 + 0.03 + 0.2 + 0.18)
    assert s["duration"] == pytest.approx(160)
    assert s["tokens"]["out"] == 6 * 900 and s["tokens"]["cache_read"] == 6 * 60000
    assert s["turns"] == 3 + 6 + 14 + 3 + 3 + 3
    assert s["outcome"]["ship"] == "merge" and s["outcome"]["shipped"] is True
    assert s["outcome"]["review"] is True and s["outcome"]["rounds"] == 1
    assert s["artefacts"] == 3 and s["ports"] == [9100, 9101]
    assert s["container"]["name"] == "al-demo-shop-a1000001" and s["container"]["state"] == "removed"


def test_failed_run_with_handoff(fx):
    model = model_for(fx)
    s = summary(model, "a1000002")
    assert s["status"] == "failed" and s["failed_step"] == "test"
    assert s["outcome"]["ship"] == "handoff" and s["outcome"]["stopped_at"] == "test"
    detail = model.run_detail("demo-shop", "a1000002")
    assert "failed after 3 attempts" in detail["handoff"]["why"]
    assert "0007_refunds.py" in detail["handoff"]["asks"]
    attempts = detail["checks"]["test"]
    assert [a["exit"] for a in attempts] == ["fail", "fail", 1]


def test_running_run_is_live(fx):
    s = summary(model_for(fx), "a1000006")
    assert s["status"] == "running" and s["duration"] is None and s["ended"] is None
    assert s["current"]["step"] == "build" and s["current"]["kind"] == "run"
    assert s["current"]["action"] == "thinking"   # last complete event was a tool result
    assert s["cost"] == pytest.approx(0.04 + 0.12) and s["cost_partial"] is True
    assert s["container"] == {"name": "al-demo-shop-a1000006", "state": "running", "source": "run.log"}


def test_missing_state_file_is_an_unknown_run_not_a_crash(fx):
    s = summary(model_for(fx), "a1000009")
    assert s["status"] == "unknown" and s["problem"] == "no state.json"
    assert s["adw"] == "zte" and s["klass"] == "bug" and s["dry_run"] is True


def test_aggregate_pass_rate_median_totals():
    runs = [{"status": "passed", "cost": 1.0, "duration": 10}, {"status": "failed", "cost": 3.0, "duration": 30},
            {"status": "passed", "cost": None, "duration": 20}, {"status": "running", "cost": 0.5, "duration": None}]
    agg = d.aggregate(runs)
    assert agg["count"] == 4 and agg["finished"] == 3 and agg["passed"] == 2 and agg["running"] == 1
    assert agg["pass_rate"] == pytest.approx(2 / 3)
    assert agg["cost_total"] == pytest.approx(4.5) and agg["cost_median"] == pytest.approx(1.0)
    assert agg["duration_median"] == 20 and agg["duration_total"] == 60
    assert d.aggregate([])["pass_rate"] is None


def test_layer_detail_stats_per_workflow(fx):
    model = model_for(fx)
    detail = model.layer_detail("demo-shop")
    zte = next(a for a in detail["adws"] if a["name"] == "zte")
    assert zte["stats"]["count"] == 9 and zte["stats"]["passed"] == 3 and zte["stats"]["failed"] == 4
    assert detail["explain"] == "EXPLAIN.md" and "plain words" in detail["explain_text"]


# ------------------------------------------------------------------------------ timeline

def test_resolve_loop_timeline_reconstructs_attempt_windows(fx):
    detail = model_for(fx).run_detail("demo-shop", "a1000003")
    bars = {b["label"]: b for b in detail["timeline"]["bars"] if b["level"] == 1}
    t0 = fixtures.at(2000).timestamp()
    lint1, resolve, lint2 = bars["lint #1"], bars["lint-resolve-1"], bars["lint #2"]
    assert lint1["status"] == "failed" and lint2["status"] == "passed"
    assert (lint1["start"] - t0, lint1["end"] - t0) == pytest.approx((90, 94))
    assert (resolve["start"] - t0, resolve["end"] - t0) == pytest.approx((94, 110))
    assert (lint2["start"] - t0, lint2["end"] - t0) == pytest.approx((110, 113))
    totals = detail["timeline"]["totals"]
    assert totals["check"] == pytest.approx(4 + 3 + 7)
    assert totals["agent"] == pytest.approx(10 + 30 + 50 + 16 + 8 + 12 + 12)
    assert totals["run"] == pytest.approx(158)


def test_two_round_review(fx):
    detail = model_for(fx).run_detail("demo-shop", "a1000004")
    rounds = detail["review"]["rounds"]
    assert [r["round"] for r in rounds] == [1, 2] and rounds[0]["success"] is False and rounds[1]["success"] is True
    assert rounds[0]["reviewers"][0]["issues"][0]["severity"] == "blocker"
    assert rounds[0]["patch"]["rerun"] == ["lint", "test"]
    assert [r["dir"] for r in rounds[1]["reviewers"]] == ["review-2-1", "review-2-2"]
    rechecks = [b for b in detail["timeline"]["bars"] if b.get("role") == "recheck"]
    assert sorted(b["label"] for b in rechecks) == ["re-check lint #2", "re-check test #2"]
    assert all(b["step"] == "review" for b in rechecks)
    review_agents = sorted(a["dir"] for a in detail["agents"] if a["step"] == "review")
    assert review_agents == ["implement-1", "patch-1", "review-1", "review-2", "review-2-1", "review-2-2"]
    node = next(n for n in detail["graph"]["nodes"] if n["id"] == "review")
    assert node["cost"] == pytest.approx(0.21 + 0.19 + 0.12 + 0.15 + 0.2 + 0.2)
    assert detail["summary"]["outcome"]["url"] == "http://forge.invalid/demo/shop/pulls/7"


def test_fan_out_detail_links_candidates(fx):
    model = model_for(fx)
    detail = model.run_detail("demo-shop", "a1000005")
    lanes = [n for n in detail["graph"]["nodes"] if n["type"] == "candidate"]
    assert [(n["run"], n["status"]) for n in lanes] == [("c1000001", "passed"), ("c1000002", "failed"), ("c1000003", "passed")]
    assert lanes[0].get("picked") is True and lanes[1]["detail"] == "step test failed"
    assert summary(model, "a1000005")["fanout_children"] == ["c1000001", "c1000002", "c1000003"]


# ------------------------------------------------------------------------------ graphs from the IR

def zte() -> dict:
    return next(a for a in fixtures.IR["adws"] if a["name"] == "zte")


def test_graph_when_branches_share_a_column():
    graph = d.build_graph(zte(), fixtures.IR)
    nodes = {n["id"]: n for n in graph["nodes"]}
    branch = [nodes[n] for n in ("plan-chore", "plan-bug", "plan-feature")]
    assert len({n["col"] for n in branch}) == 1 and sorted(n["row"] for n in branch) == [0, 1, 2]
    assert nodes["classify"]["col"] + 1 == branch[0]["col"] == nodes["build"]["col"] - 1
    edges = {(e["from"], e["to"]): e for e in graph["edges"]}
    assert edges[("classify", "plan-bug")]["kind"] == "branch" and edges[("classify", "plan-bug")]["label"] == "bug"
    assert edges[("plan-feature", "build")]["kind"] == "join"


def test_graph_node_types_and_loops():
    graph = d.build_graph(zte(), fixtures.IR)
    types = {n["id"]: n["type"] for n in graph["nodes"]}
    assert types == {"classify": "agent", "plan-chore": "agent", "plan-bug": "agent", "plan-feature": "agent",
                     "build": "agent", "lint": "check", "test": "check", "commit": "agent", "review": "review", "ship": "ship"}
    loops = [(e["from"], e["to"], e["kind"]) for e in graph["edges"] if e["kind"] in {"resolve", "patch"}]
    assert ("lint", "lint", "resolve") in loops and ("test", "test", "resolve") in loops
    assert ("review", "lint", "patch") in loops      # review → patch → re-check from the first check
    patch = next(e for e in graph["edges"] if e["kind"] == "patch")
    assert "plan-patch" in patch["label"] and "≤1" in patch["label"]
    resolve = next(e for e in graph["edges"] if e["kind"] == "resolve" and e["from"] == "test")
    assert resolve["label"] == "resolve ×2 · resolve-test"
    assert graph["review"]["reviewers"] == 2


def test_graph_fan_out_lanes_then_pick():
    race = next(a for a in fixtures.IR["adws"] if a["name"] == "race")
    graph = d.build_graph(race, fixtures.IR)
    lanes = [n for n in graph["nodes"] if n["type"] == "candidate"]
    assert len(lanes) == 3 and len({n["col"] for n in lanes}) == 1 and lanes[0]["sub"] == "candidate"
    pick = next(n for n in graph["nodes"] if n["id"] == "race")
    assert pick["type"] == "fan-out" and pick["col"] == lanes[0]["col"] + 1 and pick["sub"] == "pick best"
    assert {(e["from"], e["kind"]) for e in graph["edges"] if e["to"] == "race"} == {(n["id"], "fan") for n in lanes}
    assert not any(e["kind"] == "patch" for e in graph["edges"])   # review without patch/implement: no loop


def test_graph_check_without_resolve_has_no_loop():
    adw = {"name": "x", "steps": [{"kind": "check", "name": "t", "cmd": "t", "resolve": "", "retries": 0}]}
    assert [e for e in d.build_graph(adw)["edges"] if e["kind"] == "resolve"] == []


def test_graph_of_every_real_layer_shape_is_well_formed():
    for adw in fixtures.IR["adws"]:
        graph = d.build_graph(adw, fixtures.IR)
        ids = {n["id"] for n in graph["nodes"]}
        assert all(e["from"] in ids and e["to"] in ids for e in graph["edges"])
        assert all(0 <= n["x"] <= graph["width"] and 0 <= n["y"] <= graph["height"] for n in graph["nodes"])


# ------------------------------------------------------------------------------ transcripts

def rows_of(path: Path) -> tuple[list[dict], d.TranscriptAgg]:
    short = d.Shortener([("/work/demo-shop.al/trees/a1000001", ""), ("/work/demo-shop.al", "layer:")])
    return d.transcript_rows(path, short)


def test_transcript_normalises_every_claude_event_type(fx):
    rows, agg = rows_of(fx["agents"] / "a1000001" / "build" / "raw.jsonl")
    kinds = [r["k"] for r in rows]
    for kind in ("init", "meta", "thinking", "text", "tool", "result", "task", "user", "final"):
        assert kind in kinds, kind
    tools = {r["tool"]: r for r in rows if r["k"] == "tool"}
    assert set(tools) == {"Read", "Grep", "Glob", "Edit", "Write", "Bash", "TodoWrite"}
    assert tools["Read"]["title"] == "read layer:/specs/chore-a1000001.md"
    assert tools["Grep"]["title"] == "search /def total/ in shop"
    assert tools["Glob"]["title"] == "find files tests/test_*.py"
    edit = next(r for r in rows if r["k"] == "tool" and r["tool"] == "Edit")
    assert edit["title"] == "edit tests/test_cart.py" and edit["action"] == "editing tests/test_cart.py"
    assert "+def test_two_items():" in edit["diff"]
    assert tools["Write"]["title"] == "write tests/conftest.py (6 lines)"
    bash = next(r for r in rows if r["k"] == "tool" and r["tool"] == "Bash")
    assert bash["title"].startswith("$ uv run --locked pytest") and bash["action"] == "running pytest"
    results = [r for r in rows if r["k"] == "result"]
    assert any(r["err"] for r in results)                                  # the failing pytest run
    assert any(r.get("diff") for r in results)                             # structuredPatch from tool_use_result
    assert any(r.get("stderr", "").startswith("E   NameError") for r in results)
    glob_result = next(r for r in results if r["tool"] == "Glob")
    assert glob_result["text"] == "tests/test_cart.py\ntests/test_api.py"  # list content flattened
    tasks = [r["title"] for r in rows if r["k"] == "task"]
    assert any(t.startswith("task started: uv run") for t in tasks) and any(t.startswith("task completed") for t in tasks)
    rate = next(r for r in rows if r["k"] == "meta")
    assert rate["title"] == "rate limit: allowed (seven_day 12%)"
    final = rows[-1]
    assert final["k"] == "final" and final["cost"] == pytest.approx(0.1834) and final["turns"] == 14
    assert agg.kind == "claude" and agg.model == "claude-opus-5-5" and agg.final and not agg.is_error
    assert agg.tokens == {"in": 40, "out": 900, "cache_read": 60000, "cache_write": 5000}
    assert agg.tools == 9 and agg.thinking_tokens == 50


def test_transcript_drops_noise_events(fx):
    rows, _agg = rows_of(fx["agents"] / "a1000001" / "build" / "raw.jsonl")
    raw = (fx["agents"] / "a1000001" / "build" / "raw.jsonl").read_text()
    assert '"thinking_tokens"' in raw and '"ui_invalidate"' in raw
    assert not any("thinking_tokens" in json.dumps(r) or "ui_invalidate" in json.dumps(r) for r in rows)


def test_transcript_paths_are_shortened(fx):
    rows, _agg = rows_of(fx["agents"] / "a1000001" / "build" / "raw.jsonl")
    assert "/work/demo-shop.al" not in json.dumps(rows)


def test_transcript_corrupt_and_partial_lines(fx):
    rows, agg = d.transcript_rows(fx["agents"] / "a1000008" / "classify" / "raw.jsonl")
    corrupt = [r for r in rows if r["k"] == "corrupt"]
    assert len(corrupt) == 2   # the truncated object mid-file and the unterminated last line
    assert all(r["err"] for r in corrupt)
    plain = [r for r in rows if r["k"] == "plain"]
    assert plain and plain[0]["text"] == "not json at all, a stray diagnostic line"
    final = rows[-2] if rows[-1]["k"] == "corrupt" else rows[-1]
    assert final["k"] == "final" and final["err"] is True and final["title"] == "result: error_max_turns"
    assert agg.is_error and agg.kind == "claude"


def test_partial_last_line_is_held_back_while_the_file_is_fresh(fx):
    path = fx["agents"] / "a1000006" / "build" / "raw.jsonl"
    os.utime(path)   # "just written"
    tail = d.Tail(path, settle=60)
    _reset, lines = tail.read()
    transcript = d.Transcript()
    rows = transcript.feed(lines)
    assert not any(r["k"] == "corrupt" for r in rows)
    assert tail.pending   # half an Edit event waits for its newline
    with path.open("a") as handle:
        handle.write(fixtures.PARTIAL_EDIT[fixtures.PARTIAL_SPLIT:] + "\n")
    _reset, more = tail.read()
    assert len(more) == 1 and not tail.pending


def test_plain_text_agent(fx):
    rows, agg = d.transcript_rows(fx["agents"] / "a100000a" / "answer" / "raw.jsonl")
    assert agg.kind == "plain" and [r["k"] for r in rows] == ["plain"]
    assert "Cart.total()" in rows[0]["text"]


def test_mock_agent(fx):
    rows, agg = d.transcript_rows(fx["agents"] / "a100000b" / "classify" / "raw.jsonl")
    assert agg.kind == "mock" and rows[0]["k"] == "mock" and "mock-task" in rows[0]["text"]


def test_real_claude_transcripts_if_present():
    """Every real raw.jsonl on this machine normalises without an error row (skipped elsewhere)."""
    roots = [Path(p) for p in os.environ.get("AL_DASHBOARD_REAL_LAYERS", "").split(os.pathsep) if p]
    files = [f for root in roots for f in root.glob("*/agents/*/*/raw.jsonl")]
    if not files:
        pytest.skip("set AL_DASHBOARD_REAL_LAYERS to directories of real layers to run this")
    for path in files:
        rows, agg = d.transcript_rows(path)
        assert not [r for r in rows if r["k"] in {"corrupt"}], path
        assert agg.kind in {"claude", "mock", "plain", "empty"}


@pytest.mark.parametrize("command,verb", [
    ("uv run --locked --no-default-groups --group pre-commit pre-commit run --all-files", "pre-commit run"),
    ("uv run --locked pytest -q tests", "pytest"),
    ("python -m pytest -q", "pytest"),
    ("cd /tmp && npm test", "npm test"),
    ("FOO=1 BAR=2 cargo test --all", "cargo test"),
    ("git status --short", "git status"),
    ("timeout 60 make check", "make check"),
    ("python e2e/capture.py", "capture.py"),
    ("ls -la | head", "ls"),
    ("", "a command"),
])
def test_command_verb(command, verb):
    assert d.command_verb(command) == verb


# ------------------------------------------------------------------------------ ANSI

def test_ansi_colours_bold_and_reset():
    segments, _ = d.ansi_segments("plain \x1b[1;31mred bold\x1b[0m back")
    assert segments == [["plain ", "", "", ""], ["red bold", "#f87171", "", "b"], [" back", "", "", ""]]


def test_ansi_256_and_truecolour_and_background():
    segments, _ = d.ansi_segments("\x1b[38;5;196mA\x1b[38;2;1;2;3;48;5;21mB\x1b[39;49mC")
    assert segments[0] == ["A", "#ff0000", "", ""]
    assert segments[1] == ["B", "#010203", "#0000ff", ""]
    assert segments[2] == ["C", "", "", ""]


def test_ansi_non_colour_escapes_and_controls_are_stripped():
    text = "\x1b[2K\x1b[1Gline\x1b]0;title\x07 ok\x07\x08\n"
    assert d.strip_ansi(text) == "line ok\n"


def test_ansi_carriage_return_keeps_the_last_frame():
    assert d.strip_ansi("10%\r50%\r100%\ndone") == "100%\ndone"


def test_ansi_state_carries_over_between_chunks():
    first, state = d.ansi_segments("\x1b[32mgreen starts")
    second, _ = d.ansi_segments(" and continues\x1b[0m", state)
    assert second[0] == [" and continues", "#4ade80", "", ""]


# ------------------------------------------------------------------------------ tail

def test_tail_emits_only_the_delta(tmp_path):
    path = tmp_path / "x.log"
    path.write_text("one\ntwo\n")
    tail = d.Tail(path, settle=60)
    assert [l[2] for l in tail.read()[1]] == ["one", "two"]
    assert tail.read()[1] == []
    with path.open("a") as handle:
        handle.write("three\nfou")
    reset, lines = tail.read()
    assert not reset and [(n, text) for n, _o, text in lines] == [(3, "three")]
    with path.open("a") as handle:
        handle.write("r\n")
    assert [(n, text) for n, _o, text in tail.read()[1]] == [(4, "four")]


def test_tail_truncation_and_replacement_reset(tmp_path):
    path = tmp_path / "x.log"
    path.write_text("a\nb\nc\n")
    tail = d.Tail(path)
    tail.read()
    path.write_text("new\n")   # truncated and rewritten (same inode)
    reset, lines = tail.read()
    assert reset and [l[2] for l in lines] == ["new"]
    replacement = tmp_path / "y.log"
    replacement.write_text("other\n")
    os.replace(replacement, path)   # rotated: a new inode
    reset, lines = tail.read()
    assert reset and [l[2] for l in lines] == ["other"]


def test_tail_resumes_from_an_offset(tmp_path):
    path = tmp_path / "x.log"
    path.write_text("one\ntwo\nthree\n")
    tail = d.Tail(path, offset=len("one\n"), line_no=1)
    assert [(n, text) for n, _o, text in tail.read()[1]] == [(2, "two"), (3, "three")]


def test_tail_holds_no_file_handle(tmp_path):
    path = tmp_path / "x.log"
    path.write_text("one\n")
    before = len(os.listdir("/proc/self/fd"))
    tails = [d.Tail(path) for _ in range(50)]
    for tail in tails:
        tail.read()
    assert len(os.listdir("/proc/self/fd")) == before


# ------------------------------------------------------------------------------ slots and ports

def test_slots_inferred_without_locks(fx):
    model = model_for(fx, in_container=True)
    slots = model.slots["demo-shop"]
    assert slots["source"] == "inferred"
    assert slots["pools"]["runs"] == {"used": 1, "total": 2, "holders": [{"slot": 0, "who": "adw a1000006", "run": "a1000006"}]}
    assert slots["pools"]["checks"]["used"] == 0 and slots["pools"]["checks"]["total"] == 1
    assert slots["ports"] == [{"run": "a1000006", "ports": [9110, 9111], "alive": None}]


def test_slots_from_real_flocks(fx):
    import fcntl
    path = fx["agents"] / ".slots" / "checks.0.lock"
    path.write_text("4242 adw a1000006 check lint\n")
    handle = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        layer = d.load_layer(fx["layer"].resolve(), set())
        slots = d.layer_slots(layer, [], d.proc_locks(), in_container=False)
        assert slots["source"] == "locks"
        assert slots["pools"]["checks"]["holders"] == [{"slot": 0, "who": "adw a1000006 check lint", "run": "a1000006"}]
        assert slots["pools"]["runs"]["used"] == 0    # its lock file exists but nobody holds it
        assert slots["ports"][0]["alive"] is False    # pid 0: the lease is stale
    finally:
        os.close(handle)


def test_container_probe_degrades_without_podman():
    probe = d.ContainerProbe(None, enabled=False)
    assert probe.poll(True) is False and probe.info()["available"] is False
    assert "run.log" in probe.info()["note"]
