#!/usr/bin/env python3
"""Add or remove the healwright leaf task on Databricks jobs.

What it does to a job:
  + `__healwright`            Run Job task -> the shared healer job, depends on EVERY existing task,
                              run_if AT_LEAST_ONE_FAILED, passes the parent's job/run ids as parameters.
  + `__healwright_propagate`  (opt-in) a spark_python_task that raises, so the run still ends FAILED and
                              the job's existing on_failure notifications keep firing.

Safety model (docs/guardrails.md):
  * dry-run is the default; `--apply` asks for confirmation unless `--yes`
  * live path: fetch -> fingerprint -> patch -> re-fetch -> compare fingerprint -> update (abort on drift)
  * update sends only `tasks`; uninstall uses `fields_to_remove: ["tasks/<key>"]`, never a backup restore
  * refuses the healer job itself, jobs with no tasks, and jobs whose Run-as identity cannot run the healer
    (reported, never auto-granted). Run Job nesting depth cannot be known from a job's own definition
    (callers are invisible), so nesting is a warning you must judge, not a refusal.

Offline use (no workspace): `--job-json path/to/job.json` prints the patched definition and a diff.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import json
import os
import sys

HEALER_TASK_KEY = "__healwright"
PROPAGATE_TASK_KEY = "__healwright_propagate"
SENTINEL_TASK_KEYS = (HEALER_TASK_KEY, PROPAGATE_TASK_KEY)
HEALER_PARAMETERS = {
    "mode": "leaf-task",
    "parent_job_id": "{{job.id}}",
    "parent_run_id": "{{job.run_id}}",
    "parent_job_name": "{{job.name}}",
    "workspace_id": "{{workspace.id}}",
    "workspace_url": "{{workspace.url}}",
}
RUN_JOB_NESTING_LIMIT = 3  # Databricks: Run Job tasks nest up to three levels


class InstallError(Exception):
    pass


# --------------------------------------------------------------------------------------------
# Pure functions on job settings dicts (what `databricks jobs get` returns under `settings`)
# --------------------------------------------------------------------------------------------


def fingerprint(settings: dict) -> str:
    return hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def original_tasks(settings: dict) -> list[dict]:
    return [t for t in (settings.get("tasks") or []) if t.get("task_key") not in SENTINEL_TASK_KEYS]


def parse_propagate_compute(spec: str | None) -> dict | None:
    """'none' | 'serverless:<environment_key>' | 'job_cluster_key:<key>' | 'existing_cluster_id:<id>'."""
    if not spec or spec == "none":
        return None
    kind, _, value = spec.partition(":")
    if kind == "serverless":
        return {"environment_key": value or "healwright"}
    if kind in ("job_cluster_key", "existing_cluster_id") and value:
        return {kind: value}
    raise InstallError(f"unknown propagate compute spec: {spec!r} (use none | serverless:<env_key> | job_cluster_key:<key> | existing_cluster_id:<id>)")


def preflight(settings: dict, healer_job_id: int, healer_job_name: str | None = None, job_id: int | None = None) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings). Any error blocks installation."""
    errors: list[str] = []
    warnings: list[str] = []
    name = settings.get("name") or ""
    if healer_job_name and name == healer_job_name:
        errors.append("this is the healer job itself; it must not monitor itself")
    if job_id is not None and int(job_id) == int(healer_job_id):
        errors.append("job_id equals healer_job_id; refusing to make the healer call itself")
    tasks = original_tasks(settings)
    if not tasks:
        errors.append("job has no tasks (legacy single-task format?); convert to a multi-task job first")
    for t in tasks:
        rj = t.get("run_job_task") or {}
        if rj.get("job_id") == healer_job_id:
            errors.append(f"task {t.get('task_key')!r} already calls the healer job directly")
        if t.get("run_if", "ALL_SUCCESS") != "ALL_SUCCESS":
            warnings.append(f"task {t.get('task_key')!r} has run_if={t['run_if']}; the job already handles failures somewhere; review interaction")
    if any(t.get("run_job_task") for t in tasks):
        warnings.append("job contains Run Job tasks; Databricks limits Run Job nesting to "
                        f"{RUN_JOB_NESTING_LIMIT} levels, and this job may itself be called by another job's Run Job task")
    if settings.get("format", "MULTI_TASK") != "MULTI_TASK":
        errors.append(f"job format is {settings.get('format')}; only MULTI_TASK jobs are supported")
    return errors, warnings


def build_healer_task(tasks: list[dict], healer_job_id: int) -> dict:
    return {
        "task_key": HEALER_TASK_KEY,
        "description": "healwright: on any task failure, call the shared healer job (trigger-based self-healing).",
        "depends_on": [{"task_key": t["task_key"]} for t in tasks],
        "run_if": "AT_LEAST_ONE_FAILED",
        "run_job_task": {"job_id": int(healer_job_id), "job_parameters": dict(HEALER_PARAMETERS)},
        "timeout_seconds": 1800,
        "max_retries": 0,
        "email_notifications": {},
        "webhook_notifications": {},
    }


def build_propagate_task(tasks: list[dict], python_file: str, compute: dict, source: str = "GIT") -> dict:
    task = {
        "task_key": PROPAGATE_TASK_KEY,
        "description": "healwright: fail on purpose after an upstream failure so the run stays FAILED and on_failure notifications keep firing.",
        "depends_on": [{"task_key": t["task_key"]} for t in tasks],
        "run_if": "AT_LEAST_ONE_FAILED",
        "spark_python_task": {"python_file": python_file, "source": source},
        "timeout_seconds": 300,
        "max_retries": 0,
        "email_notifications": {},
        "webhook_notifications": {},
    }
    task.update(compute)
    return task


def patch_settings(settings: dict, healer_job_id: int, propagate_compute: dict | None = None,
                   propagate_file: str | None = None, propagate_source: str = "GIT") -> dict:
    """Return a copy of `settings` with the healwright tasks appended (idempotent)."""
    new = copy.deepcopy(settings)
    tasks = original_tasks(new)
    if not tasks:
        raise InstallError("job has no tasks to depend on")
    out = list(tasks) + [build_healer_task(tasks, healer_job_id)]
    if propagate_compute:
        if not propagate_file:
            raise InstallError("propagate requires --propagate-file (repo path of templates/healer/propagate.py)")
        out.append(build_propagate_task(tasks, propagate_file, propagate_compute, propagate_source))
    new["tasks"] = out
    return new


def unpatch_settings(settings: dict) -> tuple[dict, list[str]]:
    """Return (settings without healwright tasks, fields_to_remove for the Jobs API)."""
    new = copy.deepcopy(settings)
    present = [t["task_key"] for t in (new.get("tasks") or []) if t.get("task_key") in SENTINEL_TASK_KEYS]
    new["tasks"] = original_tasks(new)
    return new, [f"tasks/{k}" for k in present]


def is_installed(settings: dict) -> bool:
    return any(t.get("task_key") == HEALER_TASK_KEY for t in (settings.get("tasks") or []))


def render_diff(before: dict, after: dict, name: str = "job") -> str:
    a = json.dumps(before, indent=2, sort_keys=True).splitlines()
    b = json.dumps(after, indent=2, sort_keys=True).splitlines()
    return "\n".join(difflib.unified_diff(a, b, fromfile=f"{name} (live)", tofile=f"{name} (patched)", lineterm=""))


def run_as_identity(settings: dict) -> str | None:
    ra = settings.get("run_as") or {}
    return ra.get("service_principal_name") or ra.get("user_name")


def check_permission(healer_permissions: dict, identity: str | None) -> tuple[bool | None, str]:
    """Does `identity` hold CAN_MANAGE_RUN (or higher) on the healer job? None when unknown."""
    if not identity:
        return None, "job has no explicit run_as; the creator's identity applies and could not be checked"
    ok_levels = {"CAN_MANAGE_RUN", "CAN_MANAGE", "IS_OWNER"}
    for acl in healer_permissions.get("access_control_list") or []:
        who = acl.get("service_principal_name") or acl.get("user_name") or acl.get("group_name")
        levels = {p.get("permission_level") for p in acl.get("all_permissions") or []}
        if who == identity and levels & ok_levels:
            return True, f"{identity} has {sorted(levels & ok_levels)[0]} on the healer job"
    return False, f"{identity} has no CAN_MANAGE_RUN on the healer job; grant it (Permissions > Can manage run) before installing"


# --------------------------------------------------------------------------------------------
# Live workspace helpers (databricks-sdk, imported lazily)
# --------------------------------------------------------------------------------------------


def _client(profile: str | None):
    from databricks.sdk import WorkspaceClient  # type: ignore

    return WorkspaceClient(profile=profile) if profile else WorkspaceClient()


def _get_job(w, job_id: int) -> tuple[dict, str | None]:
    """(settings, effective identity). The Jobs API reports who a job actually runs as in the
    top-level `run_as_user_name`; the creator is only the last-resort fallback."""
    job = w.jobs.get(int(job_id))
    settings = job.settings.as_dict()
    return settings, getattr(job, "run_as_user_name", None) or getattr(job, "creator_user_name", None)


def _get_settings(w, job_id: int) -> dict:
    return _get_job(w, job_id)[0]


def sentinel_removals(live: dict, new: dict) -> list[str]:
    """Jobs `update` merges task lists by key, so a sentinel task left out of `new` would survive.
    Name it in `fields_to_remove` in the same call."""
    live_keys = {t.get("task_key") for t in (live.get("tasks") or []) if t.get("task_key") in SENTINEL_TASK_KEYS}
    new_keys = {t.get("task_key") for t in (new.get("tasks") or [])}
    return [f"tasks/{k}" for k in sorted(live_keys - new_keys)]


def _get_permissions(w, job_id: int) -> dict:
    try:
        return w.jobs.get_permissions(str(job_id)).as_dict()
    except Exception as e:  # permission read is best effort: report, never block on the read itself
        return {"error": str(e)}


def apply_update(w, job_id: int, new_settings: dict, expected_fingerprint: str) -> None:
    from databricks.sdk.service.jobs import JobSettings  # type: ignore

    live = _get_settings(w, job_id)
    if fingerprint(live) != expected_fingerprint:
        raise InstallError("job definition changed between read and write (fingerprint drift); re-run to re-read")
    removals = sentinel_removals(live, new_settings)
    kwargs = {"fields_to_remove": removals} if removals else {}
    w.jobs.update(job_id=int(job_id), new_settings=JobSettings.from_dict({"tasks": new_settings["tasks"]}), **kwargs)


def apply_uninstall(w, job_id: int, fields_to_remove: list[str]) -> None:
    if fields_to_remove:
        w.jobs.update(job_id=int(job_id), fields_to_remove=fields_to_remove)


def backup(settings: dict, job_id: int | str, backup_dir: str) -> str:
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(backup_dir, f"job-{job_id}-{fingerprint(settings)[:12]}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2, sort_keys=True)
    return path


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--job-json", help="offline: a job definition (settings dict or `databricks jobs get` output)")
    src.add_argument("--job-id", type=int, action="append", help="live: job id (repeatable)")
    ap.add_argument("--profile", help="Databricks CLI profile")
    ap.add_argument("--healer-job-id", type=int, required=True)
    ap.add_argument("--healer-job-name", default="healwright-healer")
    ap.add_argument("--propagate-compute", default="none", help="none | serverless:<env_key> | job_cluster_key:<key> | existing_cluster_id:<id>")
    ap.add_argument("--propagate-file", default=None, help="repo path of propagate.py, e.g. jobs/self_healing/propagate.py")
    ap.add_argument("--propagate-source", default="GIT", choices=["GIT", "WORKSPACE"])
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--apply", action="store_true", help="write to the workspace (default: dry run)")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--backup-dir", default=".healwright-backups")
    ap.add_argument("--skip-permission-check", action="store_true", help="install even if the run-as identity's permission on the healer could not be verified")
    ap.add_argument("--out", help="offline: write the patched definition here")
    args = ap.parse_args(argv)

    propagate_compute = parse_propagate_compute(args.propagate_compute)

    if args.job_json:
        with open(args.job_json, encoding="utf-8") as fh:
            doc = json.load(fh)
        settings = doc.get("settings", doc)
        return _offline(settings, args, propagate_compute)

    if args.apply and not args.yes and not sys.stdin.isatty():
        print("refusing --apply without --yes in a non-interactive shell", file=sys.stderr)
        return 2
    w = _client(args.profile)
    rc = 0
    for job_id in args.job_id:
        rc |= _live_one(w, job_id, args, propagate_compute)
    return rc


def _offline(settings: dict, args, propagate_compute) -> int:
    if args.uninstall:
        new, fields = unpatch_settings(settings)
        print(render_diff(settings, new, settings.get("name", "job")))
        print(f"\nfields_to_remove: {json.dumps(fields)}")
    else:
        errors, warnings = preflight(settings, args.healer_job_id, args.healer_job_name)
        for wmsg in warnings:
            print(f"WARN: {wmsg}")
        if errors:
            for e in errors:
                print(f"ERROR: {e}")
            return 1
        new = patch_settings(settings, args.healer_job_id, propagate_compute, args.propagate_file, args.propagate_source)
        print(render_diff(settings, new, settings.get("name", "job")))
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(new, fh, indent=2, sort_keys=True)
                fh.write("\n")
    print("\n(dry run; offline definition, nothing written to any workspace)")
    return 0


def _live_one(w, job_id: int, args, propagate_compute) -> int:
    settings, creator = _get_job(w, job_id)
    name = settings.get("name", str(job_id))
    fp = fingerprint(settings)
    print(f"== {name} (job {job_id}) fingerprint {fp[:12]}")
    if args.uninstall:
        if not is_installed(settings):
            print("   not installed; nothing to do")
            return 0
        new, fields = unpatch_settings(settings)
        print(render_diff(settings, new, name))
        print(f"   fields_to_remove: {json.dumps(fields)}")
        if not args.apply:
            print("   dry run; pass --apply to remove")
            return 0
        if not args.yes and input(f"   remove healwright tasks from {name}? [y/N] ").strip().lower() != "y":
            return 0
        path = backup(settings, job_id, args.backup_dir)
        live_fp = fingerprint(_get_settings(w, job_id))
        if live_fp != fp:
            print("   ERROR: definition changed since read; aborting (re-run)")
            return 1
        apply_uninstall(w, job_id, fields)
        print(f"   removed; pre-change backup at {path}")
        return 0

    errors, warnings = preflight(settings, args.healer_job_id, args.healer_job_name, job_id)
    perms = _get_permissions(w, args.healer_job_id)
    if "error" in perms:
        ok, msg = None, f"could not read healer job permissions: {perms['error']}"
    else:
        ok, msg = check_permission(perms, run_as_identity(settings) or creator)
    if ok:
        warnings.append(msg)
    elif args.skip_permission_check:
        warnings.append(msg + " (proceeding: --skip-permission-check)")
    elif ok is None:
        errors.append(msg + " (cannot verify the healer can be run; pass --skip-permission-check to override)")
    else:
        errors.append(msg)
    for wmsg in warnings:
        print(f"   WARN: {wmsg}")
    if errors:
        for e in errors:
            print(f"   ERROR: {e}")
        return 1
    new = patch_settings(settings, args.healer_job_id, propagate_compute, args.propagate_file, args.propagate_source)
    if is_installed(settings) and fingerprint(new) == fp:
        print("   already installed and up to date")
        return 0
    print(render_diff(settings, new, name))
    if not args.apply:
        print("   dry run; pass --apply to install")
        return 0
    if not args.yes and input(f"   install healwright leaf task on {name}? [y/N] ").strip().lower() != "y":
        return 0
    path = backup(settings, job_id, args.backup_dir)
    try:
        apply_update(w, job_id, new, fp)
    except InstallError as e:
        print(f"   ERROR: {e}")
        return 1
    print(f"   installed; pre-change backup at {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
