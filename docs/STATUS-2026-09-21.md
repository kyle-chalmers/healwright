# Status and hand-off (2026-09-21)

What is done, what is verified, and what a maintainer should do next. Written by the session that
built v0.1.0 so the next one starts from facts, not memory.

## Verified

- `bash bin/selftest.sh` green: ruff, 100 pytest cases, standalone template import, offline
  installer dry run, shape-based leak scan, skill surface, zizmor (all actions SHA-pinned).
- Local end-to-end: `templates/healer/healer.py --local --mode simulate` round-trips CODE_BUG
  (alert + issue), duplicate (`done`), success (RESOLVED + streak reset), TRANSIENT (alert only).
- Five read-only Codex reviews of the code plus two of the plan; every P0/P1 verified and either
  fixed or accepted with a documented reason (`CHANGELOG.md`, `docs/guardrails.md`). An
  independent Sonnet audit and a private literal denylist scan found no organisation or
  financial-services content.
- CI on `main` green.

## Not yet verified (do these before relying on the fix workflow)

1. **`fix.yml` has never executed against a real issue.** It was corrected once after GitHub
   rejected `runner.temp` in job-level `env`; the corrected file passes GitHub's parser (check the
   Actions tab for a red "workflow file issue" run after any push that touches it). First real
   run: use a throwaway repo, a throwaway GitHub App, and a hand-written issue in the healer's
   format. Confirm: gate passes, the model edits only inside the folder, the patch artifact
   uploads, `publish` opens a DRAFT PR, and a deliberate out-of-folder edit is discarded and
   commented.
2. **Claude Code permission-rule syntax** in the allowlist: `Edit(<folder>**)` and
   `Edit(//<abs path>)`. Verify against the current Claude Code docs; if the path form differs,
   the model either cannot edit at all (safe) or edits unscoped (the publish job still confines
   what gets committed).
3. **The Databricks live matrix** in `docs/live-validation-checklist.md`, especially whether
   `AT_LEAST_ONE_FAILED` fires for cluster-launch failures, timeouts, and cancels, and whether the
   opt-in propagate task restores `FAILED`.
4. **Delta store under two concurrent writers.** SQLite is the tested reference.
5. A sixth Codex pass was started against the final tree but had not reported when this note was
   written. Re-run one with the prompt "verify the four P1s from the previous pass are addressed
   and look for new P0/P1 in fix.yml" before announcing the repo.

## Backlog (v0.2)

Webhook relay code (`examples/relay/` is a sketch), Snowflake state backend, `tools/audit_jobs.py`,
Airflow/Dagster adapters, weekly recap, `--grant` in the installer.

## How to re-run the gates

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
bash bin/selftest.sh
HEALWRIGHT_LEAK_DENYLIST=~/.config/healwright-denylist.txt bash bin/selftest.sh   # private denylist, never committed
```
