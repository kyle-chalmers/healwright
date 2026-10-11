# Glossary

The words healwright uses, in the order you meet them. Each entry points to where the term lives.

## The methodology

- **Idempotent job**: re-running it produces the same result as running it once. Safe to retry.
  The audit decides this per job (`/self-healing-audit`).
- **Side-effecting job**: re-running it repeats something outside the data platform: a file
  delivery, an email, an API write. Never retried automatically.
- **Retry tier**: the `max_retries` a job gets from the audit (2, 1 or 0). Retries are the
  platform's native `retry_policy`; the healer never retries anything.
- **REVIEW**: an audit row nobody could classify with confidence. It stays at `max_retries: 0`
  until a person decides.
- **Candidate**: a job the audit picked for the trigger: unpaused, multi-task (or converted to
  multi-task first), and with source under `source_root` so code bugs can get a fix PR.

## Detection

- **Healer job**: the one shared job that runs `healer.py`. Every monitored job calls it; it never
  monitors itself.
- **Leaf task** (`__healwright`): the task the installer appends to each monitored job. It depends
  on every other task and has `run_if: AT_LEAST_ONE_FAILED`, so it runs only when something failed,
  and is expected to run after the platform's own retries (checklist item 3 confirms it). It is a Run Job task, so it needs no compute.
- **Trigger path**: a failure reaching the healer through the leaf task, seconds after it happens.
  The primary path (`docs/triggers.md`).
- **Reconciler**: the same healer job on an hourly schedule, sweeping completed runs to catch what
  the trigger missed. The safety net, never the main path.
- **High-water mark** (watermark): the point in time the reconciler has swept up to. Stored in the
  `watermarks` table so an outage cannot lose incidents.
- **Propagate task** (`__healwright_propagate`): an opt-in second task that raises after the healer
  runs, designed so the job still ends `FAILED` and its own `on_failure` notifications keep firing
  (not yet confirmed live: checklist items 10 and 11). It costs
  compute (`docs/guardrails.md`, "The propagate task").
- **`SUCCESS_WITH_FAILURES`**: the run state Databricks reports when a task failed but the leaf
  task succeeded. What you get without the propagate task.

## Inside the healer

- **Incident**: one failed run of one job. Its **incident key** is a hash of the platform, tenant,
  workspace, job and run, so the trigger and the reconciler always agree on it.
- **Classification**: the category the healer gives a failure: TRANSIENT, UPSTREAM_DATA,
  CONFIG_ERROR, CODE_BUG or UNKNOWN. Stage 1 reads platform termination codes; stage 2 runs the
  regexes in `patterns.yaml`.
- **Streak**: the count of consecutive failures for a job. A success after the last failure resets
  it to 0 and posts RESOLVED in the same Slack thread.
- **Claim** and **lease**: before acting on an incident, a healer claims it in the state store and
  holds it for `policy.lease_seconds`. Another healer sees `busy` or `done` and does nothing; if the
  owner dies, the lease expires and the incident is `resumed`.
- **Receipt**: a row in `action_log` written after each external action (a Slack post, an issue).
  A resumed incident skips actions that already have a receipt.
- **Shadow mode** (`policy.shadow: true`): the healer classifies and writes state but posts
  nothing. `/self-healing-build` starts every install here (the example config ships `false`); a
  person switches it off.
- **Suppression**: a repeat of the same category and pattern within `dedup_window_hours` is
  recorded but not re-alerted.
- **Redaction**: `redact()` strips token shapes, keys, emails, connection strings and
  high-entropy strings from everything before it leaves the healer.

## The fix pipeline

- **Caller workflow**: the short `.github/workflows/self-healing-fix.yml` in your repo that calls
  healwright's reusable `fix.yml` when the healer opens an issue labelled `self-healing-fix`.
- **gate / investigate / publish**: the three jobs of `fix.yml`. `gate` fixes the allowed folder
  before the AI runs; `investigate` runs the AI with a read-only token; `publish` validates the
  patch against the folder and opens a draft PR.
- **`needs-human`**: the label the pipeline adds when it could not produce a safe fix. The issue
  always gets a comment saying why.
- **Eligible job**: one whose notebook path maps to a single folder under `source_root`. Only
  eligible jobs get fix-pipeline issues.

## Setup and identity

- **Databricks profile**: a named set of workspace credentials in `~/.databrickscfg`, passed to
  the CLI and installer as `--profile`.
- **Run-as identity**: the user or service principal a job runs as (`run_as`). Without one, the
  job runs as its creator.
- **Service principal**: a non-human Databricks identity. The healer job should run as one so it
  does not depend on a person's account.
- **Secret reference**: `${secret:scope/key}` in `config.yaml`, read from a Databricks secret
  scope at run time, or `${env:NAME}` from an environment variable. Values never go in the file.
- **`source_root`**: the repo folder that holds job source, set in `config.yaml`
  (`github.source_root`) and in the caller workflow. Must be the same in both.
- **`bot_login`**: the GitHub App's login as it appears on the issues it opens: its name followed
  by `[bot]`. The fix workflow only acts on issues opened by that exact login.

## The installer

- **Dry run**: the default. The installer prints the diff and changes nothing until `--apply`.
- **Fingerprint**: a hash of the live job definition, taken at read time and checked again just
  before the write. A mismatch aborts, because someone changed the job in between.
- **`CAN_MANAGE_RUN`**: the permission each monitored job's run-as identity needs on the healer
  job so the leaf task can start it. An admin grants it; the installer only reports.
- **Run Job nesting**: Databricks limits how deep Run Job tasks can call each other. The leaf task
  adds one level, so the installer warns on jobs that already use Run Job tasks.
