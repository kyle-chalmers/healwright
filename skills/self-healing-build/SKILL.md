---
name: self-healing-build
description: Step 2 of the healwright methodology. Copy the reference healer into this jobs repo, configure it (state backend, Slack, GitHub App, monitored jobs), prove it locally with a simulated failure, and prepare the shared healer job and the fix-workflow caller. Use after /self-healing-audit, or when a team wants their own self-healing job.
argument-hint: "[target-folder e.g. jobs/self_healing]"
allowed-tools: [Bash, Read, Grep, Glob, Write, Edit]
---

# self-healing-build

You are building the team's own healer from the healwright reference, not installing a library.
Everything you create is theirs to read and change.

## Steps

1. **Get the reference.** If the healwright repo is not checked out nearby, clone it read-only:
   `git clone --depth 1 https://github.com/kyle-chalmers/healwright /tmp/healwright`.
2. **Copy `templates/healer/` into the repo** at the target folder (default `jobs/self_healing/`,
   i.e. one folder under the source root so it looks like any other job). Copy `tools/install_trigger.py`
   into `tools/`. Keep the file header comments; they are the docs future readers get.
3. **Create `config.yaml` from `config.example.yaml`.** Detect, then confirm with the user:
   - `github.repo` from `git remote get-url origin` (owner/name only).
   - `github.source_root`: the folder that holds job source.
   - `jobs.include` / `jobs.exclude` globs from the audit's candidate list.
   - `state.backend`: `delta` and a `catalog.schema` the healer's run-as identity may create tables in.
   - Slack channel ID and the secret-scope names for the bot token and the GitHub App credentials.
   Never write a secret value into any file. Config holds `${secret:scope/key}` references only.
4. **Prove the core locally** before touching the workspace:
   ```
   python jobs/self_healing/healer.py --local --mode simulate --classification CODE_BUG --store sqlite:///tmp/hw.db
   python jobs/self_healing/healer.py --local --mode simulate --classification CODE_BUG --store sqlite:///tmp/hw.db
   ```
   The first run prints the Slack text and the issue body it would send; the second reports `done`
   (duplicate). Try `--classification TRANSIENT` (no issue) and `--success` (streak reset).
5. **Fill `healer_job.json`**: notebook path of `healer.py`, `git_source` url and branch, a small
   node type or serverless, the run-as service principal, and the hourly reconcile schedule.
   Ask before creating the job in the workspace; creating it is an outward action.
   After creation, write `healer_job_id` into `config.yaml`.
6. **Prepare the fix pipeline**: copy `examples/caller-workflow.yml` to
   `.github/workflows/self-healing-fix.yml`, set `bot_login` to the GitHub App's exact bot login
   and the three repo secrets (`HEALWRIGHT_APP_ID`, `HEALWRIGHT_APP_PRIVATE_KEY`,
   `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`). Create the labels `self-healing-fix` and
   `needs-human`. The GitHub App needs Contents R/W, Issues R/W, Pull requests R/W, and
   Workflows DENIED, installed on this repo only.
7. **Start in shadow mode** (`policy.shadow: true`): the healer writes state and posts nothing
   until the team has watched a few real incidents land in the state tables.

## Done when

The healer folder is in the repo, `config.yaml` has no secret values, the local simulation
round-trips, the healer job exists (or its JSON is ready and the user chose to create it later),
and the caller workflow is in place.

## Next

- `/self-healing-install-trigger` to add the leaf task to the candidate jobs (dry run first).
- Keep `docs/live-validation-checklist.md` from healwright open: the platform semantics it lists
  must be confirmed in your workspace before shadow mode is switched off.
