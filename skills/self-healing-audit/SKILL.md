---
name: self-healing-audit
description: Step 1 of the four-step healwright methodology. Inventory the jobs in this repo, classify each one's idempotency and side effects, recommend native retry tiers, and pick the candidates for trigger-based self-healing, with the human confirming every judgement call before JOB_AUDIT.md is written. Use when starting self-healing on a jobs repo, before extending it to more jobs, or to ask "where am I with self-healing?".
argument-hint: "[job-definitions-folder] [source-folder]"
allowed-tools: [Bash, Read, Grep, Glob, Write]
---

# self-healing-audit

Self-healing that retries a non-idempotent job is worse than no self-healing. This audit is the
step that earns the right to automate anything. Output is a plain file, `JOB_AUDIT.md`, that the
team keeps even if they uninstall the tool. Paths like `docs/glossary.md` below are in the
healwright repo (github.com/kyle-chalmers/healwright), not this one: a synthetic example of the
finished file is `examples/JOB_AUDIT.example.md`, and terms are defined in `docs/glossary.md`.

## Working with the user

- The person running this may never have seen healwright. Open with one line on what this skill
  produces and that it changes nothing in the workspace.
- Before each step, one line of what you are about to do and why; after it, one line with what
  you found. Plain words, no walls of text.
- Every question says what it decides and what each answer changes. What you detected is the
  recommended answer, never the confirmed one.
- Say what you read versus what you inferred. A guess about a side effect is a REVIEW, not a tier.
- Close with what got done and the one thing to do next.

## Steps

0. **Find where you are.** Look for `JOB_AUDIT.md`, `jobs/self_healing/config.yaml`, and a
   `healer_job_id` in it. Report the state in one line, for example "No audit yet; starting
   fresh" or "Audit from <date> exists and the healer is built; I'll audit only jobs added since".
   If the healer is already installed and the user only wanted to know where they are, say which
   skill comes next (see `## Next` in each skill) and stop.
1. **Find the job definitions and the source.** Default folders: `databricks/job_definitions/`
   (or wherever `*.json` job specs live) and `jobs/` for notebooks. Confirm with the user if the
   repo uses different names. If definitions are not in the repo, list live jobs with the platform
   CLI (for Databricks: `databricks jobs list --output json`) and pull each with `jobs get`.
   Listing live jobs is read-only; say so before you run it.
2. **For every job, record** in a table: name, job id, run-as identity (or "creator" when the
   job has no `run_as`), schedule, number of tasks, whether it is PAUSED, current `max_retries` / `retry_on_timeout`, whether `on_failure` notifications exist,
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
6. **Checkpoint: the human confirms the judgement calls.** Show the tier table and the candidate
   list, then ask about each REVIEW job and each Side-effecting job, one at a time: what you saw
   in the code, the tier you recommend, and what a wrong answer costs (a retry that re-sends a
   delivery, or an alert for a failure the platform would have absorbed). Then confirm the
   candidate list as a whole. Do not write the file until this is done; a REVIEW the user cannot
   answer stays REVIEW, with `max_retries: 0` until someone can.
7. **Write `JOB_AUDIT.md`** at the repo root with the table, the tier legend, the candidate list,
   the date, and who confirmed the judgement calls. Say what was inferred versus read. Do not put
   credentials, hostnames, or customer data in it.

## Done when

`JOB_AUDIT.md` exists, every unpaused job has a tier or a REVIEW mark, the user has confirmed
every REVIEW and Side-effecting row and the candidate list, and the candidate list is explicit.
Nothing has been deployed.

## Next

- Step 2 of the methodology has no skill: apply the retry tiers with your normal deploy path
  (live-pull, patch, diff, deploy; never `jobs reset` from a stale repo JSON).
- Then `/self-healing-build` (step 3a) to copy the healer into this repo and configure it.
