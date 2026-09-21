# Architecture

```
 source ──► RunIncident (+ task failures) ──► classify ──► store.claim (lease) ──► policy ──► actions (receipts)
 │
 ├ leaf-task    __healwright Run Job task in the failing job; run_if AT_LEAST_ONE_FAILED   PRIMARY
 ├ reconcile    hourly sweep of completed runs from a durable high-water mark (+ overlap)  SAFETY NET
 └ simulate     fabricated incident, local only                                            TESTS / DEMOS
```

Everything lives in `templates/healer/healwright_core.py`, in this order.

## Models

- `RunIncident`: one failed run of one job. `tasks` holds the failed tasks (`TaskFailure`), with
  the sentinel task keys (`__healwright`, `__healwright_propagate`) filtered out by
  `failed_tasks`. Key: `incident_key = sha1(platform|tenant|workspace_id|job_id|run_id)`.
  Every source produces the same key for the same run, so the leaf task and the reconciler
  never count a streak twice.
- `RunSuccess`: a successful run, used to reset a streak and post RESOLVED.
- `Classification`: `category` (TRANSIENT, UPSTREAM_DATA, CONFIG_ERROR, CODE_BUG, UNKNOWN),
  `stage` (STAGE_1 termination code or result state, STAGE_2 regex), `pattern`.
- `JobState`: the per-job row (streak, last classification, Slack thread, tracked issue, issue
  claim token, fix-notified flag).

## Classification

`Classifier` loads `patterns.yaml`. Stage 1 maps structured platform signals (termination codes,
`TIMEDOUT`) straight to a category. Stage 2 runs ordered regexes over
`state_message + error_message + strip_source_context(trace)`; category order is fixed and
load-bearing (see the comment in `patterns.yaml`). `strip_source_context` removes traceback lines
that merely quote a `raise` statement, because a library's own error-handling source once turned
a transient 503 into a CODE_BUG. Consumers add patterns in their `config.yaml` under
`classification`; user patterns win within a category.

## State store

`StateStore` is the contract; `SqliteStore` is the tested reference and `DeltaStore` mirrors it
with `spark.sql` MERGE / UPDATE. Timestamps are ISO-8601 UTC strings on disk everywhere, so no
backend can reintroduce a local-time column next to a UTC one.

Tables: `incidents` (one row per run, with `status`, `lease_until`, `attempts`,
`consecutive_failures`, `alert_suppressed`), `incident_tasks`, `job_state`, `action_log`
(receipts, PK `(incident_key, action)`), `watermarks`.

`claim_incident` is the atomic step. Every insert carries an owner token that is read back before
the row is treated as ours; the job's streak moves only after that read-back succeeds. It returns:

| status | meaning | what the healer does |
|---|---|---|
| `new` | first time this run is seen; streak incremented, suppression decided from pre-state | full processing |
| `resumed` | a previous owner's lease expired before `complete_incident` | actions again; receipts skip what was already done |
| `busy` | another healer owns it right now | nothing |
| `done` | already completed | nothing |

`apply_success` resets a streak only if the success started after the last recorded failure.
`claim_incident` for a failure that started before the last recorded success records the incident
but does not regress the streak (a late reconciler event).

## Policy

`should_suppress`: same category and pattern as the previous failure within `dedup_window_hours`.
Decided from the **pre-update** state at claim time and stored on the incident, so a resumed
incident makes the same decision. `needs_new_thread`: no thread, or older than
`thread_reset_hours`. Rate limits: `max_alerts_per_run` per healer invocation with a flood summary,
`max_issues_per_hour` counted from `action_log`.

## Actions

`SlackClient` and `GitHubClient` speak HTTP through a `Transport` callable. `urllib_transport` is
the real one; `RecordingTransport` records calls and answers from a responder, which is how tests
and dry runs work. Actions post first and write their receipt second, so a failed post leaves no receipt and is
retried; a crash between "posted" and "receipt written" produces at worst a duplicate comment,
never a lost incident. When any external action fails, the incident is **released** (status
`pending`, no lease) instead of completed, so the reconciler picks it up again.

Issue creation is a two-level claim: `record_action(incident, "github_issue")` for this run, and
`claim_issue_slot(job)` for the job, both with leases. Before creating, the client searches open
issues for the incident marker as a net for a previous crash.

## Platform

`Platform` is a protocol with `fetch_run(run_id)` and `list_completed_runs(start_ms, end_ms)`.
`DatabricksPlatform` uses `databricks-sdk`: `jobs.get_run` (tasks included), `jobs.get_run_output`
per failed task (task run id, not the job run id), and the termination code from
`status.termination_details.code` when it is specific, else the cluster termination reason.
It decodes both the legacy `state` and the 2.2 `status` shapes. In leaf-task mode the parent run
is still RUNNING (the `__healwright` task is part of it), so the incident is built from the failed
tasks and `result_state` is derived as FAILED. Sentinel tasks are excluded at this layer.

## Healer

`Healer.handle_incident`: filter (`jobs.include/exclude`, never the healer itself) → classify →
claim → alert (threaded, suppressed, capped) → issue (eligibility, cap, tracked issue still open,
slot claim, marker search, create, record) → complete. `handle_success`: RESOLVED in the failure
thread, then reset. `run_reconcile`: the window reaches back from the watermark by `max_run_hours` (the Jobs API
filters on start time and returns only completed runs, so a long run that started before the last
sweep would otherwise never be seen), successes first, then incidents oldest to newest, then
advance the watermark. `run_followups`: once per issue, tell the channel
whether the fix pipeline opened a PR or labelled `needs-human`.

`dry_run=True` swaps in a `RecordingTransport` and prints what would have been sent.
`policy.shadow=true` keeps the real transports but never posts: state is written, nothing leaves.
