---
description: {{description}}
argument-hint: (none)
---

# Start

`{{command_name}}` in layer `{{name}}`: start the application from the worktree in the background, confirm it is serving, and report how to reach and stop it.

## Variables

WORKTREE: ${worktree}
ADW_ID: ${adw_id}
LOG_FILE: ${artefacts_dir}/start.log

## Instructions

- Work only inside WORKTREE (your current directory). Never switch branches, push, or touch the main checkout.
- Use the codebase's `start` command from the list below, from the worktree root. If no command named `start` is listed, this codebase has nothing to start: report that and stop.
- This run owns the ports in the environment variables `ADW_PORT_0`, `ADW_PORT_1`, and so on. Bind only to those, using whatever port option the start command or the codebase's configuration supports. Never use the codebase's default port: another run may hold it.
- Check each port first. If something is already listening, find out whether it is this worktree's own earlier process; stop it only if it is. Never stop a process that belongs to another worktree or to the main checkout.
- Run the command in the background with stdout and stderr redirected to LOG_FILE, and record the process id.
- Wait until the application answers (an HTTP response, an open port, or the ready line in the log), at most 60 seconds. A process that is running but not answering has not started.
- Do not edit application code or tracked configuration to make it start. Port and host go in through arguments or environment variables.
- If it fails to start, stop the process, read LOG_FILE, and report the cause. Do not attempt a fix.
{{focus}}

## Conditional Documentation

{{conditional_docs}}

{{fabric}}

## Workflow

1. Read the port variables from the environment.
2. Free a port only if this worktree's own stale process holds it.
3. Start the application in the background, logging to LOG_FILE.
4. Poll until it answers or 60 seconds pass.
5. Report.

## Validation Commands

Started means: the process is alive and the application answers on its port. Every command of the codebase:

{{all_commands}}

## Report

- Status: started, nothing to start, or failed.
- The exact command line you ran, the process id, and the address it answers on.
- The command that stops it.
- On failure: the last 30 lines of LOG_FILE.

{{output_contract}}
