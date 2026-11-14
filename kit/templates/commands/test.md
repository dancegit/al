---
description: {{description}}
argument-hint: (none)
---

# Test

`{{command_name}}` in layer `{{name}}`: run the codebase's validation commands in order and return a machine-readable result. Observe and report; fix nothing.

## Variables

WORKTREE: ${worktree}
ADW_ID: ${adw_id}

## Instructions

- Run every command from the root of WORKTREE, exactly as written. Do not add flags, change directories, or substitute an equivalent command.
- Run them top to bottom. Stop at the first command that exits non-zero: do not run the commands after it.
- You are a reporter. Do not edit any file, do not retry a failing command with changes, do not install anything that a listed command does not install itself.
- Never switch branches, push, or touch the main checkout.
- `name` is the command's name as listed (the word before the shell command); `command` is the exact shell string you ran.
- `passed` is true only when the exit code is 0. Warnings on a zero exit are still a pass.
- `error` is the empty string on a pass. On a failure it is the part of the output that explains the failure: the failing test names, the assertion or compiler message, and the file and line, at most 60 lines. Cut noise, never paraphrase.
- A command that times out or cannot start has failed; say which in `error`.
{{focus}}

{{fabric}}

## Workflow

1. Run the first command and capture stdout, stderr and the exit code.
2. Record its result object.
3. If it passed and commands remain, continue with the next one; otherwise stop.
4. Emit the results.

## Validation Commands

{{validation_commands}}

## Report

Return ONLY a JSON array, no prose and no code fence, one object per command that was run, in run order:

```json
[
  {"name": "<command name>", "passed": true, "command": "<shell command>", "error": ""},
  {"name": "<command name>", "passed": false, "command": "<shell command>", "error": "<relevant failure output>"}
]
```

Commands after the first failure are not run and do not appear in the array.

{{output_contract}}
