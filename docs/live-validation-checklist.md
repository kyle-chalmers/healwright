# Live validation checklist

The local tests prove the logic. They cannot prove how your platform behaves. Work through this
list in your workspace before switching alerts on, and record the answers in your repo. Items
marked release-blocking decide whether the trigger path can be trusted at all.

Use a throwaway job with two tasks (`a` → `b`) where `a` fails on purpose, installed with
`tools/install_trigger.py` like any other job. A second throwaway job whose cluster spec is
invalid (a node type that does not exist) covers the launch-failure case. For the fix-pipeline
items, the test job's notebook must live in its own folder under `github.source_root` in your
jobs repo, so the pipeline has a folder to edit.

## The stages

Shadow mode (`policy.shadow: true`) posts nothing, so some checks cannot run in it: a RESOLVED
message, an issue body, and everything in the fix pipeline. Those run in a **rehearsal** where
only the throwaway test jobs go live, while every real job stays in shadow.

| Stage | What runs | Items |
|---|---|---|
| **A. Shadow** | The main healer, `policy.shadow: true`, the trigger on the test jobs and on your real candidates | 1 to 16, 18 |
| **B. Rehearsal** | A second healer job that posts, called only by the test jobs | 17, 19 to 24 |
| **C. Live** | The main healer with `policy.shadow: false` | sign-off below |

**Setting up stage B.** No code changes; it is the same healer folder with a second config.

1. Copy `config.yaml` to `config.rehearsal.yaml` beside it and change four things:
   `policy.shadow: false`; `healer_job_name: healwright-healer-rehearsal` (and `healer_job_id`
   once the job exists); `jobs.include` set to the test job names only; and
   `state.delta_schema` set to a separate schema, so rehearsal streaks never mix with real ones.
   Point `slack.channel` at a test channel. Keep `github.repo` as your real jobs repo: the fix
   pipeline is what you are testing.
2. Create a second job from `healer_job.json` named `healwright-healer-rehearsal`, with the
   `config_path` parameter's default set to the path of `config.rehearsal.yaml`. Keep its hourly
   schedule on: the leaf task only fires on failure, so the scheduled `mode=reconcile` run is what
   sees a test job's success (item 17's RESOLVED message and streak reset) and posts the
   follow-up after the fix workflow finishes (item 22). `jobs.include` limits it to the test
   jobs. To skip the wait, start the rehearsal job by hand with `mode=reconcile` after the
   success run and after the fix workflow completes.
3. Re-install the trigger on the test jobs only, pointing at the rehearsal healer:
   `--uninstall --apply` with the main healer id, then a dry run and `--apply` with
   `--healer-job-id` set to the rehearsal job's id. Real jobs keep calling the main healer. The
   main healer's hourly reconciler still sees the test jobs' runs and records them in shadow;
   add the test job names to `jobs.exclude` in `config.yaml` if you want its state kept clean.
4. Confirm the first rehearsal run read the rehearsal config (its run shows the `config_path`
   parameter, and its incident lands in the rehearsal schema). The leaf task does not pass
   `config_path`, so this relies on the job-level default applying; record what you saw.

When stage B is done, uninstall the trigger from the test jobs and delete the rehearsal job.

## Trigger semantics (release-blocking, stage A)

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

## Propagate task (if enabled, stage A)

| # | Check | Expected | Result |
|---|---|---|---|
| 10 | Run ends `FAILED`, not `SUCCESS_WITH_FAILURES` | | |
| 11 | The job's existing `on_failure` webhook fires exactly once | | |
| 12 | The healer never classifies `__healwright_propagate` | no incident_tasks row for it | |
| 13 | Compute cost per run for the chosen `propagate.compute` | measure | |

## Healer behaviour in the workspace

| # | Stage | Check | Expected | Result |
|---|---|---|---|---|
| 14 | A | Delta tables created in `state.delta_schema` on first run | five `healwright_*` tables | |
| 15 | A | Two healer runs for one parent run (re-trigger the leaf task) | second reports `done` | |
| 16 | A | Reconciler after a simulated healer outage (pause the healer, fail a job, unpause) | incident recorded with `source=reconcile`, streak 1 | |
| 17 | B | Success after failure | RESOLVED posted in the failure thread; streak 0; issue fields cleared | |
| 18 | A | Delta under two concurrent writers (`max_concurrent_runs` > 1, fail two jobs at once) | one owner per incident; no duplicate `action_log` rows | |
| 19 | B | Redaction on a real trace | paste a trace with a fake token into a failing test notebook; confirm `[REDACTED:...]` in the issue body | |

## Fix pipeline (stage B)

| # | Check | How | Expected | Result |
|---|---|---|---|---|
| 20 | Issue by the healer bot with the label triggers the caller workflow | fail the test job with a CODE_BUG | `fix.yml` runs once | |
| 21 | AI step cannot push | try to make the test fix include a workflow file change | run fails on scope; edits discarded; comment + `needs-human` | |
| 22 | Success path | one-line bug in the test notebook | draft PR opened, comment on the issue, follow-up posted by the healer | |
| 23 | No-fix path | make the bug unfixable from the folder | comment with the summary + `needs-human`, no PR | |
| 24 | Two label events within seconds | remove and re-add the label quickly | second run waits for the first (concurrency group) | |

## Sign-off (stage C)

Shadow mode on the main healer is switched off by a named person after items 1 to 3 and 14 to
16 are green in stage A, items 17 and 20 to 23 are green in stage B, and the rest have a recorded
answer. Anything red stays in the reconciler-only configuration until it is understood.
