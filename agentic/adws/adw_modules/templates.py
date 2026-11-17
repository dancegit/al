from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

TEMPLATE_VARIABLES = (
    "adw_id", "layer_dir", "specs_dir", "worktree", "artefacts_dir",
    "branch", "default_branch", "class", "prompt", "plan", "workflow_steps", "scratch_dir",
)

# One pass over the text so substituted values are never substituted again.
_TOKEN = re.compile(r"\$ARGUMENTS|\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([1-9])(?![0-9])")
_STEP_ARG = re.compile(r"\$(out\.[A-Za-z0-9_.-]*[A-Za-z0-9_-]|prompt|plan|adw_id|class|branch)(?![A-Za-z0-9_])")


def render(text: str, variables: Mapping[str, object], args: Sequence[str]) -> str:
    """Substitute $ARGUMENTS, $1..$9 (missing -> "") and ${name}; unknown ${...} stay untouched."""
    rendered_args = [str(value) for value in args]

    def replace(match: re.Match[str]) -> str:
        name, position = match.group(1), match.group(2)
        if name is not None:
            if name in TEMPLATE_VARIABLES and name in variables:
                return str(variables[name])
            return match.group(0)
        if position is not None:
            index = int(position) - 1
            return rendered_args[index] if index < len(rendered_args) else ""
        return " ".join(rendered_args)

    return _TOKEN.sub(replace, text)


def render_step_arg(value: str, variables: Mapping[str, object], outputs: Mapping[str, str]) -> str:
    """Resolve IR step args: $prompt, $plan, $adw_id, $class, $branch, $out.<step>."""
    mapping = {key: str(item) for key, item in variables.items()}
    mapping.update({f"out.{key}": str(item) for key, item in outputs.items()})
    return _STEP_ARG.sub(lambda match: mapping.get(match.group(1), match.group(0)), value)
