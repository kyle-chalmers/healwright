# healwright

**Make a failed data job heal itself, seconds after it fails, without anyone polling.**

healwright is a methodology and a reference kit, not a library. You copy one folder into your
jobs repo, point one GitHub workflow at ours, and end up with a self-healing loop you fully own
and can read end to end:

```
 job fails ─► in-job trigger calls the healer ─► classify ─► record streak ─► alert (threaded)
                                                                          └─► CODE_BUG: issue ─► guarded AI draft PR ─► human merges
```

It was generalized from a production system that watched a fleet of scheduled Databricks jobs
with a six-times-a-day polling monitor. The polling worked, and it also produced hours of latency,
races across cycle boundaries, and duplicate issues. This kit keeps everything that earned its
place (two-stage classification, streak state, threaded alerts, folder-scoped draft PRs) and
replaces the poll with a trigger that fires inside the failing job.

## The methodology, in four steps

| Step | What you do | What you get | Skill |
|---|---|---|---|
| 1. Audit | Inventory every job, classify idempotency and side effects | `JOB_AUDIT.md`, retry tiers, trigger candidates | `/self-healing-audit` |
| 2. Native retries | Put `retry_policy` on the jobs, by tier | Transient failures never reach a human | your deploy path |
| 3. Trigger-based detection | Copy `templates/healer/`, configure, install the leaf task | Failures classified and recorded within a minute | `/self-healing-build`, `/self-healing-install-trigger` |
| 4. Guarded AI remediation | Point a 15-line workflow at `fix.yml` | Draft PRs for code bugs, comments for everything else | `examples/caller-workflow.yml` |

The healer **observes**. It never retries, pauses, cancels, or edits a job. Retries are the
platform's job; fixes are draft PRs a human merges.

## How the trigger works

`tools/install_trigger.py` appends one task to each monitored job:

```
__healwright   Run Job task ─► the shared healer job
               depends_on: every existing task     run_if: AT_LEAST_ONE_FAILED
               parameters: {{job.id}} {{job.run_id}} {{job.name}} {{workspace.id}} {{workspace.url}}
```

Run Job tasks need no compute of their own, so this fires even when the parent's cluster never
launched. It runs only after the platform's own task retries are exhausted. It sees per-task
failures that end in `SUCCESS_WITH_FAILURES`, which job-level `on_failure` webhooks never emit.
And it needs no public endpoint: everything stays inside your workspace and your GitHub org.

A low-frequency **reconciler** (the same healer job on an hourly schedule) sweeps completed runs
from a durable high-water mark, so a healer outage cannot lose an incident. Both paths produce the
same incident key, so nothing is counted twice. `docs/triggers.md` compares this with a webhook
relay and explains why a direct webhook to GitHub does not work.

## Quick start

```bash
git clone https://github.com/kyle-chalmers/healwright
cd healwright && python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt

# see the whole loop run locally, with nothing sent anywhere
python templates/healer/healer.py --local --mode simulate --classification CODE_BUG --store sqlite:///tmp/hw.db
python templates/healer/healer.py --local --mode simulate --classification CODE_BUG --store sqlite:///tmp/hw.db   # -> done (duplicate)
python templates/healer/healer.py --local --mode simulate --success --run-id 101 --store sqlite:///tmp/hw.db      # -> streak reset

# see what the installer would do to a job, offline
python tools/install_trigger.py --job-json tests/fixtures/job_before.json --healer-job-id 999
```

Then, in your jobs repo, run the skills in order, or follow them by hand:

1. Copy `templates/healer/` to `jobs/self_healing/` and `tools/install_trigger.py` to `tools/`.
2. Copy `config.example.yaml` to `config.yaml`; fill in the repo, source root, state schema, and the
   `${secret:scope/key}` references. No secret values ever go in the file.
3. Create the healer job from `healer_job.json` (a small node or serverless, run as a service principal).
4. Copy `examples/caller-workflow.yml` into `.github/workflows/`, set the bot login and three secrets.
5. `python tools/install_trigger.py --profile <p> --healer-job-id <id> --job-id <a> --job-id <b>` (dry run), review, then `--apply`.
6. Start with `policy.shadow: true`, watch incidents land in the state tables, then switch it off.

Install the skills with the plugin marketplace (`/plugin marketplace add kyle-chalmers/healwright`)
or copy `skills/` into your `.claude/skills/`. They are Markdown; nothing about them is required.

## What keeps you safe

- **The healer never modifies a job.** It reads runs, writes its own state tables, posts, and opens issues.
- **Incidents are claimed, not checked.** One owner per run, one issue slot per job, leases that
  expire if a healer dies, and a receipt for every external action. Two healers cannot double-post.
- **Redaction before anything leaves.** Token shapes, keys, emails, connection strings, id
  numbers, high-entropy strings. Raw traces are opt-in.
- **The AI cannot push, or write anything on GitHub.** In `fix.yml` a gate job snapshots the
  allowed folder from the issue before the model runs. The model then runs in a job whose token
  is read-only on every scope; its edits leave as a patch artifact. A third job on a fresh runner
  validates every path in the patch against the snapshot, applies it, and only then commits,
  pushes, and opens a **draft** PR with a write-scoped token. One run per issue at a time. Every
  outcome, including "no fix", comments on the issue.
- **The installer never clobbers.** Dry run by default, live definition fetched and fingerprinted,
  only the task list sent, uninstall via `fields_to_remove`, per-job backup before any write.
- **No organisation values in this repo.** `bin/leak_scan.py` fails CI on the shapes of them.

Details, with the reasoning: `docs/guardrails.md`.

## Repo map

```
templates/healer/      copy this: healwright_core.py, healer.py, propagate.py, patterns.yaml, config.example.yaml, healer_job.json
tools/install_trigger.py   add/remove the leaf task on live jobs (dry-run default)
.github/workflows/fix.yml  reusable fix workflow; examples/caller-workflow.yml is the 15-line caller
skills/                three Claude Code skills, one per methodology step
docs/                  architecture, triggers, guardrails, lessons, live-validation-checklist, migrating-from-a-polling-monitor
tests/                 pytest over the template, the installer, and the workflow's structure
bin/selftest.sh        the gate
```

## Status

**v0.1.0.** Local tests green (classification goldens, claim/lease races, installer diffs,
workflow structure). Databricks only.

Not yet validated in a live workspace, and listed as open in `docs/live-validation-checklist.md`:
how `AT_LEAST_ONE_FAILED` behaves on a cluster-launch failure, a job timeout, and a whole-run
cancel; whether a Run Job task mirrors its child's result; whether the opt-in propagate task
restores `FAILED`; Delta state backend under two concurrent writers. Run shadow mode beside any
existing monitor before trusting it.

Hand-off and open verification items: `docs/status.md`.

Backlog: webhook relay code (documented in `docs/triggers.md`), Snowflake state backend, Airflow
and Dagster adapters, weekly recap, `tools/audit_jobs.py`.

## License

MIT. Copyright (c) 2026 Kyle Chalmers.
