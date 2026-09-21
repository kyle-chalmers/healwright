# Databricks notebook source
# MAGIC %md
# MAGIC # healwright healer
# MAGIC
# MAGIC One shared job that every monitored job calls from a `__healwright` leaf task when one of its
# MAGIC tasks fails (`mode=leaf-task`), and that a low-frequency schedule runs as a safety net
# MAGIC (`mode=reconcile`, the default). Logic lives in `healwright_core.py` next to this notebook.
# MAGIC
# MAGIC Job parameters: `mode`, `parent_job_id`, `parent_run_id`, `parent_job_name`, `workspace_id`,
# MAGIC `workspace_url`, `config_path` (default `config.yaml` beside this notebook).

# COMMAND ----------

from __future__ import annotations

import argparse
import json
import os
import sys


def _here() -> str:
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:  # Databricks notebooks have no __file__; Files-in-Repos sets cwd to the notebook dir
        return os.getcwd()


sys.path.insert(0, _here())

import healwright_core as hw  # noqa: E402

IN_DATABRICKS = "dbutils" in globals()


def _param(name: str, default: str = "") -> str:
    if IN_DATABRICKS:
        try:
            dbutils.widgets.text(name, default)  # type: ignore[name-defined]  # noqa: F821
            return dbutils.widgets.get(name) or default  # type: ignore[name-defined]  # noqa: F821
        except Exception:
            pass
    return os.environ.get(f"HEALWRIGHT_{name.upper()}", default)


def _secret_getter(scope: str, key: str) -> str:
    if IN_DATABRICKS:
        return dbutils.secrets.get(scope, key)  # type: ignore[name-defined]  # noqa: F821
    return os.environ.get(f"{scope}_{key}".upper(), "")


def run_in_databricks() -> dict:
    mode = _param("mode", "reconcile")
    config_path = _param("config_path", os.path.join(_here(), "config.yaml"))
    cfg = hw.load_config(config_path if os.path.exists(config_path) else None, secret_getter=_secret_getter)
    if not cfg.get("healer_job_id"):
        try:
            ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()  # type: ignore[name-defined]  # noqa: F821
            tags = json.loads(ctx.toJson()).get("tags", {})
            if tags.get("jobId"):
                cfg["healer_job_id"] = int(tags["jobId"])
        except Exception:
            pass
    store = hw.make_store(cfg, spark=globals().get("spark"))
    platform = hw.DatabricksPlatform(workspace_url=_param("workspace_url") or None,
                                     workspace_id=_param("workspace_id") or None, tenant=cfg.get("tenant", ""))
    healer = hw.Healer(cfg, store, platform=platform)
    if mode == "leaf-task":
        run_id = _param("parent_run_id")
        if not run_id:
            raise hw.ConfigError("mode=leaf-task needs parent_run_id (pass {{job.run_id}} from the leaf task)")
        result = healer.run_leaf_task(int(run_id)).as_dict()
        followups = [r.as_dict() for r in healer.run_followups()]
        return {"mode": mode, "result": result, "followups": followups}
    if mode == "reconcile":
        results = [r.as_dict() for r in healer.run_reconcile()]
        followups = [r.as_dict() for r in healer.run_followups()]
        return {"mode": mode, "results": results, "followups": followups}
    if mode == "followups":
        return {"mode": mode, "followups": [r.as_dict() for r in healer.run_followups()]}
    raise hw.ConfigError(f"unknown mode: {mode}")


def run_local(argv: list[str]) -> dict:
    ap = argparse.ArgumentParser(description="healwright healer (local mode)")
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--mode", default="simulate", choices=["simulate", "leaf-task", "reconcile", "followups"])
    ap.add_argument("--config", default=None)
    ap.add_argument("--store", default="sqlite:///tmp/healwright.db", help="sqlite:///abs/path.db or a bare file path")
    ap.add_argument("--classification", default="CODE_BUG", help="simulate: which sample failure to fabricate")
    ap.add_argument("--run-id", type=int, default=100)
    ap.add_argument("--job-id", type=int, default=4242)
    ap.add_argument("--job-name", default="example_job")
    ap.add_argument("--success", action="store_true", help="simulate: a SUCCESS run instead of a failure")
    ap.add_argument("--dry-run", action="store_true", help="record Slack/GitHub calls instead of sending")
    ap.add_argument("--profile", default=None, help="Databricks CLI profile for leaf-task/reconcile modes")
    ap.add_argument("--parent-run-id", type=int, default=None)
    args = ap.parse_args(argv)

    overrides: dict = {"state": {"backend": "sqlite", "sqlite_path": _sqlite_path(args.store)}}
    if args.config is None:
        overrides["patterns_file"] = os.path.join(_here(), "patterns.yaml")
        if args.mode == "simulate":
            overrides["slack"] = {"enabled": True, "channel": "C-local", "bot_token": "dry-run"}
            overrides["github"] = {"enabled": True, "repo": "example-org/example-jobs", "auth": {"type": "token", "token": "dry-run"}}
    cfg = hw.load_config(args.config, overrides, secret_getter=_secret_getter)
    store = hw.make_store(cfg)
    dry = args.dry_run or args.mode == "simulate"

    log = lambda msg: print(msg, file=sys.stderr)  # noqa: E731  # stdout carries only the JSON result
    if args.mode == "simulate":
        healer = hw.Healer(cfg, store, dry_run=dry, log=log)
        if args.success:
            item = hw.RunSuccess("databricks", "0", args.job_id, args.job_name, args.run_id, "simulate", hw.utcnow(), hw.utcnow())
        else:
            item = hw.simulate_incident(args.classification, args.job_id, args.run_id, args.job_name)
        result = healer.handle(item).as_dict()
        posted = [{"method": c["method"], "url": c["url"], "body": c["body"]} for c in (healer.recorder.calls if healer.recorder else [])]
        return {"mode": "simulate", "result": result, "would_send": posted}

    platform = hw.DatabricksPlatform(_workspace_client(args.profile), tenant=cfg.get("tenant", ""))
    healer = hw.Healer(cfg, store, platform=platform, dry_run=dry, log=log)
    if args.mode == "leaf-task":
        if args.parent_run_id is None:
            ap.error("--parent-run-id is required for --mode leaf-task")
        return {"mode": "leaf-task", "result": healer.run_leaf_task(args.parent_run_id).as_dict()}
    if args.mode == "reconcile":
        return {"mode": "reconcile", "results": [r.as_dict() for r in healer.run_reconcile()]}
    return {"mode": "followups", "followups": [r.as_dict() for r in healer.run_followups()]}


def _sqlite_path(spec: str) -> str:
    """`sqlite:///tmp/x.db` -> `/tmp/x.db`; `sqlite://rel.db` or a bare path -> as given."""
    if spec.startswith("sqlite://"):
        spec = spec[len("sqlite://"):]
    return os.path.normpath(spec) if spec else "healwright.db"


def _workspace_client(profile: str | None):
    from databricks.sdk import WorkspaceClient  # type: ignore

    return WorkspaceClient(profile=profile) if profile else WorkspaceClient()


# COMMAND ----------

if __name__ == "__main__" and not IN_DATABRICKS:
    out = run_local(sys.argv[1:])
    print(json.dumps(out, indent=2, default=str))
elif IN_DATABRICKS:
    out = run_in_databricks()
    print(json.dumps(out, indent=2, default=str))
    dbutils.notebook.exit(json.dumps({"mode": out.get("mode"), "summary": str(out.get("result") or len(out.get("results", [])))}))  # type: ignore[name-defined]  # noqa: F821
