---
name: self-healing-audit
description: Step 1 of the healwright methodology. Inventory the jobs in this repo, classify each one's idempotency and side effects, recommend native retry tiers, and pick the candidates for trigger-based self-healing. Use when starting self-healing on a jobs repo, or before extending it to more jobs.
argument-hint: "[job-definitions-folder] [source-folder]"
allowed-tools: [Bash, Read, Grep, Glob, Write]
---

# self-healing-audit

Self-healing that retries a non-idempotent job is worse than no self-healing. This audit is the
step that earns the right to automate anything. Output is a plain file, `JOB_AUDIT.md`, that the
team keeps even if they uninstall the tool.

## Steps

1. **Find the job definitions and the source.** Default folders: `databricks/job_definitions/`
   (or wherever `*.json` job specs live) and `jobs/` for notebooks. Confirm with the user if the
   repo uses different names. If definitions are not in the repo, list live jobs with the platform
   CLI (for Databricks: `databricks jobs list --output json`) and pull each with `jobs get`.
2. **For every job, record** in a table: name, schedule, number of tasks, whether it is
   PAUSED, current `max_retries` / `retry_on_timeout`, whether `on_failure` notifications exist,
   and its **side effects**. Grep the job's source for the side-effect shapes: outbound file
   delivery (SFTP/FTP/S3 put), email or messaging sends, writes to external APIs, appends to
   tables without a key, non-idempotent DDL. Read the code around each hit, do not trust the grep
   alone.
3. **Classify each job**:
   - **Idempotent**: re-running produces the same result. Safe for `max_retries: 2`.
   - **Idempotent with guard**: re-run is safe because the code checks before it acts. `max_retries: 1`.
   - **Side-effecting**: re-run duplicates a delivery or a send. `max_retries: 0`; the healer alerts,
     humans rerun deliberately.
   Mark anything you could not determine as **REVIEW** rather than guessing.
4. **Recommend the retry tier per job** and note which jobs are already compliant. Retries are
   the platform's job (native `retry_policy`); the healer never retries anything.
5. **Pick the trigger candidates**: unpaused, multi-task or convertible, with source under the
   source folder (so the fix pipeline can scope edits to one job folder). Jobs that call other jobs
   with Run Job tasks get a note about the nesting limit.
6. **Write `JOB_AUDIT.md`** at the repo root with the table, the tier legend, the candidate list,
   and the date. Say what was inferred versus read. Do not put credentials, hostnames, or customer
   data in it.

## Done when

`JOB_AUDIT.md` exists, every unpaused job has a tier or a REVIEW mark, and the candidate list is
explicit. Nothing has been deployed.

## Next

- Apply the retry tiers with your normal deploy path (live-pull, patch, diff, deploy; never
  `jobs reset` from a stale repo JSON).
- Then `/self-healing-build` to copy the healer into this repo and configure it.
