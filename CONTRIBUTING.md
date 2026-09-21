# Contributing to healwright

The mission and the nine tiebreakers that decide ambiguous changes live in [`AGENTS.md`](AGENTS.md).
Read it first and cite it in review. Below are the mechanics.

## Dev setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
bash bin/selftest.sh
```

## Adding a classification pattern

1. Add the entry to `templates/healer/patterns.yaml` under the right category. Category order is
   load-bearing; read the comment at the top of the file before moving anything.
2. Add a golden case to `tests/test_classify.py` using a **synthetic** message or trace that has
   the same shape as the real one. Never paste a real trace: it carries hostnames, ids, and
   sometimes data.
3. If the pattern is specific to your organisation or domain, it belongs in your own
   `config.yaml` under `classification.patterns`, not here.

## Adding a state backend

Implement `StateStore` in `healwright_core.py`, then parametrise the store tests over it. The
contract includes the concurrency tests (one owner per incident, one winner per issue slot). A
backend that cannot pass them under two writers is documented as such in README Status.

## Adding a platform adapter

Implement the `Platform` protocol (`fetch_run`, `list_completed_runs`) so it yields `RunIncident`
and `RunSuccess`. Sentinel task keys must be excluded from `tasks`. The installer is
Databricks-specific today; a second platform needs its own trigger mechanism documented in
`docs/triggers.md` before code.

## Leak audit before a release

`bash bin/selftest.sh` scans shapes. Additionally, run the scanner with a private literal denylist
that never enters the repo:

```bash
HEALWRIGHT_LEAK_DENYLIST=~/.config/healwright-denylist.txt bash bin/selftest.sh
```

Then have a second, independent reviewer read the diff specifically for organisation names,
hostnames, channel ids, ticket keys, job names, and domain vocabulary.

## Provenance

healwright was generalized from a production self-healing system for a fleet of scheduled data
jobs: a polling monitor, a two-stage classifier, a state store with streak tracking, threaded
alerts, and a guarded AI fix pipeline. Every rule here was earned against real failures before it
was lifted out, and stripped of all organisation-specific values on the way. The polling monitor
became the reconciler; the trigger path is new.
