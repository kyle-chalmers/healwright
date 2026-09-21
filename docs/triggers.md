# Triggers: how a failure reaches the healer

Four designs were evaluated. One is primary, one is the safety net, one is documented for other
platforms, one is rejected. The Databricks semantics table at the end separates what the
documentation states from what a workspace still has to confirm.

## T1. In-job leaf task (primary)

The installer appends `__healwright`, a **Run Job** task that depends on every existing task with
`run_if: AT_LEAST_ONE_FAILED`, calling one shared healer job with
`{{job.id}} {{job.run_id}} {{job.name}} {{workspace.id}} {{workspace.url}}` as job parameters.

Why it wins:

- **No infrastructure.** No public endpoint, no relay, no secret material outside the workspace.
- **Runs after native retries.** `run_if` is evaluated when the dependencies finish, retries included.
- **Sees what webhooks cannot.** A run where one task failed but every leaf succeeded ends
  `SUCCESS_WITH_FAILURES`, which the platform counts as success: job-level `on_failure` webhooks
  do not fire. The leaf task does.
- **Fires even when the cluster never launched.** Run Job tasks need no compute of their own.
  Whether `AT_LEAST_ONE_FAILED` is evaluated for a cluster-launch failure is a live check (below).

Why it must depend on **every** task, not just the leaves: if task A fails, its downstream B is
marked `Upstream failed`, not `Failed`. A healer that only depends on B may never see a failure.

The trade-off: once `__healwright` succeeds, the run that had a real failure ends
`SUCCESS_WITH_FAILURES`, and the job's own `on_failure` notifications stop firing. Two answers:

1. Let the healer own alerting (default). It posts a threaded, classified alert within a minute.
2. Opt into `__healwright_propagate`: a tiny task that raises after any upstream failure, so the
   run stays `FAILED`. It needs compute (`propagate.compute` must be explicit: serverless, a job
   cluster key, or an existing cluster). Reusing the job cluster can keep it alive, or launch it,
   just to raise. `docs/guardrails.md` has the cost discussion.

## T4. Reconciler (safety net)

The same healer job on a schedule (hourly by default) with `mode=reconcile`. It lists completed
runs from `watermark - overlap` to now, capped per sweep, processes successes first, then
incidents, then advances the watermark. Same incident key, same claim, so it never double-counts
anything the trigger already handled. It exists for one reason: a healer outage, a permission
change, or a job installed without the leaf task must not lose an incident.

Compared with the polling monitor this replaces, it has no wall-clock lookback cap. A daily
schedule or a 20-hour outage is fine; the next sweep continues from the watermark. Because the
Jobs API filters on run **start** time and returns only completed runs, the window reaches back
`max_run_hours` (default 24) before the watermark so a long run that straddled the last sweep is
still found; dedup makes the re-listing harmless. `expand_tasks` returns at most 100 tasks per
run; jobs larger than that need `get_run` per run, which the leaf-task path already does.

## T2. Webhook relay (documented, code in the backlog)

Databricks job and task notifications can POST to a webhook destination. Facts to design around:

- The payload is fixed: `event_type`, `workspace_id`, `run.run_id` (and `parent_run_id` for
  task-level events), `job.job_id`, `job.name`, `task.task_key` for task-level events. **No error
  text.** The receiver must call the Jobs API to learn anything, so a platform credential lives
  outside the workspace.
- Job-level and task-level payloads differ: for a task-level event `run.run_id` is the **task**
  run, and the job run is `parent_run_id`. Normalise to the parent before building the incident key.
- Destinations support URL plus HTTP Basic auth only. No custom headers, no signing. The
  endpoint must be HTTPS with a trusted certificate; IP allow-listing is on you.
- Up to three destinations per event type per job: **add** the relay as a second destination,
  never repoint the existing Slack one.
- `on_failure` does not fire for `SUCCESS_WITH_FAILURES` (see T1).
- Whether delivery is retried on a non-2xx response is not documented. Dedupe anyway; the incident
  key handles it.

A relay is the right shape for Airflow, Dagster, or Prefect, which can all POST on failure.
`examples/relay/` holds a stdlib receiver sketch and the deployment notes.

## T3. Webhook straight to GitHub (rejected)

It is tempting: point the Databricks destination at
`https://api.github.com/repos/o/r/dispatches` with Basic auth (login + PAT). It does not work
well enough to ship: GitHub ignores the unknown top-level fields, so `client_payload` is empty
and the workflow has to re-scan the workspace for the failing run, which is polling relocated
into GitHub Actions. It also stores a raw PAT as a Basic-auth password on a notification
destination, the opposite of the org-owned App tokens the rest of the pipeline uses.

## Databricks semantics: documented vs. still to confirm live

| Claim | Status | Source |
|---|---|---|
| `run_if` values: ALL_SUCCESS, AT_LEAST_ONE_SUCCESS, NONE_FAILED, ALL_DONE, AT_LEAST_ONE_FAILED, ALL_FAILED | documented | jobs/run-if |
| Excluded upstream tasks count as successful for `run_if` | documented | jobs/run-if |
| Cancelling a task runs downstream failure handlers | documented | jobs/run-if |
| Intermediate failure + all leaves succeed = `SUCCESS_WITH_FAILURES`, treated as success by notifications | documented | jobs/monitor, jobs/notifications |
| Run Job tasks need no compute; parameters can carry `{{job.run_id}}` etc. | documented; also seen in production job JSON | jobs/parameters, dynamic value references |
| Run Job nesting limit (three levels) | documented | jobs docs |
| `fields_to_remove: ["tasks/<task_key>"]` on Jobs 2.1 update | documented | Jobs API reference |
| Webhook payload shape, Basic auth only, 3 destinations per event type | documented | jobs/notifications, notification destinations |
| `AT_LEAST_ONE_FAILED` fires when the upstream **cluster failed to launch** | **live check** | |
| `AT_LEAST_ONE_FAILED` fires on **job timeout** and **whole-run cancel** | **live check** | |
| `AT_LEAST_ONE_FAILED` waits for upstream **retries** to be exhausted | **live check** (implied by "dependencies have run") | |
| A Run Job task's own result mirrors the child job's result | **live check** | |
| `__healwright_propagate` restores `FAILED` and re-fires `on_failure` | **live check** | |
| Webhook redelivery on non-2xx | **live check** | |

`docs/live-validation-checklist.md` turns the live checks into a procedure.
