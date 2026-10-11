# Troubleshooting

Every entry has the same shape: the **symptom** you see (quoted exactly when the tool prints it),
the **cause**, and the **fix**. The healer explains itself in the `notes` list of its JSON result,
and the installer in its `WARN` and `ERROR` lines, so start from that text.

One rule for people and agents alike: if nothing here matches, report the exact message and the
step that printed it. Do not guess a fix you cannot ground in the message or the run log.

When you add an entry, use the same three fields, quote the message exactly, and put it in the
section of the tool that prints it.

## The healer: "why didn't it post or open an issue?"

**1. `"notes": ["shadow mode: alert not posted"]` or `"shadow mode: issue not opened"`**
- Cause: `policy.shadow: true`. State is written, nothing leaves. `/self-healing-build` turns it
  on for every new install (`config.example.yaml` ships `false`).
- Fix: none while you validate. Switch it off after the live checklist sign-off
  (`docs/live-validation-checklist.md`).

**2. `"slack disabled"` or `"github disabled"`**
- Cause: `slack.enabled` or `github.enabled` is `false` in `config.yaml`. Both default to `false`
  in `config.example.yaml`.
- Fix: set it to `true` and make sure the referenced secrets exist in the secret scope.

**3. `"alert suppressed: ..."`**
- Cause: the same category and pattern failed again within `policy.dedup_window_hours`. The
  incident and the streak are recorded; only the repeat alert is skipped.
- Fix: none if that is what you want. Lower `dedup_window_hours` if repeats should page.

**4. `"no issue: notebook path outside source_root"`**
- Cause: the failing task's notebook path does not map to a folder under `github.source_root`,
  so the fix pipeline could not scope a change to one folder. A stray space in the path once
  caused this for four jobs (`docs/lessons.md`, "Notebook paths").
- Fix: check `source_root` against the job's notebook path. Jobs outside it get an alert and no issue, by design.

**5. `"no issue: tracked #N still open"`**
- Cause: this job already has an open fix issue for the current streak. One issue per job per streak.
- Fix: work or close issue #N. The next failure after it closes can open a new one.

**6. `"no issue: N/hour cap reached"`**
- Cause: `policy.max_issues_per_hour` was hit across all jobs, usually during a platform-wide incident.
- Fix: none for a real incident. Raise the cap only if the volume is legitimate.

**7. Second local simulation prints `"outcome": "done"` with `"notes": ["already handled"]`**
- Cause: the duplicate guard. The same run id was already processed in that store.
- Fix: none; this is the expected result. Use a new `--run-id` or a fresh `--store` path to see a
  first run again.

**8. A transient failure (a 503, a timeout) was classified CODE_BUG or UNKNOWN**
- Cause: no pattern in `patterns.yaml` matched its shape, or a broader pattern matched first.
- Fix: add the pattern under `classification.patterns` in `config.yaml`. In this repo, add it to
  `templates/healer/patterns.yaml` with a golden test in `tests/test_classify.py` built from a
  synthetic trace of the same shape (`docs/lessons.md`, "Classification").

## The job: "something changed after I installed the trigger"

**9. The job's own `on_failure` Slack or email stopped arriving; runs now end `SUCCESS_WITH_FAILURES`**
- Cause: the leaf task succeeded, so the platform no longer sees the run as failed.
- Fix: either rely on the healer's alert (classified and threaded), or reinstall with
  `--propagate-compute` and `--propagate-file` (the repo path of `propagate.py`) so a propagate task re-raises. It is designed to make the run end `FAILED` again; confirm that
  with checklist items 10 and 11 before you rely on it. It costs compute;
  see `docs/guardrails.md`, "The propagate task".

## The installer

**10. `ERROR: this is the healer job itself; it must not monitor itself` or `ERROR: job_id equals healer_job_id`**
- Cause: the healer job's id was passed as a `--job-id`.
- Fix: drop it from the list. The healer is never monitored.

**11. `ERROR: job has no tasks (legacy single-task format?)` or `ERROR: job format is ...; only MULTI_TASK jobs are supported`**
- Cause: the leaf task needs a multi-task job to depend on.
- Fix: convert the job to multi-task with your normal deploy path, then dry-run again.

**12. `ERROR: task 'x' already calls the healer job directly`**
- Cause: someone wired a call to the healer by hand.
- Fix: remove the hand-wired task through your deploy path, then install, so there is exactly one trigger.

**13. `WARN: task 'x' has run_if=...; the job already handles failures somewhere; review interaction`**
- Cause: the job already has a failure handler. The leaf task will run alongside it.
- Fix: a human decision. Read what the handler does; if it alerts, decide whether the team wants
  both alerts during shadow mode, and which one after.

**14. `WARN: job contains Run Job tasks; Databricks limits Run Job nesting to ... levels`**
- Cause: the leaf task adds one level of Run Job nesting, and the installer cannot see who calls
  this job.
- Fix: check whether another job calls this one with a Run Job task. If so, test it with
  checklist item 8 before relying on the trigger.

**15. `... has no CAN_MANAGE_RUN on the healer job; grant it (Permissions > Can manage run) before installing`**
- Cause: the job's run-as identity cannot start the healer job, so the leaf task would fail.
- Fix: a workspace admin grants Can manage run on the healer job to that identity. The installer never grants.

**16. `could not read healer job permissions: ...` or `job has no explicit run_as; the creator's identity applies and could not be checked`**
- Cause: the installer could not verify the permission, so it blocks by default.
- Fix: confirm the permission in the UI, then re-run with `--skip-permission-check`. Do not use
  the flag to skip a check you have not done by hand.

**17. `ERROR: definition changed since read; aborting (re-run)`**
- Cause: someone edited the job between the installer's read and its write.
- Fix: dry-run again, read the new diff, then apply.

**18. `refusing --apply without --yes in a non-interactive shell`**
- Cause: `--apply` asks for confirmation, and there is no terminal to ask in (CI, a piped shell, an agent).
- Fix: run it in a terminal, or pass `--yes` only after you have reviewed that exact dry run.

**19. `already installed and up to date`**
- Cause: the job already has the current leaf task.
- Fix: none. Re-running the installer is safe.

## The fix workflow

**20. The healer opened an issue but `fix.yml` never ran**
- Cause: the caller workflow starts only when an issue is labelled `self-healing-fix`. The label
  is missing from the issue, or from the repo, or the caller file is not on the default branch.
- Fix: check the label exists on the repo and on the issue, and that
  `.github/workflows/self-healing-fix.yml` is on the default branch.

**21. The issue got a comment starting `The issue did not pass the gate:` and `needs-human`**
- Cause: `fix.yml` ran, and its gate job refused the issue before any AI step. The comment lists
  why, for example `issue author '...' is not the healer bot '...'` (a wrong `bot_login`) or
  `could not derive a job folder under 'jobs' from notebook path '...'` (a `source_root` mismatch).
- Fix: correct the value the comment names. `bot_login` in the caller must equal the issue's
  author exactly, and the caller's `source_root` must equal `github.source_root` in `config.yaml`.

**22. Every fix run fails with a 401 while alerts keep working**
- Cause: the model credential (`CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`) or the App key
  expired or was revoked. A user-minted token died with its account once and nobody noticed for
  two months (`docs/lessons.md`, "The fix pipeline").
- Fix: rotate the secret. Prefer org-owned credentials, and treat issues that get neither a PR nor
  `needs-human` as a pipeline health alarm.

**23. The issue got `needs-human` and a comment saying the patch was out of scope**
- Cause: the AI's patch touched a path outside the job's folder, or a forbidden path
  (`.github/`, `.claude/`, `AGENTS.md`, `CLAUDE.md`). The publish step discarded it.
- Fix: none to the pipeline; it worked. A human takes the issue from the comment's diagnosis.
