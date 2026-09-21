import datetime as dt

import healwright_core as hw
from conftest import make_incident, make_success


def build(cfg, store, clock, responder=None, platform=None):
    rec = hw.RecordingTransport(responder)
    slack = hw.SlackClient(cfg["slack"]["bot_token"], rec)
    gh = hw.GitHubClient(cfg["github"]["repo"], cfg["github"]["auth"], rec)
    h = hw.Healer(cfg, store, platform=platform, slack=slack, github=gh, now=clock, log=lambda s: None)
    return h, rec


def slack_calls(rec):
    return [c for c in rec.calls if "slack.com" in c["url"]]


def issue_posts(rec):
    return [c for c in rec.calls if c["method"] == "POST" and c["url"].endswith("/issues")]


def test_code_bug_alerts_and_opens_issue_once(cfg, store, clock):
    h, rec = build(cfg, store, clock)
    inc = make_incident(end=clock())
    r = h.handle(inc)
    assert r.outcome == "handled" and r.classification == "CODE_BUG"
    assert "slack_alert" in r.actions and "github_issue" in r.actions
    assert len(slack_calls(rec)) == 1 and len(issue_posts(rec)) == 1
    body = issue_posts(rec)[0]["body"]["body"]
    assert f"{hw.INCIDENT_MARKER}{inc.key}" in body
    assert "Raw traces are not attached" in body
    # second delivery of the same run (say the reconciler) is a no-op
    r2 = h.handle(make_incident(end=clock(), source="reconcile"))
    assert r2.outcome == "done" and len(issue_posts(rec)) == 1 and len(slack_calls(rec)) == 1


def test_second_failure_same_pattern_is_suppressed_and_no_duplicate_issue(cfg, store, clock):
    h, rec = build(cfg, store, clock)
    h.handle(make_incident(run_id=1, end=clock()))
    clock.tick(hours=1)
    r = h.handle(make_incident(run_id=2, end=clock()))
    assert r.consecutive == 2
    assert "slack_alert" not in r.actions and any("suppressed" in n for n in r.notes)
    assert len(issue_posts(rec)) == 1 and any("still open" in n for n in r.notes)


def test_closed_issue_on_still_failing_job_gets_new_issue(cfg, store, clock):
    state = {"n": 0}

    def responder(method, url, body):
        if method == "POST" and url.endswith("/issues"):
            state["n"] += 1
            return 201, {"number": state["n"], "html_url": f"https://example.com/i/{state['n']}", "state": "open"}
        if "/search/issues" in url:
            return 200, {"total_count": 0, "items": []}
        if url.endswith("/issues/1"):
            return 200, {"number": 1, "state": "closed", "labels": []}
        return 200, hw._default_fake_response(method, url, body)

    h, rec = build(cfg, store, clock, responder)
    h.handle(make_incident(run_id=1, end=clock()))
    clock.tick(hours=6)
    r = h.handle(make_incident(run_id=2, end=clock()))
    assert "github_issue" in r.actions and store.get_job_state(r.incident_key and make_incident().job_key).issue_number == 2


def test_transient_does_not_open_issue(cfg, store, clock):
    h, rec = build(cfg, store, clock)
    r = h.handle(make_incident(msg="502 Bad Gateway", end=clock()))
    assert r.classification == "TRANSIENT" and "github_issue" not in r.actions and issue_posts(rec) == []


def test_issue_requires_notebook_inside_source_root(cfg, store, clock):
    h, rec = build(cfg, store, clock)
    r = h.handle(make_incident(notebook="/Shared/adhoc/thing", end=clock()))
    assert "github_issue" not in r.actions and any("source_root" in n for n in r.notes)


def test_issue_cap_per_hour(cfg, store, clock):
    cfg["policy"]["max_issues_per_hour"] = 2
    h, rec = build(cfg, store, clock)
    for jid in range(1, 5):
        h.handle(make_incident(job_id=jid, run_id=jid, job_name=f"job{jid}", end=clock()))
    assert len(issue_posts(rec)) == 2


def test_alert_cap_per_run_then_no_more_posts(cfg, store, clock):
    cfg["policy"]["max_alerts_per_run"] = 2
    h, rec = build(cfg, store, clock)
    for jid in range(1, 5):
        h.handle(make_incident(job_id=jid, run_id=jid, job_name=f"job{jid}", msg="502 Bad Gateway", end=clock()))
    assert len(slack_calls(rec)) == 2


def test_resolution_posts_in_thread_and_resets(cfg, store, clock):
    h, rec = build(cfg, store, clock)
    fail_time = clock()
    h.handle(make_incident(run_id=1, start=fail_time - dt.timedelta(minutes=5), end=fail_time))
    thread_ts = store.get_job_state(make_incident().job_key).thread_ts
    assert thread_ts
    clock.tick(hours=30)  # older than thread reset: RESOLVED still threads under the failure
    r = h.handle(make_success(run_id=2, start=clock()))
    assert "slack_resolved" in r.actions and "streak_reset" in r.actions
    last = slack_calls(rec)[-1]["body"]
    assert last.get("thread_ts") == thread_ts and "RESOLVED" in last["text"]
    assert store.get_job_state(make_incident().job_key).issue_number is None


def test_thread_reset_after_window(cfg, store, clock):
    cfg["policy"]["dedup_window_hours"] = 0
    h, rec = build(cfg, store, clock)
    h.handle(make_incident(run_id=1, msg="502 Bad Gateway", end=clock()))
    clock.tick(hours=25)
    h.handle(make_incident(run_id=2, msg="502 Bad Gateway", end=clock()))
    calls = slack_calls(rec)
    assert len(calls) == 2 and "thread_ts" not in calls[1]["body"]


def test_escalation_footer_lists_other_jobs(cfg, store, clock):
    cfg["policy"]["dedup_window_hours"] = 0
    h, rec = build(cfg, store, clock)
    msg = "Failed to checkout Git repository: UNAUTHENTICATED"
    h.handle(make_incident(job_id=2, run_id=20, job_name="other_job", msg=msg, end=clock()))
    for run in (1, 2, 3):
        clock.tick(hours=1)
        h.handle(make_incident(run_id=run, msg=msg, end=clock()))
    text = slack_calls(rec)[-1]["body"]["text"]
    assert "ESCALATION (3 consecutive" in text and "other_job" in text


def test_shadow_mode_writes_state_but_posts_nothing(cfg, store, clock):
    cfg["policy"]["shadow"] = True
    h, rec = build(cfg, store, clock)
    r = h.handle(make_incident(end=clock()))
    assert r.outcome == "handled" and r.actions == [] and rec.calls == []
    assert store.get_job_state(make_incident().job_key).consecutive_failures == 1


def test_unmonitored_job_skipped(cfg, store, clock):
    cfg["jobs"]["include"] = ["etl_*"]
    h, rec = build(cfg, store, clock)
    assert h.handle(make_incident(job_name="adhoc_thing", end=clock())).outcome == "skipped"
    assert h.handle(make_incident(job_name="etl_thing", end=clock())).outcome == "handled"


def test_healer_job_is_never_monitored(cfg, store, clock):
    cfg["healer_job_id"] = 77
    h, rec = build(cfg, store, clock)
    assert h.handle(make_incident(job_id=77, job_name="whatever")).outcome == "skipped"
    assert h.handle(make_incident(job_id=1, job_name=cfg["healer_job_name"])).outcome == "skipped"


def test_secret_in_error_is_redacted_everywhere(cfg, store, clock):
    cfg["policy"]["include_raw_trace"] = True
    h, rec = build(cfg, store, clock)
    inc = make_incident(msg="KeyError: 'x' token=xoxb-111111111111-abcdefghijkl", trace="password=hunter22\nKeyError", end=clock())  # leak-scan-ok
    h.handle(inc)
    everything = str(rec.calls)
    assert "xoxb-1111" not in everything and "hunter22" not in everything


def test_followups_notify_once(cfg, store, clock):
    def responder(method, url, body):
        if url.endswith("/timeline?per_page=100"):
            return 200, [{"event": "cross-referenced", "source": {"issue": {"pull_request": {}, "html_url": "https://example.com/pr/9"}}}]
        return 200, hw._default_fake_response(method, url, body)

    h, rec = build(cfg, store, clock, responder)
    h.handle(make_incident(end=clock()))
    out = h.run_followups()
    assert len(out) == 1 and "slack_followup" in out[0].actions
    assert "pr/9" in slack_calls(rec)[-1]["body"]["text"]
    assert h.run_followups() == []


class FakePlatform:
    platform = "databricks"
    workspace_id = "w1"
    workspace_url = "https://example.cloud.databricks.com"

    def __init__(self, items):
        self.items = items
        self.windows = []

    def fetch_run(self, run_id, source="leaf-task"):
        for i in self.items:
            if i.run_id == run_id:
                i.source = source
                return i
        return None

    def list_completed_runs(self, start_ms, end_ms):
        self.windows.append((start_ms, end_ms))
        return [i for i in self.items if i.run_start_time and start_ms <= i.run_start_time.timestamp() * 1000 <= end_ms]


def test_leaf_task_and_reconcile_share_incident(cfg, store, clock):
    inc = make_incident(run_id=5, start=clock() - dt.timedelta(minutes=30), end=clock())
    plat = FakePlatform([inc])
    h, rec = build(cfg, store, clock, platform=plat)
    r1 = h.run_leaf_task(5)
    assert r1.outcome == "handled"
    results = h.run_reconcile()
    assert [r.outcome for r in results] == ["done"]
    assert len(issue_posts(rec)) == 1
    wm = store.get_watermark("reconcile_end")
    assert wm is not None


def test_reconcile_uses_watermark_with_overlap(cfg, store, clock):
    cfg["reconcile"] = {"overlap_minutes": 10, "initial_lookback_minutes": 60, "max_window_minutes": 1440, "max_run_hours": 0}
    plat = FakePlatform([])
    h, rec = build(cfg, store, clock, platform=plat)
    h.run_reconcile()
    first_start, first_end = plat.windows[0]
    assert first_end - first_start == 60 * 60 * 1000
    clock.tick(hours=2)
    h.run_reconcile()
    second_start, _ = plat.windows[1]
    assert second_start == first_end - 10 * 60 * 1000


def test_reconcile_reaches_back_for_long_running_jobs(cfg, store, clock):
    # The API filters on START time and only returns completed runs. A run that started before the
    # previous sweep and finished after it must still be found: the window reaches back max_run_hours.
    cfg["reconcile"] = {"overlap_minutes": 10, "initial_lookback_minutes": 60, "max_window_minutes": 4320, "max_run_hours": 24}
    plat = FakePlatform([])
    h, rec = build(cfg, store, clock, platform=plat)
    h.run_reconcile()  # sets the watermark to now
    long_run = make_incident(run_id=9, start=clock() - dt.timedelta(hours=5), end=clock() + dt.timedelta(hours=1))
    plat.items.append(long_run)
    clock.tick(hours=2)
    results = h.run_reconcile()
    assert [r.outcome for r in results] == ["handled"]


def test_leaf_task_with_only_sentinel_failure_is_nothing_to_do(cfg, store, clock):
    inc = make_incident(run_id=7, tasks=False, result_state="SUCCESS_WITH_FAILURES")
    inc.tasks.append(hw.TaskFailure(hw.PROPAGATE_TASK_KEY, "FAILED", 1, "boom"))
    h, rec = build(cfg, store, clock, platform=FakePlatform([inc]))
    assert h.run_leaf_task(7).outcome == "skipped"


def test_dry_run_healer_records_instead_of_sending(cfg, store, clock):
    cfg["github"]["auth"] = {"type": "token", "token": ""}
    h = hw.Healer(cfg, store, now=clock, log=lambda s: None, dry_run=True)
    r = h.handle(make_incident(end=clock()))
    assert r.outcome == "handled" and h.recorder is not None and len(h.recorder.calls) >= 2


def test_source_folder_mapping():
    assert hw.source_folder("/jobs/My_Job/nb", "jobs") == "jobs/My_Job/"
    assert hw.source_folder("jobs /My_Job/nb", "jobs") == "jobs/My_Job/"
    assert hw.source_folder("/Shared/other/nb", "jobs") is None
    assert hw.source_folder("/jobs/../etc/nb", "jobs") is None
    assert hw.source_folder("/src/pipelines/My_Job/nb", "src/pipelines") == "src/pipelines/My_Job/"


def test_config_refs_resolve(monkeypatch, tmp_path):
    monkeypatch.setenv("HW_TOKEN", "abc")
    cfg = hw.load_config(None, {"slack": {"bot_token": "${env:HW_TOKEN}"}, "github": {"auth": {"token": "${secret:scope/key}"}}},
                         secret_getter=lambda scope, key: f"{scope}:{key}")
    assert cfg["slack"]["bot_token"] == "abc" and cfg["github"]["auth"]["token"] == "scope:key"
    cfg2 = hw.load_config(None, {"slack": {"bot_token": "${secret:scope/key}"}})
    assert cfg2["slack"]["bot_token"] == ""



def test_slack_failure_leaves_no_receipt_and_releases_incident(cfg, store, clock):
    def responder(method, url, body):
        if "slack.com" in url:
            return 500, {"ok": False, "error": "boom"}
        return 200, hw._default_fake_response(method, url, body)

    h, rec = build(cfg, store, clock, responder)
    inc = make_incident(end=clock())
    r = h.handle(inc)
    assert r.outcome == "released" and "slack_alert" in r.failed and "slack_alert" not in r.actions
    assert not store.has_action(inc.key, "slack_alert")
    assert store.get_incident(inc.key)["status"] == "pending"
    # a later delivery (the reconciler) resumes and, with Slack healthy, posts exactly once
    h2, rec2 = build(cfg, store, clock)
    r2 = h2.handle(make_incident(end=clock(), source="reconcile"))
    assert r2.outcome == "resumed" and "slack_alert" in r2.actions and r2.consecutive == 1
    assert len(slack_calls(rec2)) == 1


def test_github_failure_does_not_mark_incident_done(cfg, store, clock):
    def responder(method, url, body):
        if method == "POST" and url.endswith("/issues"):
            return 502, {"message": "bad gateway"}
        return 200, hw._default_fake_response(method, url, body)

    h, rec = build(cfg, store, clock, responder)
    inc = make_incident(end=clock())
    r = h.handle(inc)
    assert r.outcome == "released" and "github_issue" in r.failed
    assert store.get_incident(inc.key)["status"] == "pending"
    assert store.get_job_state(inc.job_key).issue_claim_token is None  # slot released for the retry


def test_clear_issue_is_conditional_on_number(store, clock):
    inc = make_incident()
    store.claim_incident(inc, hw.Classification("CODE_BUG", "STAGE_2", "python_error"), 900, clock(), lambda p: (False, ""))
    assert store.claim_issue_slot(inc.job_key, "t", 900, clock()) and store.set_issue(inc.job_key, "t", 5, "u", clock())
    assert not store.clear_issue(inc.job_key, 4)   # someone replaced it: leave alone
    assert store.get_job_state(inc.job_key).issue_number == 5
    assert store.clear_issue(inc.job_key, 5)
    assert store.get_job_state(inc.job_key).issue_number is None


def test_platform_controlled_fields_are_redacted_and_neutralised(cfg, store, clock):
    h, rec = build(cfg, store, clock)
    inc = make_incident(job_name="job owned by someone@example.com | `x`", end=clock())
    inc.tasks[0].task_key = "task <script>"
    h.handle(inc)
    text = slack_calls(rec)[0]["body"]["text"]
    issue = issue_posts(rec)[0]["body"]
    assert "someone@example.com" not in text and "someone@example.com" not in issue["title"] and "someone@example.com" not in issue["body"]
    assert "`x`" not in issue["body"] and "<script>" not in issue["body"]


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeJobsApi:
    def __init__(self, run):
        self.run = run

    def get_run(self, run_id):
        return self.run

    def get_run_output(self, task_run_id):
        return _Obj(error="KeyError: 'x'", error_trace="Traceback\nKeyError: 'x'")


class _FakeClient:
    def __init__(self, run):
        self.jobs = _FakeJobsApi(run)
        self.clusters = _Obj(get=lambda cid: _Obj(termination_reason=None))
        self.config = _Obj(host="https://example.cloud.databricks.com")

    def get_workspace_id(self):
        return 42


def _running_parent_with_failed_task():
    failed_task = _Obj(task_key="main", run_id=901, start_time=1_700_000_000_000, end_time=1_700_000_600_000, attempt_number=1,
                       state=_Obj(result_state=_Obj(value="FAILED"), life_cycle_state=_Obj(value="TERMINATED"), state_message="Workload failed"),
                       notebook_task=_Obj(notebook_path="/jobs/example_job/main"), cluster_instance=None)
    healer_task = _Obj(task_key=hw.HEALER_TASK_KEY, run_id=902, start_time=None, end_time=None, attempt_number=0,
                       state=_Obj(result_state=None, life_cycle_state=_Obj(value="RUNNING"), state_message=""), notebook_task=None, cluster_instance=None)
    return _Obj(job_id=7, run_id=900, run_name="example_job", start_time=1_700_000_000_000, end_time=None,
                state=_Obj(result_state=None, life_cycle_state=_Obj(value="RUNNING"), state_message=""), status=None,
                tasks=[failed_task, healer_task])


def test_leaf_task_sees_failed_task_while_parent_run_is_still_running():
    plat = hw.DatabricksPlatform(_FakeClient(_running_parent_with_failed_task()))
    inc = plat.fetch_run(900, "leaf-task")
    assert isinstance(inc, hw.RunIncident)
    assert inc.result_state == "FAILED" and inc.life_cycle_state == "RUNNING"
    assert [t.task_key for t in inc.tasks] == ["main"]
    assert inc.error_message == "KeyError: 'x'" and inc.notebook_path == "/jobs/example_job/main"
    assert inc.run_end_time is not None and inc.workspace_id == "42"


def test_running_parent_with_no_failed_task_is_nothing():
    run = _running_parent_with_failed_task()
    run.tasks[0].state = _Obj(result_state=_Obj(value="SUCCESS"), life_cycle_state=_Obj(value="TERMINATED"), state_message="")
    assert hw.DatabricksPlatform(_FakeClient(run)).fetch_run(900) is None


def test_v2_status_decoding():
    def run_with(code, ttype):
        task = _Obj(task_key="main", run_id=1, start_time=1, end_time=2, attempt_number=0, notebook_task=None, cluster_instance=None,
                    state=None, status=_Obj(state=_Obj(value="TERMINATED"), termination_details=_Obj(code=_Obj(value=code), type=_Obj(value=ttype), message="m")))
        return _Obj(job_id=1, run_id=10, run_name="j", start_time=1, end_time=2, state=None,
                    status=_Obj(state=_Obj(value="TERMINATED"), termination_details=_Obj(code=_Obj(value=code), type=_Obj(value=ttype), message="m")),
                    tasks=[task])

    P = hw.DatabricksPlatform
    assert P._state_of(run_with("SUCCESS", "SUCCESS"))[0] == "SUCCESS"
    assert P._state_of(run_with("RUN_EXECUTION_ERROR", "CLIENT_ERROR"))[0] == "FAILED"
    assert P._state_of(run_with("CLOUD_FAILURE", "CLOUD_FAILURE"))[0] == "FAILED"
    assert P._state_of(run_with("USER_CANCELED", "SUCCESS"))[0] == "CANCELED"
    assert P._state_of(run_with("DRIVER_TIMEOUT", "CLIENT_ERROR"))[0] == "TIMEDOUT"
    plat = P(_FakeClient(run_with("RUN_EXECUTION_ERROR", "CLIENT_ERROR")))
    inc = plat.fetch_run(10, "reconcile")
    assert isinstance(inc, hw.RunIncident) and inc.termination_code is None  # generic code: not a Stage-1 signal
    # a generic cluster-level code falls back to the cluster's termination reason (a real SDK enum value)
    client = _FakeClient(run_with("CLUSTER_ERROR", "CLOUD_FAILURE"))
    client.jobs.run.tasks[0].cluster_instance = _Obj(cluster_id="c-1")
    client.clusters = _Obj(get=lambda cid: _Obj(termination_reason=_Obj(code=_Obj(value="SPOT_INSTANCE_TERMINATION"))))
    inc2 = P(client).fetch_run(10, "reconcile")
    assert inc2.termination_code == "SPOT_INSTANCE_TERMINATION"
    # a specific job-level code that the SDK keeps is used directly
    inc3 = P(_FakeClient(run_with("LIBRARY_INSTALLATION_ERROR", "CLIENT_ERROR"))).fetch_run(10, "reconcile")
    assert inc3.termination_code == "LIBRARY_INSTALLATION_ERROR"



def test_complete_and_release_are_conditional_on_owner(store, clock):
    inc = make_incident()
    r1 = store.claim_incident(inc, hw.Classification("CODE_BUG", "STAGE_2", "python_error"), 900, clock(), lambda p: (False, ""))
    clock.tick(seconds=901)
    r2 = store.claim_incident(inc, hw.Classification("CODE_BUG", "STAGE_2", "python_error"), 900, clock(), lambda p: (False, ""))
    assert r2.status == "resumed" and r2.owner and r2.owner != r1.owner
    store.complete_incident(inc.key, clock(), r1.owner)   # the expired worker cannot close it
    assert store.get_incident(inc.key)["status"] == "processing"
    store.release_incident(inc.key, clock(), r1.owner)    # nor release it
    assert store.get_incident(inc.key)["status"] == "processing"
    store.complete_incident(inc.key, clock(), r2.owner)
    assert store.get_incident(inc.key)["status"] == "done"
