# Migrating from a polling monitor

You already have a scheduled job that lists recent runs, classifies failures, and alerts. This is
how to move to the trigger without a gap in coverage or a burst of duplicate alerts.

## 1. Map what you have

Write down, for your monitor: the state tables and their keys, the classification rules, the alert
channel and threading convention, the issue-creation rule, the schedule, and every consumer of
its output (dashboards, weekly summaries, humans who read the channel). Anything that reads the
old tables needs a plan before those tables stop being written.

## 2. Bring the rules over

Port your classification rules into `config.yaml` under `classification.patterns`. Keep the ones
that are specific to your domain there, not in `patterns.yaml`. Port termination codes the same
way. Add a golden test for each rule you care about.

## 3. Run both, healer in shadow

Deploy the healer job with `policy.shadow: true` and its own state tables. Install the leaf task
on a handful of jobs. For a week, compare: every failure your monitor alerted on should have an
incident row from the trigger path with the same classification, minutes earlier. Differences are
either a pattern to port or a bug to fix; both are cheap while nothing is posted.

## 4. Switch alerting, keep the old monitor as the reconciler

Point the healer at the channel, switch shadow off, and at the same time reduce the old monitor to
observe-only into its own tables, or retire it and rely on the healer's `mode=reconcile` schedule.
Two alerting systems at once is the one state to avoid.

## 5. Move state consumers

Dashboards and summaries read the `healwright_*` tables from now on. If you kept the old tables
for history, leave them frozen and say so in a table comment.

## 6. Retire the poll

When every consumer reads the new tables and a month of incidents shows the trigger path caught
everything the reconciler later saw, pause the old monitor's schedule. Keep its definition in
version control; the reconciler is now the safety net.

## Keep from the old system

The classification order and the incident-tested patterns. The idea that the monitor never
retries. Draft-only PRs. The rate limits. The channel conventions your team already reads.

## Leave behind

Lookback windows and their caps. Search-based duplicate checks. Local-time timestamps. Any place
the monitor and the monitored jobs shared a credential that a single expiry could take down
together; the healer's own credentials should be few, org-owned, and documented.
