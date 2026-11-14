#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""PITER · T — push-to-talk voice trigger (terminal loop).

Press Enter, speak; the transcript is routed by the voice prompt-input option:
task -> append to the tasklist (or run now), question -> the `ask`/`question`
ADW, summary -> spoken status of the latest runs, auto -> keyword rules.

    trigger_voice.py [--layer DIR] [--trigger ID] [--once] [--dry-run] [--text "…"]

--text skips recording (useful for testing the routing); --once handles one
utterance and exits with the run's status.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules import inputs, tasklist  # noqa: E402
from adw_modules.forge import as_layer  # noqa: E402

DEFAULT_LAYER = Path(__file__).resolve().parent.parent.parent
VOICE_DIR = Path(__file__).resolve().parent.parent / "voice"

SUMMARY_RX = re.compile(r"^(status|summary|summari[sz]e|what happened|what'?s (running|the status)|"
                        r"how did|report|progress)\b", re.I)
QUESTION_RX = re.compile(r"^(what|why|how|where|when|which|who|explain|describe|tell me|does|do|is|are|can)\b", re.I)
TASK_RX = re.compile(r"^(add|fix|implement|create|update|remove|delete|refactor|rename|write|make|change|"
                     r"move|build|document|improve|upgrade|bump|replace|support|migrate|test)\b", re.I)
FILLER_RX = re.compile(r"^(?:(?:ok(?:ay)?|so|please|hey|um+|uh+|well)[\s,]+)+", re.I)


def classify(text: str) -> str:
    """auto routing: 'task' | 'question' | 'summary' by the leading keyword."""
    t = FILLER_RX.sub("", text.strip())
    if SUMMARY_RX.match(t):
        return "summary"
    if TASK_RX.match(t):
        return "task"
    if QUESTION_RX.match(t) or t.endswith("?"):
        return "question"
    return "task"


def slug(text: str, words: int = 5) -> str:
    parts = re.findall(r"[a-z0-9]+", FILLER_RX.sub("", text.lower()))
    return "-".join(parts[:words]) or "voice"


def _source_for(adw: Mapping[str, Any], voice_id: str | None) -> str:
    return voice_id if voice_id and voice_id in (adw.get("input") or []) else "cli"


def plan_action(text: str, ir: Mapping[str, Any], trigger_id: str | None = None) -> dict:
    """Pure routing: transcript -> action dict (append | run | summary | none)."""
    voice = next((o for o in ir.get("promptInputs") or [] if o.get("kind") == "voice"), None)
    option = {**inputs.DEFAULTS["voice"], **(voice or {})}
    voice_id = voice.get("id") if voice else None
    route = option.get("route") or "auto"
    kind = classify(text) if route == "auto" else route
    adws = list(ir.get("adws") or [])

    if kind == "summary":
        return {"action": "summary", "route": kind}
    if kind == "question":
        ask = next((a for a in adws if a.get("name") in ("ask", "question")), None)
        if not ask:
            return {"action": "none", "route": kind,
                    "message": "No question workflow. Add (adw ask …) or (adw question …) running the "
                               "question template to the layer to answer codebase questions by voice."}
        return {"action": "run", "route": kind, "adw": ask["name"], "prompt": text,
                "source": _source_for(ask, voice_id)}
    # task
    tl_opt = next((o for o in ir.get("promptInputs") or [] if o.get("kind") == "tasklist"), None)
    if option.get("appendToTasklist", True) and tl_opt:
        return {"action": "append", "route": kind, "path": tl_opt.get("path", "tasks.md"),
                "section": slug(text), "text": text, "option": tl_opt}
    workers = [a for a in adws if a.get("name") not in ("ask", "question")]
    pool = ([a for a in workers if trigger_id in (a.get("trigger") or [])] if trigger_id else []) or workers
    adw = next((a for a in pool if voice_id and voice_id in (a.get("input") or [])), None)
    if not adw:
        return {"action": "none", "route": kind,
                "message": "No ADW accepts voice input; add the voice prompt-input to an ADW's #:input."}
    return {"action": "run", "route": kind, "adw": adw["name"], "prompt": text, "source": voice_id}


def summarize_states(states: Iterable[Mapping[str, Any]]) -> str:
    """Pure: recent state.json dicts (newest first) -> one short spoken paragraph."""
    lines = []
    for s in states:
        adw = s.get("adw") or s.get("workflow") or s.get("adw_name") or s.get("name") or "a workflow"
        sid = str(s.get("adw_id") or "")[:8]
        status = s.get("status") or "unknown"
        what = (s.get("prompt") or "").strip().splitlines()[0][:80] if s.get("prompt") else ""
        line = f"Run {sid} of {adw} {status}" if sid else f"{adw} {status}"
        if s.get("branch"):
            line += f" on branch {s['branch']}"
        if what:
            line += f": {what}"
        lines.append(line.rstrip(".") + ".")
    return " ".join(lines) if lines else "There are no runs yet."


def recent_states(layer_dir: Path, n: int = 3) -> list[dict]:
    files = sorted((Path(layer_dir) / "agents").glob("*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files[:n]:
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def speak(layer_dir: Path, text: str) -> None:
    print(f"  > {text}", flush=True)
    subprocess.run(["uv", "run", "--quiet", str(VOICE_DIR / "say.py"), "--layer", str(layer_dir), text],
                   check=False)


def listen(layer_dir: Path) -> str:
    proc = subprocess.run(["uv", "run", "--quiet", str(VOICE_DIR / "listen.py"), "--layer", str(layer_dir)],
                          stdout=subprocess.PIPE, text=True, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def execute(layer, action: dict, dry_run: bool) -> int:
    ld = Path(layer.layer_dir)
    kind = action["action"]
    if kind == "append":
        path = ld / action["path"]
        line = tasklist.append(path, action["section"], action["text"], option=action["option"])
        speak(ld, f"Added to section {action['section'].replace('-', ' ')}.")
        print(f"appended {path}:{line}", flush=True)
        return 0
    if kind == "summary":
        speak(ld, summarize_states(recent_states(ld)))
        return 0
    if kind == "none":
        speak(ld, action["message"])
        return 1
    argv = inputs.adw_argv(ld, action["adw"], action["source"], prompt=action["prompt"], dry_run=dry_run)
    print(f"start {action['adw']} <- {action['source']}", flush=True)
    rc = subprocess.run(argv, check=False).returncode
    speak(ld, f"{action['adw']} {'finished' if rc == 0 else 'failed'}.")
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Push-to-talk voice trigger")
    ap.add_argument("--layer", default=str(DEFAULT_LAYER))
    ap.add_argument("--trigger", help="voice trigger id (default: the first voice trigger)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--text", help="use this text instead of recording")
    args = ap.parse_args(argv)

    layer = as_layer(args.layer)
    trig = next((t for t in layer.ir.get("triggers") or []
                 if (t.get("id") == args.trigger if args.trigger else t.get("kind") == "voice")), {"id": None})
    voice = next((o for o in layer.ir.get("promptInputs") or [] if o.get("kind") == "voice"), {})
    confirm = voice.get("confirm", True)
    print(f"voice {trig.get('id')}: route {voice.get('route', 'auto')}, confirm {confirm}. "
          "Press Enter to talk, Ctrl-C to quit.", flush=True)
    rc = 0
    try:
        while True:
            if args.text:
                text = args.text
            else:
                input("[Enter] to record… ")
                text = listen(layer.layer_dir)
            if not text:
                print("(nothing recognised)", flush=True)
            else:
                print(f"heard: {text}", flush=True)
                action = plan_action(text, layer.ir, trig.get("id"))
                go = True
                if confirm and not args.text:
                    speak(layer.layer_dir, f"I heard: {text}. Press Enter to {action['action']}, or n to discard.")
                    go = input("[Enter]=go, n=discard: ").strip().lower() not in ("n", "no")
                if go:
                    rc = execute(layer, action, args.dry_run)
                else:
                    print("discarded", flush=True)
            if args.once or args.text:
                return rc
    except (KeyboardInterrupt, EOFError):
        print("\nbye", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
