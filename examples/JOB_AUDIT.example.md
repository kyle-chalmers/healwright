# Job audit (example)

> This is a **synthetic** example of the file `/self-healing-audit` writes at the root of a jobs
> repo. The jobs, schedules and findings are invented to show the shape. Yours will list your jobs.

Audited: 2026-01-15, from `databricks/job_definitions/` (read) and `jobs/` (source read around
every side-effect hit). Judgement calls confirmed by: the data platform on-call lead.

## Tier legend

| Tier | Meaning | Native retries |
|---|---|---|
| Idempotent | Re-running produces the same result | `max_retries: 2` |
| Idempotent with guard | Re-run is safe because the code checks before it acts | `max_retries: 1` |
| Side-effecting | Re-run repeats a delivery or a send; humans rerun deliberately | `max_retries: 0` |
| REVIEW | Could not be determined; stays at 0 until someone decides | `max_retries: 0` |

## Jobs

| Job | Job id | Run as | Schedule | Tasks | Paused | Retries now | on_failure | Side effects (evidence) | Tier | Basis |
|---|---|---|---|---|---|---|---|---|---|---|
| `orders_daily_load` | 1001 | sp-etl | 02:00 daily | 3 | no | 0 | email | none; `MERGE` on `order_id` (`jobs/orders_daily_load/load.py`) | Idempotent | read |
| `inventory_snapshot` | 1002 | sp-etl | hourly | 2 | no | 1 | none | overwrites one partition per hour | Idempotent | read |
| `partner_export` | 1003 | sp-etl | 05:30 daily | 2 | no | 3 | Slack | SFTP put of a dated file; skips when the file exists on the server (`export.py`, `if sftp.exists(...)`) | Idempotent with guard | read |
| `weekly_summary_email` | 1004 | creator | Mon 07:00 | 1 | no | 2 | none | sends an email to a distribution list (`notify.py`) | Side-effecting | read |
| `ledger_append` | 1005 | sp-etl | 03:00 daily | 2 | no | 0 | email | `INSERT INTO` with no key; a re-run likely duplicates rows | REVIEW | inferred |
| `legacy_backfill` | 1006 | creator | none | 1 | yes | 0 | none | not inspected (paused) | n/a | n/a |

## Changes to apply (step 2, your deploy path)

- `orders_daily_load`: `max_retries` 0 to 2.
- `partner_export`: `max_retries` 3 to 1.
- `weekly_summary_email`: `max_retries` 2 to 0. A retry would send the email twice.
- `inventory_snapshot`: already compliant.
- `ledger_append`: stays 0 until the owner confirms whether the table is deduplicated downstream.

## Trigger candidates

| Job | Why |
|---|---|
| `orders_daily_load` | unpaused, multi-task, source under `jobs/` |
| `inventory_snapshot` | unpaused, multi-task, source under `jobs/` |
| `partner_export` | unpaused, multi-task, source under `jobs/` |
| `ledger_append` | unpaused, multi-task; retries stay at 0 until its REVIEW is settled, so the trigger is its only safety net |

Not candidates: `weekly_summary_email` (single task; convert it first if you want it monitored),
`legacy_backfill` (paused).

No job here uses Run Job tasks, so the nesting warning does not apply. The installer needs each
job's run-as identity (`sp-etl` here) to hold Can manage run on the healer job;
`weekly_summary_email` and `legacy_backfill` run as their creator, which the installer cannot
check by itself.

## Open questions

- `ledger_append`: is the target table deduplicated downstream? Owner to confirm before any
  retry is turned on.
