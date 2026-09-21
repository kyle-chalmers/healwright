# Guardrails

Each of these exists because its absence caused an incident in the system this was generalized
from, or because a reviewer found the hole before it did.

## The healer

| Guardrail | Mechanism | Why |
|---|---|---|
| Observe-only | No code path calls run-now, cancel, pause, or job update | A monitor that retries a non-idempotent job duplicates a delivery |
| Never monitors itself | `jobs.exclude_healer`, `healer_job_id`, sentinel task keys | Self-alerting loops |
| One owner per incident | `claim_incident` with a lease and an owner token; `busy`/`done`/`resumed`; `complete` and `release` are conditional on the token, so an expired worker cannot close or reopen a lease another worker took over | Two healers (trigger + reconciler) on one run |
| Crash-safe | `resumed` after lease expiry; receipts written **after** the post; a failed action releases the incident for retry; incident marker in every post | A crash between insert and alert used to lose the alert forever |
| One issue per job per streak | `claim_issue_slot` token + lease, `set_issue` conditional on the token, tracked issue re-checked with a direct read, marker search as a net | Two runs in one window produced two issues seconds apart |
| Streaks cannot regress | success resets only if newer than the last failure; a stale failure does not increment; increments are relative (`+ 1`) so two concurrent incidents of one job both count | Late reconciler events out of order |
| RESOLVED posts, then resets | a failed RESOLVED post is logged and the streak still resets: state must reflect reality even when the channel is down (the next failure starts a fresh thread) | A monitor that refused to reset on a Slack error stayed "failing" forever |
| Alert dedup | same category + pattern within `dedup_window_hours` → suppressed (decided from pre-state) | Channel noise on every retry cycle |
| Flood cap | `max_alerts_per_run` then one summary | An infrastructure incident should not page per job |
| Issue cap | `max_issues_per_hour` from receipts | A pattern regression should not open fifty issues |
| Redaction | `redact()` on every trace **and on every platform-controlled field** (job name, task key, path, pattern, title); Markdown and table syntax neutralised; raw traces opt-in; issue body is a field allowlist | Traces carry tokens, emails, connection strings; a job name can too; issue bodies are permanent |
| Eligibility for AI fixes | notebook path must map to one folder under `source_root`; folder must exist if a checkout is given | The fix pipeline is scoped to one folder; anything else is a human's problem |
| UTC everywhere | ISO-8601 UTC strings in every store | A local-time column next to a UTC one hid a race for weeks |

## The installer

| Guardrail | Mechanism |
|---|---|
| Dry run by default | `--apply` required; confirmation unless `--yes`; refuses `--apply` without `--yes` in a non-TTY |
| Live definition only | fetch → fingerprint → patch → re-fetch → compare → update; abort on drift. Repo JSONs are not authoritative. |
| Minimal write | `jobs.update(new_settings={"tasks": ...})`; uninstall via `fields_to_remove: ["tasks/__healwright", ...]`; never a backup restore |
| Backup first | pre-change definition written to `.healwright-backups/` |
| Preflight | refuses the healer itself, jobs with no tasks, jobs already calling the healer; warns on existing `run_if` handlers and Run Job nesting (callers are invisible from a job's own definition, so depth cannot be measured) |
| Reinstall removes what it drops | a sentinel task present live but absent from the new definition is named in `fields_to_remove` in the same update (Jobs `update` merges task lists by key) |
| Permissions reported, never granted | run-as identity (or the creator, for jobs without `run_as`) must hold `CAN_MANAGE_RUN` on the healer job; unverifiable means blocked unless `--skip-permission-check`; the tool tells you, an admin grants |

The remaining window between re-fetch and update is seconds wide and has no API compare-and-swap.
It is documented here rather than pretended away.

## The fix workflow

| Guardrail | Mechanism |
|---|---|
| Only the healer's issues | author login must equal `bot_login`; the fix label must be present |
| Strict folder | derived from the issue's `Notebook Path` row, validated against `^[A-Za-z0-9][A-Za-z0-9._-]*$` and existence in the checkout |
| Three jobs, one boundary | `gate` reads the issue **before** the AI runs and snapshots the allowed folder as an immutable job output. `investigate` runs the AI with an App token that is read-only on every scope, so nothing in that job, including the action's own post-steps under a poisoned environment, can write to GitHub; the only thing that leaves is a patch artifact. `publish` is a fresh runner and a fresh checkout: it takes the folder from `gate`, validates every path the patch names (headers, renames, copies; realpath containment; no `.github/`, `.claude/`, `.git/`, `AGENTS.md`, `CLAUDE.md`), `git apply --check`s it, applies it, re-checks what actually changed on disk, and only then mints the write token. Runner state the AI could poison (`GITHUB_ENV`, `PATH`, hooks, `.git/config`, files) dies with the `investigate` runner |
| Pinned actions | Every action, including `anthropics/claude-code-action`, is pinned to a full commit SHA; `tests/test_workflow.py` and `zizmor` enforce it |
| Self-hosted runners | Each job sets up Python; the trusted steps run as `python -I` and need nothing else on the runner beyond `git` and `gh` |
| AI cannot push | No `gh`, no `git push`, no `Write`, `cat`, `tee`, `echo`, `find`, or `python` in the tool allowlist; `Do NOT push` in the prompt; and, structurally, no credential in that job that could push |
| Nothing after the AI step trusts the environment | Post-AI steps in `investigate` run as `python -I` (isolated: ignores `PYTHONPATH`) and hold no secret. Trusted git in `publish` runs with hooks, fsmonitor and credential helpers disabled |
| Scope validated mechanically | every changed path realpath-checked against the folder; workflow files, `AGENTS.md`, `CLAUDE.md` always forbidden; violations discard the tree and fail the run |
| Trusted push | a separate write-scoped token commits, pushes, and opens a **draft** PR |
| Always a comment | no PR for any reason → comment with the agent's summary + `needs-human` label |
| One run per issue | `concurrency: healwright-fix-<issue>` without cancel |
| Turn budget | `--max-turns`, and a decide-by turn in the prompt; the summary file is the deliverable |
| No untrusted context in shell | inputs and issue fields reach `run:` steps through `env:`, never inlined |

## The propagate task: cost and choice

Without it, a job with a real failure ends `SUCCESS_WITH_FAILURES` once `__healwright` succeeds.
Job-level `on_failure` webhooks (the ones already pointed at a Slack channel) stop firing for
that job. The healer's own alert replaces them, classified and threaded, but it is a change.

With it, a second task runs after any failure and raises, so the run is `FAILED` and every
existing notification behaves as before. It costs compute: on serverless a few seconds; on a job
cluster it keeps the cluster alive until it runs, or launches one just to raise. That is why it is
opt-in and why `propagate.compute` has no default: the team decides where it runs, knowing the bill.

## What this does not protect against

- A wrong classification pattern. Review UNKNOWN weekly and add patterns with golden tests.
- A compromised GitHub App private key. Rotate; the App's `Workflows: denied` permission still
  stops the AI from editing CI.
- A platform semantic we assumed. The live checklist exists for that.
