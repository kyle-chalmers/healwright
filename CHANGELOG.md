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
- **Skills that explain themselves and find where you are** (adapted from the StreamSnow plugin's
  onboarding): each skill opens with a narration contract (what and why before each action, the
  result after, every question names what it decides), starts by detecting what already exists
  and skipping it; build and install-trigger list the steps only a human can do, with the reason. The audit now stops
  for the human to confirm every REVIEW and side-effecting job before `JOB_AUDIT.md` is written.
  Skill descriptions now match the README's step numbers (1, 3a, 3b), and
  `self-healing-install-trigger` can write the checklist answers it asks for and applies with
  `--apply --yes` after an explicit yes in chat (the installer's prompt needs a terminal the agent
  does not have). Pain: a newcomer
  could not tell which step they were on, what a skill was about to do, or which of its
  decisions were theirs, and the riskiest call (is this job safe to retry?) reached the next
  step without a human reading it.
- **Glossary, troubleshooting, and a worked example** (`docs/glossary.md`,
  `docs/troubleshooting.md`, `examples/JOB_AUDIT.example.md`), plus expected output for the
  quick start, a "Is this for you?" list, a prerequisites table, and project-scope install
  commands in the README. Pain: the docs used a dozen terms nowhere defined, and the healer's own
  `notes` and the installer's WARN/ERROR lines had no lookup from message to cause to fix.
- **Staged live validation** (`docs/live-validation-checklist.md`): stage A in shadow mode,
  stage B a rehearsal where a second healer job posts for the throwaway test jobs only, stage C
  the sign-off. Pain: the sign-off required a RESOLVED post, an issue body and the fix pipeline
  to be green before shadow mode was off, which shadow mode makes impossible, so a team either
  went live unvalidated or could never sign off. Also: step 2 (native retries) now has a how-to
  in the README, and `/self-healing-build` checks the tiers landed before it starts. Pain: the
  one step without a skill said only "your deploy path", and nothing caught a skipped retry tier.
- **Skill-surface tests and plugin validation in CI** (`tests/test_skills.py`, `plugin-validate`
  job with a negative control). Pain: the gate only checked that each skill ended in `## Next`,
  so a broken frontmatter, a dead doc pointer, or a skill losing its human checkpoint would ship
  green.

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
