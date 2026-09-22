# Status and hand-off (as of 2026-09-21)

Written by the session that built v0.1.0 so the next person (or agent) starts from facts. Read
this before touching anything. Sections: what is done, what is verified, what is NOT verified and
exactly how to verify it, known gaps by file, and the backlog.

## What exists

| Piece | Path | State |
|---|---|---|
| Methodology | `README.md`, `docs/architecture.md`, `docs/triggers.md`, `docs/guardrails.md`, `docs/lessons.md`, `docs/migrating-from-a-polling-monitor.md` | complete for v0.1 |
| Copyable healer | `templates/healer/` (`healwright_core.py`, `healer.py`, `propagate.py`, `patterns.yaml`, `config.example.yaml`, `healer_job.json`) | complete; SQLite tested, Delta implemented but never run |
| Installer | `tools/install_trigger.py` | complete; offline mode tested, live mode never run against a workspace |
| Reusable fix workflow | `.github/workflows/fix.yml` + `examples/caller-workflow.yml` | complete; parses on GitHub; **never executed end to end** |
| Skills | `skills/self-healing-{audit,build,install-trigger}/SKILL.md` + `.claude-plugin/` | complete; never validated with `claude plugin validate` |
| Gate | `bin/selftest.sh`, `bin/leak_scan.py`, `.github/workflows/ci.yml` | green on `main` |
| Tests | `tests/` (100 cases) | green |

## Verified

- `bash bin/selftest.sh`: ruff, 100 pytest cases, standalone template import, offline installer
  dry run, shape-based leak scan, skill surface, zizmor (every action SHA-pinned).
- Local end-to-end via `templates/healer/healer.py --local --mode simulate`: CODE_BUG produces
  one alert and one issue; a second delivery of the same run reports `done`; a newer success
  posts RESOLVED and resets the streak; TRANSIENT alerts without an issue.
- Two Codex plan reviews and five read-only Codex code reviews. Every P0/P1 was verified against
  the code and either fixed or accepted with a written reason (`CHANGELOG.md`, `docs/guardrails.md`).
  A sixth pass was started against the final tree and had not reported when this was written.
- An independent Sonnet audit and a private literal denylist scan found no organisation- or
  financial-services-specific content. Maintainer identity is intentionally public.
- GitHub accepts `fix.yml` (an earlier revision was rejected for `runner.temp` in job-level `env`;
  the fix is a first step per job that writes the scratch dir to `GITHUB_ENV`).
- CI green on `main`.

## NOT verified, and how to verify each

### 1. `fix.yml` end to end (highest priority; nothing about it has run)

Do this in a **throwaway** repo with a **throwaway** GitHub App. Never validate against a real
jobs repo first.

1. Create a repo with `jobs/example_job/main.py` containing a one-line bug (`df["customer_id"]`
   on a frame without that column), plus `AGENTS.md`.
2. Create a GitHub App: Contents R/W, Issues R/W, Pull requests R/W, Metadata R, **Workflows
   denied**; install it on that repo only. Store `HEALWRIGHT_APP_ID`, `HEALWRIGHT_APP_PRIVATE_KEY`,
   and `CLAUDE_CODE_OAUTH_TOKEN` (or `ANTHROPIC_API_KEY`) as repo secrets. Create labels
   `self-healing-fix` and `needs-human`.
3. Copy `examples/caller-workflow.yml` to `.github/workflows/self-healing-fix.yml`; set
   `bot_login` to the App's exact login (`<app-slug>[bot]`).
4. As the App (or temporarily as yourself, then set `bot_login` to your login), open an issue in
   the healer's format. Minimum body:
   ```
   | Field | Value |
   |-------|-------|
   | Notebook Path | `/jobs/example_job/main` |
   ```
   plus an "Error Output" section with the KeyError. Add the `self-healing-fix` label.
5. Expect: `gate` passes; `investigate` runs the model and uploads `healwright-<issue>-<run>`
   artifact containing `changes.patch`, `summary.md`, `issue.md`, `history.txt`; `publish`
   opens a DRAFT PR titled `fix: jobs/example_job (healwright #N)` and comments on the issue.
6. Negative test: ask (via a second issue whose body points at a folder that exists) for a change
   the model will make outside the folder, or hand-craft a patch artifact. Expect `publish` to
   discard it, comment, add `needs-human`, and fail the run.
7. Things most likely to break, in order:
   - **Claude Code permission-rule syntax** in `--allowed-tools`: `Edit(<folder>**)` and
     `Edit(//<absolute path>)`. If the syntax is wrong the model either cannot edit (it will say so
     in `summary.md`) or edits unscoped (still confined by `publish`). Check the current Claude
     Code permissions docs and adjust `fix.yml` line with `--allowed-tools`.
   - `anthropics/claude-code-action` behaviour with a token that is **read-only on every scope**.
     If it hard-fails before the model runs, grant `issues: read` only (already) and see whether
     `pull-requests: read` is required; do not grant any write scope in `investigate`.
   - The action may try to post a tracking comment and fail (harmless) or refuse to start.
   - `shell: python -I {0}` requires Python on the runner; `actions/setup-python` is in every job.
8. When it works, record the run URL and the observed behaviour here.

### 2. Live Databricks matrix (`docs/live-validation-checklist.md`)

Nothing in the trigger design has been exercised in a workspace. Items 1 to 3 and 14 to 17 in
that checklist are the minimum before switching `policy.shadow` off anywhere. The open
semantics are listed in `docs/triggers.md` under "still to confirm live".

### 3. Delta state backend

`DeltaStore` mirrors `SqliteStore` with MERGE/UPDATE and owner-token read-back, but has never
executed. First run will surface Spark SQL syntax issues (quoting, MERGE with `INSERT *`,
boolean literals). Test procedure: run `healer.py` in a notebook with `state.backend: delta`
and `mode=reconcile` in shadow mode; then fail two throwaway jobs simultaneously with
`max_concurrent_runs` > 1 on the healer and confirm one `incidents` row per run and no duplicate
`action_log` rows.

### 4. Installer live path

`--job-id` mode has only been exercised through fakes. First live use: `--dry-run` (default) on
one throwaway job, read the diff and the WARN lines, then `--apply`. Confirm the leaf task
appears in the UI with `depends_on` = every task and `run_if` = At least one failed. Then
`--uninstall --apply` and confirm the task is gone (`fields_to_remove`).

### 5. Plugin manifest

Run `claude plugin validate .claude-plugin/marketplace.json --strict` and
`claude plugin validate .claude-plugin/plugin.json --strict` (pinned Claude Code CLI). Not yet
done; the skills are plain Markdown and work by copy regardless.

## Known gaps by file

- `templates/healer/healwright_core.py`
  - `DeltaStore`: untested (above). Concurrency relies on Delta optimistic transactions plus
    owner-token read-back; no live evidence.
  - `DatabricksPlatform._state_of`: decodes legacy `state` and 2.2 `status`; the mapping of
    `termination_details.code` to result states is best effort (`_CODE_TO_RESULT`).
  - `list_completed_runs` uses `expand_tasks=True`, which returns at most 100 tasks per run.
    Jobs bigger than that need `get_run` per run.
  - `redact()` is shape-based and will over-redact occasionally (by design).
- `tools/install_trigger.py`
  - Run Job nesting depth cannot be measured from a job's own definition; it is a warning only.
  - The window between re-fetch and `jobs.update` has no compare-and-swap; documented.
- `.github/workflows/fix.yml`
  - Never executed (above).
  - Summary redaction in `publish` is a compact copy of the shapes in `healwright_core.redact`;
    keep them in sync if you change one.
- `bin/leak_scan.py`
  - Shape-based only. The literal denylist lives outside the repo; keep it that way. The
    ticket-key shape also matches dated filenames like `X-2026-09-21`, so avoid that pattern in
    repo paths.

## Backlog (v0.2)

- Webhook relay as code (`examples/relay/receiver.py` is a sketch; needs TLS story and deployment recipe).
- Snowflake state backend (column-compatible with the polling monitor's tables; migration DDL).
- `tools/audit_jobs.py` (the audit skill is a checklist today).
- Airflow / Dagster platform adapters (protocol seam exists: `Platform`).
- Weekly recap message.
- `--grant` in the installer (report-only today, by design).

## How to re-run the gates

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
bash bin/selftest.sh
HEALWRIGHT_LEAK_DENYLIST=~/.config/healwright-denylist.txt bash bin/selftest.sh   # private denylist, never committed
```

Before any release: an independent read-only review (for example
`codex exec --sandbox read-only --skip-git-repo-check "<review prompt>" < /dev/null`) and a
second reviewer looking only for organisation or domain leakage.
