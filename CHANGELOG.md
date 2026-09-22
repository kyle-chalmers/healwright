# Changelog

All notable changes to healwright. Each entry names the adopter pain it addresses.

## 0.1.0 (unreleased)

First public cut of the methodology and reference kit.

- **Trigger-based detection** (`tools/install_trigger.py`, `templates/healer/healer.py` mode
  `leaf-task`): a Run Job leaf task with `run_if: AT_LEAST_ONE_FAILED` that depends on every task
  calls one shared healer job the moment a run fails. Pain: polling monitors reported failures
  hours late and raced their own state across cycle boundaries.
- **Reconciler from a durable high-water mark** (mode `reconcile`): a low-frequency sweep with
  overlap catches anything the trigger missed. Pain: a wall-clock lookback cap silently dropped
  failures after long outages.
- **Claim/lease incidents and per-job issue slots** in the state store, with action receipts and
  incident markers in every Slack message and issue body. Pain: two runs of one job in one window
  produced duplicate issues; a crash between insert and alert lost the alert forever.
- **Redaction before anything is posted**, raw traces opt-in. Pain: error traces carry tokens,
  emails, and connection strings, and issue bodies in a shared repo are forever.
- **Reusable fix workflow** (`.github/workflows/fix.yml`) where the AI cannot push: it runs in a
  job with no write credential, its edits leave as a patch, and a clean second job validates the
  patch against one job folder before opening the draft PR; every outcome comments on the issue. Pain: a post-step scope check
  ran after the branch was already pushed, and the no-fix path went silent when the sandbox
  lacked `gh`.
- **Three skills** (`self-healing-audit`, `self-healing-build`, `self-healing-install-trigger`)
  that carry a team through the methodology. Pain: the knowledge lived in one org's runbook.

Reviewed before release by two independent read-only passes (a Codex code review and a separate
organisation-leak audit); the P0/P1 findings are folded in: the leaf trigger now builds incidents
from failed tasks while the parent run is still RUNNING, the Delta store claims with an owner
token and moves state only after winning, receipts are written after posts and a failed action
releases the incident for retry, every platform-controlled field is redacted, and the fix
workflow is split into `gate` (folder snapshot from the issue, before the AI), `investigate`
(AI under a token that is read-only on every scope, patch artifact out) and `publish` (fresh
runner, patch paths validated against the snapshot and applied, then push), because runner state
a model can write during its step (`GITHUB_ENV`, `PATH`, git hooks) is applied to every later
step in the same job, including the action's own, and a mutable issue body cannot be the authority
for scope once the AI has run. All actions are SHA-pinned; the model's `Edit` is path-scoped and
it receives the issue and git history as files instead of tools; its summary is redacted and
fenced before publication; the caller grants the read permissions the gate needs. A sixth pass
then removed the model's remaining shell and web tools entirely (`rg --pre`, `pip --log` and
`WebFetch` were escape routes), moved the scratch files inside the checkout so the `Edit` rule
needs no runner path, and made the summary artifact redacted and one-day. Lease owner
tokens guard completion and release; the installer checks the job's effective run-as identity
and blocks when the healer's permissions cannot be read.

Known limits in this release: Databricks only; Delta state backend implemented but not
concurrency-tested live; webhook relay documented, not shipped; the platform semantics in
`docs/live-validation-checklist.md` are open items until a workspace confirms them.
