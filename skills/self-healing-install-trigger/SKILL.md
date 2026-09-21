---
name: self-healing-install-trigger
description: Step 3 of the healwright methodology. Add the trigger-based leaf task to monitored jobs so a failure calls the shared healer immediately, with a dry run and diff per job, permission preflight, confirmation, and a shadow-mode validation before alerts go live. Use after /self-healing-build.
argument-hint: "<job-id ...> --healer-job-id <id>"
allowed-tools: [Bash, Read, Grep, Glob]
---

# self-healing-install-trigger

The installer edits live job definitions. It is dry-run by default, fetches the live definition
(repo JSONs are not authoritative), sends only the task list, and refuses when the definition
changed between read and write. You still review every diff.

## Steps

1. **Collect inputs**: the healer job id (from `config.yaml`), the Databricks profile, the job ids
   from `JOB_AUDIT.md`'s candidate list. Decide `--propagate-compute`: leave it `none` unless the
   team relies on the job's own `on_failure` notifications staying exactly as they are; if they
   do, use `serverless:<environment_key>` where serverless is enabled, otherwise a small
   `job_cluster_key`. Explain the cost: the propagate task runs compute only to raise.
2. **Dry run every job**:
   ```
   python tools/install_trigger.py --profile <p> --healer-job-id <id> --job-id <a> --job-id <b>
   ```
   Read the diff. The healer task must depend on every original task, have
   `run_if: AT_LEAST_ONE_FAILED`, no compute of its own, and `max_retries: 0`. Read the WARN lines:
   existing `run_if` handlers and Run Job nesting need a human decision.
3. **Resolve permission findings**. If the tool reports that a job's run-as identity lacks
   `CAN_MANAGE_RUN` on the healer job, ask the workspace admin to grant it. The tool never grants.
4. **Apply with confirmation**, one job or a reviewed batch:
   ```
   python tools/install_trigger.py --profile <p> --healer-job-id <id> --job-id <a> --apply
   ```
   A pre-change backup of each definition lands in `.healwright-backups/` (gitignored).
5. **Validate in shadow mode** with a throwaway test job that fails on purpose (a one-cell
   notebook raising `KeyError`), installed the same way. Trigger it, then confirm: the healer run
   started from the leaf task, one row in the incidents table with `source=leaf-task`, the job's
   streak is 1, and nothing was posted. Trigger a successful run and confirm the streak resets.
6. **Work the live checklist** (`docs/live-validation-checklist.md`) for your platform: what
   `AT_LEAST_ONE_FAILED` does on a cluster-launch failure, a timeout, a cancel; whether the
   propagate task restores `FAILED`. Record the answers in the repo.
7. **Switch shadow off** (`policy.shadow: false`) once the team has seen incidents land correctly.
   Keep the hourly reconcile schedule: it is the safety net for a healer outage.

## Done when

Every candidate job shows the `__healwright` leaf task in its live definition, the test job
produced exactly one incident through the trigger path, and shadow mode is off by an explicit
decision.

## Next

- Uninstall or re-run at any time: `python tools/install_trigger.py --job-id <a> --healer-job-id <id> --uninstall --apply`.
- New jobs: run `/self-healing-audit` on the additions, then this skill again.
