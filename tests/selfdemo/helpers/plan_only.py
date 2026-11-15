"""Run ONE agent step of a layer for real: the planning step of a workflow, nothing after it.

    python plan_only.py LAYER_DIR ADW STEP PROMPT OUT.md [--dry-run]

Uses the layer's own runtime (LAYER_DIR/adws/adw_modules): the ADW's environment stack (here a
worktree and the podman container with the agent inside), the ADW's agent and the generated
prompt of STEP, exactly as a full run would. Writes the plan the agent produced to OUT.md and
prints the run id. The worktree and container are removed afterwards; nothing is shipped.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path


def main() -> int:
    layer_dir, adw_name, step_name, prompt, out = Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3], sys.argv[4], Path(sys.argv[5])
    dry_run = "--dry-run" in sys.argv[6:]
    sys.path.insert(0, str(layer_dir / "adws"))
    from adw_modules import agents, envs, ir, state as state_module

    layer = ir.load(layer_dir)
    adw = layer.validate_adw(adw_name)
    step = next(s for s in adw["steps"] if s.get("name") == step_name)
    state = state_module.ADWState(adw_id=state_module.new_adw_id(), adw=adw_name, prompt=prompt, source="cli",
                                  klass="chore", class_fixed=True, dry_run=dry_run)
    state.save(layer.layer_dir)
    print(f"plan_only: run {state.adw_id}, step {step_name} of {adw_name}", flush=True)
    stack = envs.build(layer, state, list(adw["environment"]))
    ok = False
    try:
        stack.setup()
        command = layer.command(str(step["command"]))
        plan = agents.run_agent(layer, state, step_name, str(step["command"]), [prompt],
                                str(command.get("modelSet") or "heavy"), stack)
        shutil.copyfile(plan, out)
        state.plan, state.status, ok = plan, "passed", True
    finally:
        stack.teardown(ok)
        if not ok:
            state.status = "failed"
        state.save(layer.layer_dir)
    print(f"plan_only: plan {state.plan} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
