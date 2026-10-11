---
name: self-healing-build
description: Step 3a of the four-step healwright methodology. Copy the reference healer into this jobs repo, configure it (state backend, Slack, GitHub App, monitored jobs), prove it locally with a simulated failure, and prepare the shared healer job and the fix-workflow caller. Detects what is already built and does only what is missing. Use after /self-healing-audit, or when a team wants their own self-healing job.
argument-hint: "[target-folder e.g. jobs/self_healing]"
allowed-tools: [Bash, Read, Grep, Glob, Write, Edit]
---

# self-healing-build

You are building the team's own healer from the healwright reference, not installing a library.
Everything you create is theirs to read and change. Paths like `docs/glossary.md` are in the
healwright repo, which step 1 clones: terms are defined in `docs/glossary.md`, and known failures
and their fixes are in `docs/troubleshooting.md`.

## Working with the user

- Open with one line: this copies files into their repo, runs a local simulation that sends
  nothing, and asks before anything touches the workspace.
- Before each step, one line of what and why; after it, one line with the result.
- Every question says what it decides and what each answer changes. A detected value is the
  recommended answer, never the confirmed one.
- Creating the healer job is an outward action: always ask first, and accept "later".
- Never ask for, print, or write a secret value. Secrets go into the secret store by a human.

### Only you can do these

Name these to the user up front, with the reason, so nothing surprises them at step 6:

- **Create the GitHub App** and install it on this repo only: it needs an org owner, and its
  private key must never pass through this session.
- **Set the repo secrets and the workspace secret-scope values**: secret values are typed by a
  human into the secret store, never into a file or a chat.
- **Create or choose the Slack bot and channel** (optional): it is a workspace admin decision.
  No Slack yet is fine: leave `slack.enabled: false` and tell `/self-healing-install-trigger`, so
  it can add the propagate task to keep each job's own failure notifications firing (confirm
  that live with checklist items 10 and 11).
- **No GitHub App yet** is fine too: leave `github.enabled: false` and skip step 6 until it
  exists. Detection, state and Slack alerts still work.
- **Pick the run-as service principal** for the healer job: identity is a team policy call.

## Steps

0. **Find where you are.** Need `JOB_AUDIT.md`; if it is missing, say so and route to
   `/self-healing-audit` first. Check that step 2 landed: for each candidate, read the live
   definition (`databricks jobs get <job-id>`, read-only) and compare each task's `max_retries`
   with the job's tier. List any that differ and ask whether to continue anyway; a retry tier
   left at 2 on a side-effecting job is the failure the audit exists to prevent. Then check, and report in one line, which of these exist: the
   target folder (default `jobs/self_healing/`), its `config.yaml`, a `healer_job_id` in it,
   `.github/workflows/self-healing-fix.yml`. Skip each step below whose output is already there,
   and say that you skipped it.
1. **Get the reference.** If the healwright repo is not checked out nearby, clone it read-only:
   `git clone --depth 1 https://github.com/kyle-chalmers/healwright /tmp/healwright`.
2. **Copy `templates/healer/` into the repo** at the target folder (default `jobs/self_healing/`,
   i.e. one folder under the source root so it looks like any other job). Copy `tools/install_trigger.py`
   into `tools/`. Keep the file header comments; they are the docs future readers get.
3. **Create `config.yaml` from `config.example.yaml`.** Detect, then confirm with the user, one
   setting at a time with a one-line explanation of what it controls:
   - `github.repo` from `git remote get-url origin` (owner/name only).
   - `github.source_root`: the folder that holds job source. Only jobs under it can get AI fixes.
   - `jobs.include` / `jobs.exclude` globs from the audit's candidate list.
   - `state.backend: delta` and `state.delta_schema`: a `catalog.schema` the healer's run-as
     identity may create tables in.
   - Slack channel ID and the secret-scope names for the bot token and the GitHub App credentials.
   Never write a secret value into any file. Config holds `${secret:scope/key}` references only.
4. **Prove the core locally** before touching the workspace. Tell the user nothing is sent. It
   needs Python 3.10+ and `pyyaml` (`pip install pyyaml`):
   ```
   python jobs/self_healing/healer.py --local --mode simulate --classification CODE_BUG --store sqlite:///tmp/hw.db
   python jobs/self_healing/healer.py --local --mode simulate --classification CODE_BUG --store sqlite:///tmp/hw.db
   ```
   What they should see: the first run prints `new: example_job run 100 -> CODE_BUG/python_error
   (streak 1)` and a `would_send` list holding the Slack text and the issue body; the second
   reports `"outcome": "done"` with `"notes": ["already handled"]` (the duplicate guard). Try
   `--classification TRANSIENT` (no issue) and `--success --run-id 101` (streak reset, a RESOLVED
   message in the same thread). Show the user the Slack text: it is what their channel will get.
5. **Fill `healer_job.json`**: notebook path of `healer.py`, `git_source` url and branch, a small
   node type or serverless, the run-as service principal, and the hourly reconcile schedule.
   Ask before creating the job in the workspace; creating it is an outward action.
   After creation, write `healer_job_id` into `config.yaml`.
6. **Prepare the fix pipeline**: copy `examples/caller-workflow.yml` to
   `.github/workflows/self-healing-fix.yml`, set `bot_login` to the GitHub App's exact bot login
   (its name followed by `[bot]`) and `source_root` to the same value as `github.source_root`. The
   caller passes `CLAUDE_CODE_OAUTH_TOKEN`; a team using an API key changes that line to
   `anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}`.
   Then hand the user this checklist (the "only you" items above): the three repo secrets
   (`HEALWRIGHT_APP_ID`, `HEALWRIGHT_APP_PRIVATE_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` or
   `ANTHROPIC_API_KEY`); the labels `self-healing-fix` and `needs-human`; a GitHub App with
   Contents R/W, Issues R/W, Pull requests R/W, and Workflows DENIED, installed on this repo only.
   Workflows is denied so even a leaked App key cannot edit CI.
7. **Start in shadow mode** (`policy.shadow: true`): the healer writes state and posts nothing
   until the team has watched a few real incidents land in the state tables.

## Done when

The healer folder is in the repo, `config.yaml` has no secret values, the local simulation
round-trips, the healer job exists (or its JSON is ready and the user chose to create it later),
the caller workflow is in place, and the user has the list of what only they can do.

## Next

- `/self-healing-install-trigger` (step 3b) to add the leaf task to the candidate jobs (dry run first).
- Keep `docs/live-validation-checklist.md` from healwright open: the platform semantics it lists
  must be confirmed in your workspace before shadow mode is switched off.
