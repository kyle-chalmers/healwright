---
name: self-healing-install-trigger
description: Step 3b of the four-step healwright methodology. Add the trigger-based leaf task to monitored jobs so a failure calls the shared healer immediately, with a dry run and diff per job, permission preflight, confirmation, and a shadow-mode validation before alerts go live. Detects which jobs already have it. Use after /self-healing-build.
argument-hint: "<job-id ...> --healer-job-id <id>"
allowed-tools: [Bash, Read, Grep, Glob, Write, Edit]
---

# self-healing-install-trigger

The installer edits live job definitions. It is dry-run by default, fetches the live definition
(repo JSONs are not authoritative), sends only the task list, and refuses when the definition
changed between read and write. You still review every diff. Paths like `docs/glossary.md` are
in the healwright repo (cloned to `/tmp/healwright` by `/self-healing-build`): terms are defined
in `docs/glossary.md`, and installer messages and their fixes are in `docs/troubleshooting.md`.

## Working with the user

- Open with one line: dry runs first and change nothing; each `--apply` is confirmed per job or
  per reviewed batch; shadow mode stays on until the user decides otherwise.
- Before each command, one line of what and why; after it, one line with the result.
- Read every diff and every WARN line to the user in plain words: which task was added, what it
  depends on, and what the warning means for this job. Never apply past a WARN silently.
- Every question says what it decides and what each answer changes.
- When the installer prints an ERROR, quote it, look it up in `docs/troubleshooting.md`, and
  never guess a fix you cannot ground in the message.

### Only you can do these

- **Grant `CAN_MANAGE_RUN`** on the healer job to each job's run-as identity: a workspace admin
  decides who may start what. The installer reports, it never grants.
- **Decide each WARN** (an existing failure handler, Run Job nesting): it depends on how the team
  runs that job, which the definition does not show.
- **Switch shadow mode off**: it is the moment alerts and issues go live for the whole team.

## Steps

0. **Find where you are.** Need `config.yaml` with a `healer_job_id`; if either is missing, say so
   and route to `/self-healing-build` first. Read `policy.shadow` and report it. In step 2, a job
   that already has the current leaf task prints `already installed and up to date`; report those
   and leave them alone. A job installed with a propagate task needs the same `--propagate-*`
   flags in the dry run, or the diff shows the propagate task being removed: never apply that by
   accident.
1. **Collect inputs**: the healer job id (from `config.yaml`), the Databricks profile, the job ids
   from `JOB_AUDIT.md`'s candidate list. Decide `--propagate-compute`: leave it `none` unless the
   team relies on the job's own `on_failure` notifications staying exactly as they are, or
   `slack.enabled` is false (then the job's own notifications are the only alert anyone gets); if
   so, use `serverless:<environment_key>` where serverless is enabled, otherwise a small
   `job_cluster_key`, and pass `--propagate-file jobs/self_healing/propagate.py`. Explain the trade-off before asking: without it, a failed job ends
   `SUCCESS_WITH_FAILURES` and its own `on_failure` webhook stops firing (the healer's alert
   replaces it); with it, the run is designed to stay `FAILED` (confirm with checklist items 10
   and 11) but the propagate task runs compute only to raise.
2. **Dry run every job**:
   ```
   python tools/install_trigger.py --profile <p> --healer-job-id <id> --job-id <a> --job-id <b>
   ```
   Read the diff. The healer task must depend on every original task, have
   `run_if: AT_LEAST_ONE_FAILED`, no compute of its own, and `max_retries: 0`. Read the WARN lines:
   existing `run_if` handlers and Run Job nesting need a human decision.
3. **Resolve permission findings**. If the tool reports that a job's run-as identity lacks
   `CAN_MANAGE_RUN` on the healer job, ask the workspace admin to grant it. The tool never grants.
4. **Apply with confirmation**, one job or a reviewed batch. The installer's own prompt needs a
   terminal, and the agent's shell has none (it refuses `--apply` without `--yes`). So show the
   user the exact dry-run diff for these job ids, get an explicit yes in chat, then run the same
   command with `--apply --yes`:
   ```
   python tools/install_trigger.py --profile <p> --healer-job-id <id> --job-id <a> --apply --yes
   ```
   A pre-change backup of each definition lands in `.healwright-backups/` (gitignored). Tell the
   user where, and that `--uninstall --apply` reverses the change.
5. **Validate in shadow mode** with the throwaway test job described at the top of
   `docs/live-validation-checklist.md` (task `a` fails on purpose, task `b` depends on it). Ask
   before creating it; it is a new job in their workspace. Install the trigger on it the same way,
   trigger it, then confirm: the healer run
   started from the leaf task, one row in the incidents table with `source=leaf-task`, the job's
   streak is 1, and nothing was posted. Trigger a successful run and confirm the streak resets.
6. **Work the live checklist** (`docs/live-validation-checklist.md`) for your platform, in its
   stages. Stage A runs in shadow mode: what `AT_LEAST_ONE_FAILED` does on a cluster-launch
   failure, a timeout, a cancel; whether the propagate task restores `FAILED`. Stage B is a
   rehearsal that needs a second healer job posting live for the test jobs only, so the RESOLVED
   message, the issue body and the fix pipeline can be checked while real jobs stay in shadow.
   Creating that job and re-pointing the test jobs at it are outward actions: explain the setup
   from the checklist and ask first. Record the answers in the repo, in a copy of the checklist
   next to `JOB_AUDIT.md`.
7. **Switch shadow off** (`policy.shadow: false`) on the main healer by a named person, following
   the checklist's Sign-off section (stage C). Ask; do not decide for them. Then remove the
   rehearsal job and the test jobs' triggers. Keep the hourly reconcile schedule: it is the
   safety net for a healer outage.

## Done when

Every candidate job shows the `__healwright` leaf task in its live definition, the test job
produced exactly one incident through the trigger path, the checklist answers are recorded, and
shadow mode is off by an explicit decision.

## Next

- Uninstall or re-run at any time: `python tools/install_trigger.py --profile <p> --job-id <a> --healer-job-id <id> --uninstall --apply`.
- Step 4 of the methodology is the caller workflow from `/self-healing-build`; watch the first
  CODE_BUG issue go through it (`docs/live-validation-checklist.md` items 20 to 23).
- New jobs: run `/self-healing-audit` on the additions, then this skill again.
