# Lessons carried over

The system this kit was generalized from ran for months against a fleet of production jobs. These
are the incidents that shaped the design, with the identifying details removed. Each one maps to a
rule in `AGENTS.md` or a guardrail in `docs/guardrails.md`.

## Polling races

**A recovery that spanned a cycle boundary was invisible.** The monitor listed only completed runs
inside a lookback window anchored on the newest recorded event. A manual re-run that started
before one cycle and finished after it was never seen, so the job stayed "failing" with a stale
issue link until the next clean success. The fix at the time was to also anchor the window on the
oldest open failure. The real fix is an event per run: the leaf task fires when the run ends, and
the reconciler works from a watermark with overlap, not from "the newest thing I saw".

**Two failed runs of one job inside one window produced two issues one second apart.** The
scheduled run and a manual re-run both classified as code bugs; the duplicate guard was a GitHub
search, which is eventually consistent and could not see the issue created a second earlier.
Rule: dedup by state you own (the tracked issue, read back directly), and claim the per-job slot
atomically before you create. Search is a net, not a guard.

**A local-time column next to a UTC column masked the race above for weeks.** `created_at`
defaulted to the session time zone; `last_failure_time` was written from Python in UTC. Every
timestamp is now an ISO-8601 UTC string, in every backend.

## Classification

**A transient 503 from a spreadsheet API paged the team as a code bug.** Two bugs at once: the
"HTTP 503 service unavailable" regex was anchored and missed `[503]: The service is currently
unavailable.`, and the fallback matched `PermissionError` in a line of the library's own source
that the traceback quoted (`raise PermissionError from ex`). Fixes: loosen the transient pattern,
strip quoted `raise` lines before matching, and prefer the concise `error` field over the full
trace. The test for it lives in `tests/test_classify.py` with a synthetic trace of the same shape.

**A partner file that had not arrived yet looked like `FileNotFoundError`.** No code change would
have helped that run. UPSTREAM_DATA runs before CODE_BUG for the SFTP shape, and the alert says
"may resolve on the next scheduled run".

**Git-checkout failures contain the words "auth" and "credential".** CONFIG_ERROR runs before
CODE_BUG, or an expired platform git credential gets sent to the AI fix pipeline, which cannot
touch it.

## Trace truncation

**Head-only truncation dropped the exception.** Tracebacks put the exception at the bottom. A
Spark-wrapped Python trace truncated to its first N characters classified as UNKNOWN. Truncation
keeps a head (context) and a tail (the exception), with a marker between.

## Notebook paths

**Four jobs silently lost AI remediation to a stray space.** Their notebook path read
`jobs /Folder/notebook`; the platform resolved it fine, an exact `parts[0] == "jobs"` did not.
`source_folder()` strips whitespace per segment and the eligibility decision is logged.

## The fix pipeline

**Thirty percent of turns went to permission denials.** The tool allowlist was too tight; the
model reached for `grep`/`find`/`rg` naturally and got denied each time. Allow the read-only shell
tools you expect an investigator to use.

**Silent death at the turn limit.** Hitting `--max-turns` produced nothing: no comment, no PR.
The prompt now carries a decide-by turn and requires a summary file; the workflow comments on
every outcome from a trusted step, so the model's ability to post is not the deliverable's
dependency.

**The no-fix path could not comment.** The sandbox lacked `gh` and blocked direct API calls, so a
good diagnosis with no confident fix never reached the issue. Same fix: the trusted step posts.

**Two `labeled` events two seconds apart.** The second run had no issue context at all. One
concurrency group per issue, and the workflow reads the issue itself rather than trusting the
event payload.

**The scope gate ran after the push.** A post-step diff check cannot un-push a branch. The AI now
edits under a read-only token and a separate step validates before anything leaves the runner.

**A user-minted OAuth token died with the account and nobody noticed for two months.** Every fix
run failed with a 401 while detection and alerting kept working, so the pipeline being down was
invisible. The follow-up notifier (`run_followups`) is the counter: an issue that gets neither a
PR nor a `needs-human` label within a reasonable time is a pipeline health signal. Prefer an
org-owned GitHub App for everything the App can do; document who owns the one credential it
cannot replace.

## Deployment

**A batch deploy from repo JSON overwrote seven live job definitions.** Jobs had been edited in
the UI; the repo copies were stale; `jobs reset` is a full replacement. The installer only ever
patches a live definition it just fetched, fingerprints it, and sends only the task list.
