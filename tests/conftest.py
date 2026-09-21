from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "templates", "healer"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import healwright_core as hw  # noqa: E402

PATTERNS = os.path.join(ROOT, "templates", "healer", "patterns.yaml")


@pytest.fixture
def classifier():
    return hw.Classifier.from_file(PATTERNS)


@pytest.fixture
def store(tmp_path):
    s = hw.SqliteStore(str(tmp_path / "state.db"))
    yield s
    s.close()


@pytest.fixture
def cfg(tmp_path):
    return hw.load_config(None, {
        "patterns_file": PATTERNS,
        "state": {"backend": "sqlite", "sqlite_path": str(tmp_path / "state.db")},
        "slack": {"enabled": True, "channel": "C-test", "bot_token": "test-bot-token"},
        "github": {"enabled": True, "repo": "example-org/example-jobs", "auth": {"type": "token", "token": "t"},
                   "source_root": "jobs"},
    })


class Clock:
    def __init__(self, start: dt.datetime | None = None):
        self.t = start or dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)

    def __call__(self):
        return self.t

    def tick(self, **kw):
        self.t += dt.timedelta(**kw)
        return self.t


@pytest.fixture
def clock():
    return Clock()


def make_incident(job_id=1, run_id=100, job_name="example_job", msg="KeyError: 'x'", err=None, trace=None,
                  notebook="/jobs/example_job/main", start=None, end=None, result_state="FAILED", source="leaf-task",
                  tasks=True, term=None, workspace_id="w1"):
    inc = hw.RunIncident("databricks", workspace_id, job_id, job_name, run_id, source, result_state=result_state,
                         state_message=msg, error_message=err, error_trace=trace, notebook_path=notebook,
                         run_start_time=start, run_end_time=end, workspace_url="https://example.cloud.databricks.com",
                         termination_code=term)
    if tasks:
        inc.tasks.append(hw.TaskFailure("main", "FAILED", run_id * 10, msg, None, err, trace, notebook))
    return inc


def make_success(job_id=1, run_id=200, job_name="example_job", start=None, end=None, workspace_id="w1"):
    return hw.RunSuccess("databricks", workspace_id, job_id, job_name, run_id, "reconcile", start, end)
