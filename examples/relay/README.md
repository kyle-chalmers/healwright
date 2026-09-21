# Webhook relay (sketch)

For platforms without an in-job trigger, or for teams who want a second, independent path, a
small HTTPS receiver can turn the platform's failure webhook into a healer invocation. This is a
sketch, not shipped code: `docs/triggers.md` (T2) lists the facts it must respect.

Contract:

1. Accept `POST /databricks` with HTTP Basic auth (the only auth Databricks destinations support).
   Compare credentials in constant time; reject anything else with 401 and no body.
2. Parse the fixed payload. For a task-level event, `run.run_id` is the **task** run and the job
   run is `run.parent_run_id`; normalise to the job run before doing anything.
3. Return 202 immediately, then enrich (Jobs API `get_run`), build a `RunIncident` with
   `source="webhook"`, and hand it to `Healer.handle`. The incident key dedupes redelivery.
4. Sit behind TLS with a trusted certificate (a reverse proxy or a managed function URL) and an
   IP allow-list for the platform's egress ranges.
5. Add it as a **second** destination on `on_failure`; never repoint the existing one.

`receiver.py` is the smallest working shape of steps 1 to 3 using the standard library. It has
no TLS and no deployment wiring on purpose.
