---
description: {{description}}
argument-hint: (none)
---

# Track Agentic KPIs

`{{command_name}}` in layer `{{name}}`: recompute the layer's agentic KPIs from the recorded run states and update the KPI document.

## Variables

STATE_FILES: ${layer_dir}/agents/*/state.json
KPI_FILE: ${layer_dir}/docs/agentic_kpis.md
CURRENT_ADW_ID: ${adw_id}

## Instructions

- You write exactly one file, KPI_FILE, in the layer. Do not edit application code or any state file. Never switch branches, push, or touch the main checkout.
- Read every file matching STATE_FILES. Each is one run. Use the fields that are present; never estimate a missing value: write `n/a` for it and leave it out of averages.
- Do the arithmetic with a tool (a shell one-liner or a JSON processor), not in your head.
- Definitions, per run:
  - Size: lines added plus lines removed by the run's diff, as recorded in the state. Bigger shipped work per run is better.
  - Attempts: how many passes the run needed before it passed or stopped: 1 plus every resolve, retry and patch-and-re-review loop. Lower is better; 1 is a one-shot.
  - Streak: the count of consecutive successful runs with Attempts of 2 or fewer, ending at this run when ordered by start time. A failed run or one with more than 2 attempts resets it to 0.
  - Presence: how many times a human had to be present: 2 is prompt and review, 1 is prompt only, 0 is fully zero-touch. Count every additional human intervention recorded in the state. Lower is better.
- Totals: number of runs, successful runs, current streak, longest streak, median and largest Size, average Attempts, average Presence, and the share of runs with Presence of 1 or less.
- Rebuild the table from the state files every time, newest run first, so the document can never drift from the data. Preserve nothing from the previous version except the title.
- Dates are ISO 8601. Numbers are plain; averages have one decimal.
{{focus}}

{{fabric}}

## Document Format

```md
# Agentic KPIs

Updated: <timestamp>  Runs: <n>

## Totals

| Metric | Value |
| --- | --- |
| Runs (successful / total) | <a> / <b> |
| Current streak | <n> |
| Longest streak | <n> |
| Size (median / largest) | <n> / <n> |
| Attempts (average) | <n.n> |
| Presence (average) | <n.n> |
| Runs with presence 1 or less | <n>% |

## Runs

| Date | ADW ID | Workflow | Class | Result | Size | Attempts | Streak | Presence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| <date> | <id> | <adw name> | <class> | <passed or failed> | <n> | <n> | <n> | <n> |
```

## Workflow

1. List and read the state files.
2. Compute the four KPIs per run, then the totals.
3. Write KPI_FILE in the Document Format.
4. Re-read it and check the row count equals the number of state files.

## Validation Commands

None of the codebase's commands apply. The document is valid when its run rows equal the state file count and every total can be recomputed from the rows.

## Report

The path of KPI_FILE, the number of runs counted, and the current streak, average attempts and average presence. Name any state file that could not be read.

{{output_contract}}
