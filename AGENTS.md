# healwright: agent and contributor rules

Source of truth for how this repo is worked on. `.claude/CLAUDE.md` is a stub that points here.

## Mission and vision: the tiebreakers

**Mission.** A failed data job heals itself or tells a human exactly why it could not, seconds
after it fails, without anyone polling for it.

**Vision.** Any team can take this repo, copy one folder into their jobs repo, point one workflow
at ours, and have a self-healing loop they fully own and can read end to end.

When a change is ambiguous, these decide it:

1. **Trigger first, poll last.** The in-job leaf task is the primary path. The reconciler exists
   to catch what the trigger missed, never to replace it. A change that makes polling load-bearing
   again is a regression.
2. **The healer observes; it never retries, pauses, cancels, or edits a job.** Retries are the
   platform's `retry_policy`. Fixes are draft PRs a human merges. There is no auto-merge.
3. **Copyable beats installable.** `templates/healer/` is one file of logic plus config and
   patterns, stdlib plus the platform SDK. No pip package, no framework the team must adopt.
   If a change needs a new dependency inside the template, it needs a very good reason.
4. **Redact before you post.** Nothing leaves the healer (Slack, GitHub, logs) without `redact()`.
   Raw traces are opt-in. A public repo issue body is the threat model.
5. **Claim, don't check.** Anything two healers could do twice (an incident, an issue for a job)
   is claimed atomically in the store with a lease, and every external action leaves a receipt.
6. **The AI cannot push.** In `fix.yml`, the model edits a working tree under a read-only token.
   A trusted step validates the diff against one folder and does the git work. Keep those two
   steps separate.
7. **No organisation-specific values anywhere.** Not in code, patterns, fixtures, docs, or tests.
   Fixtures are synthetic. `bin/leak_scan.py` enforces shapes; the private denylist stays outside
   the repo.
8. **Say what is verified live and what is not.** README Status and
   `docs/live-validation-checklist.md` are honest. Platform semantics we could not verify from
   docs are listed as open, not assumed.
9. **Every skill ends by naming the next one** (`## Next`). `bin/selftest.sh` checks.

## The gate

`bash bin/selftest.sh` is THE gate: ruff, pytest, the template's standalone import, an offline
installer dry run, the shape-based leak scan, the skill surface, and zizmor when installed. Run it
before every commit; CI runs it on Python 3.10 to 3.12.

## Ground rules for changes

- Patterns change in `templates/healer/patterns.yaml`, with a golden test in
  `tests/test_classify.py` built from a **synthetic** trace of the same shape.
- The store contract lives in `StateStore`; SQLite is the tested reference. Any new backend gets
  the same contract tests. The Delta backend is implemented but not concurrency-tested live.
- `fix.yml` changes must keep `tests/test_workflow.py` green: read-only token on the AI step,
  scope validation before any push, concurrency per issue, no `gh` or `git push` in the allowlist.
- Every CHANGELOG entry names the adopter pain it fixes.
- Secrets never in the repo. Config holds `${secret:scope/key}` and `${env:NAME}` references only.

## Where things live

- `templates/healer/healwright_core.py`: models, classifier, redaction, stores, clients, `Healer`.
- `templates/healer/healer.py`: notebook and local CLI entrypoint. `propagate.py`: the opt-in raise task.
- `tools/install_trigger.py`: adds/removes the leaf task on live jobs (dry-run default).
- `.github/workflows/fix.yml`: the reusable fix workflow; `examples/caller-workflow.yml` shows the 15-line caller.
- `skills/`: the three-step methodology as Claude Code skills. `docs/`: architecture, triggers, guardrails, lessons, live checklist, migration.
