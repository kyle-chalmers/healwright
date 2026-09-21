# Live validation checklist

The local tests prove the logic. They cannot prove how your platform behaves. Work through this
list in your workspace **in shadow mode** (`policy.shadow: true`) before switching alerts on,
and record the answers in your repo. Items marked release-blocking decide whether the trigger
path can be trusted at all.

Use a throwaway job with two tasks (`a` → `b`) where `a` fails on purpose, installed with
`tools/install_trigger.py` like any other job. A second throwaway job whose cluster spec is
invalid (a node type that does not exist) covers the launch-failure case.

## Trigger semantics (release-blocking)

| # | Check | How | Expected | Result |
|---|---|---|---|---|
| 1 | `__healwright` runs when a task fails | run the test job | healer run starts with `parent_run_id` = the test run; one incident, `source=leaf-task` | |
| 2 | Depends-on-all works for an **upstream** failure | make `a` fail, `b` default `run_if` | `b` shows Upstream failed; `__healwright` still runs | |
| 3 | Fires after **retries** are exhausted | set `max_retries: 1` on `a` | healer runs once, after the retry | |
| 4 | **Cluster launch failure** | test job with an invalid node type | does `__healwright` run? does the incident classify STAGE_1 from the termination code? | |
| 5 | **Job timeout** | `timeout_seconds: 60` on the job, task sleeps longer | does `__healwright` run? result state seen by the healer? | |
| 6 | **Whole-run cancel** | cancel the run from the UI while `a` is running | does `__healwright` run? (documented for task cancel, not run cancel) | |
| 7 | Run Job task result mirrors the child | make the healer job fail on purpose once | parent's `__healwright` task state? parent run state? | |
| 8 | Nesting | install on a job that is itself called by another job's Run Job task | does the healer still run at depth 3? | |
| 9 | Permission | remove `CAN_MANAGE_RUN` from the run-as identity | what does the parent task show? does the installer's preflight catch it? | |

## Propagate task (if enabled)

| # | Check | Expected | Result |
|---|---|---|---|
| 10 | Run ends `FAILED`, not `SUCCESS_WITH_FAILURES` | | |
| 11 | The job's existing `on_failure` webhook fires exactly once | | |
| 12 | The healer never classifies `__healwright_propagate` | no incident_tasks row for it | |
| 13 | Compute cost per run for the chosen `propagate.compute` | measure | |

## Healer behaviour in the workspace

| # | Check | Expected | Result |
|---|---|---|---|
| 14 | Delta tables created in `state.delta_schema` on first run | five `healwright_*` tables | |
| 15 | Two healer runs for one parent run (re-trigger the leaf task) | second reports `done` | |
| 16 | Reconciler after a simulated healer outage (pause the healer, fail a job, unpause) | incident recorded with `source=reconcile`, streak 1 | |
| 17 | Success after failure | RESOLVED posted in the failure thread (once shadow is off); streak 0; issue fields cleared | |
| 18 | Delta under two concurrent writers (`max_concurrent_runs` > 1, fail two jobs at once) | one owner per incident; no duplicate `action_log` rows | |
| 19 | Redaction on a real trace | paste a trace with a fake token into a failing test notebook; confirm `[REDACTED:...]` in the issue body | |

## Fix pipeline

| # | Check | Expected | Result |
|---|---|---|---|
| 20 | Issue by the healer bot with the label triggers the caller workflow | `fix.yml` runs once | |
| 21 | AI step cannot push | try to make the test fix include a workflow file change | run fails on scope; edits discarded; comment + `needs-human` | |
| 22 | Success path | one-line bug in the test notebook | draft PR opened, comment on the issue, follow-up posted by the healer | |
| 23 | No-fix path | make the bug unfixable from the folder | comment with the summary + `needs-human`, no PR | |
| 24 | Two label events within seconds | remove and re-add the label quickly | second run waits for the first (concurrency group) | |

## Sign-off

Shadow mode is switched off by a named person after items 1 to 3, 14 to 17, and 20 to 23 are
green and the rest have a recorded answer. Anything red stays in the reconciler-only
configuration until it is understood.
