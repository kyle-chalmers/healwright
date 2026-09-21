import datetime as dt
import threading

import healwright_core as hw
from conftest import Clock, make_incident, make_success

CB = hw.Classification("CODE_BUG", "STAGE_2", "python_error")
NOSUP = lambda pre: (False, "")  # noqa: E731


def test_incident_key_parity_across_sources():
    a = make_incident(run_id=5, source="leaf-task")
    b = make_incident(run_id=5, source="reconcile")
    assert a.key == b.key
    assert make_incident(run_id=5, workspace_id="w2").key != a.key
    other_tenant = make_incident(run_id=5)
    other_tenant.tenant = "acct-2"
    assert other_tenant.key != a.key


def test_claim_new_then_done_then_duplicate(store, clock):
    inc = make_incident()
    r1 = store.claim_incident(inc, CB, 900, clock(), NOSUP)
    assert r1.status == "new" and r1.consecutive_failures == 1
    store.complete_incident(inc.key, clock())
    r2 = store.claim_incident(inc, CB, 900, clock(), NOSUP)
    assert r2.status == "done"
    assert store.get_job_state(inc.job_key).consecutive_failures == 1  # not double counted


def test_claim_busy_within_lease_then_resumed_after_expiry(store, clock):
    inc = make_incident()
    assert store.claim_incident(inc, CB, 900, clock(), NOSUP).status == "new"
    assert store.claim_incident(inc, CB, 900, clock(), NOSUP).status == "busy"
    clock.tick(seconds=901)
    r = store.claim_incident(inc, CB, 900, clock(), NOSUP)
    assert r.status == "resumed" and r.consecutive_failures == 1
    assert store.get_incident(inc.key)["attempts"] == 2


def test_streak_increments_and_suppression_uses_pre_state(store, clock):
    a = make_incident(run_id=1, end=clock())
    r1 = store.claim_incident(a, CB, 900, clock(), lambda pre: hw.should_suppress(CB, pre, clock(), 4))
    assert not r1.alert_suppressed
    store.complete_incident(a.key, clock())
    clock.tick(hours=1)
    b = make_incident(run_id=2, end=clock())
    r2 = store.claim_incident(b, CB, 900, clock(), lambda pre: hw.should_suppress(CB, pre, clock(), 4))
    assert r2.consecutive_failures == 2 and r2.alert_suppressed
    clock.tick(hours=5)
    c = make_incident(run_id=3, end=clock())
    r3 = store.claim_incident(c, CB, 900, clock(), lambda pre: hw.should_suppress(CB, pre, clock(), 4))
    assert r3.consecutive_failures == 3 and not r3.alert_suppressed


def test_success_resets_only_when_newer_than_last_failure(store, clock):
    fail_end = clock()
    inc = make_incident(run_id=1, start=fail_end - dt.timedelta(minutes=5), end=fail_end)
    store.claim_incident(inc, CB, 900, clock(), NOSUP)
    old_success = make_success(run_id=0, start=fail_end - dt.timedelta(hours=2))
    pre, reset = store.apply_success(old_success, clock())
    assert not reset and store.get_job_state(inc.job_key).consecutive_failures == 1
    new_success = make_success(run_id=2, start=fail_end + dt.timedelta(minutes=1))
    pre, reset = store.apply_success(new_success, clock())
    assert reset and store.get_job_state(inc.job_key).consecutive_failures == 0


def test_stale_failure_after_success_does_not_regress(store, clock):
    now = clock()
    store.apply_success(make_success(run_id=9, start=now), now)
    late = make_incident(run_id=1, start=now - dt.timedelta(hours=1), end=now - dt.timedelta(minutes=50))
    r = store.claim_incident(late, CB, 900, now, NOSUP)
    assert r.status == "new" and r.consecutive_failures == 0
    assert store.get_job_state(late.job_key).consecutive_failures == 0


def test_action_receipts_and_hourly_count(store, clock):
    assert store.record_action("k1", "github_issue", "url", clock())
    assert not store.record_action("k1", "github_issue", "url", clock())
    assert store.has_action("k1", "github_issue")
    assert store.count_actions_since("github_issue", clock() - dt.timedelta(hours=1)) == 1
    assert store.count_actions_since("github_issue", clock() + dt.timedelta(seconds=1)) == 0


def test_issue_slot_claim_is_exclusive(store, clock):
    inc = make_incident()
    store.claim_incident(inc, CB, 900, clock(), NOSUP)
    assert store.claim_issue_slot(inc.job_key, "tokA", 900, clock())
    assert not store.claim_issue_slot(inc.job_key, "tokB", 900, clock())
    assert not store.set_issue(inc.job_key, "tokB", 7, "u", clock())
    assert store.set_issue(inc.job_key, "tokA", 7, "u", clock())
    clock.tick(seconds=2000)
    assert not store.claim_issue_slot(inc.job_key, "tokC", 900, clock())  # issue exists: no new claim
    st = store.get_job_state(inc.job_key)
    assert st.issue_number == 7


def test_issue_slot_lease_expires_when_claimant_died(store, clock):
    inc = make_incident()
    store.claim_incident(inc, CB, 900, clock(), NOSUP)
    assert store.claim_issue_slot(inc.job_key, "tokA", 900, clock())
    clock.tick(seconds=901)
    assert store.claim_issue_slot(inc.job_key, "tokB", 900, clock())


def test_concurrent_claims_on_one_incident_yield_one_owner(tmp_path):
    path = str(tmp_path / "race.db")
    inc = make_incident()
    results = []
    lock = threading.Lock()

    def worker():
        s = hw.SqliteStore(path)
        r = s.claim_incident(inc, CB, 900, hw.utcnow(), NOSUP)
        with lock:
            results.append(r.status)
        s.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("new") == 1 and results.count("busy") == 7
    s = hw.SqliteStore(path)
    assert s.get_job_state(inc.job_key).consecutive_failures == 1


def test_concurrent_issue_slot_claims_yield_one_winner(tmp_path):
    path = str(tmp_path / "race2.db")
    inc = make_incident()
    hw.SqliteStore(path).claim_incident(inc, CB, 900, hw.utcnow(), NOSUP)
    wins = []
    lock = threading.Lock()

    def worker(i):
        s = hw.SqliteStore(path)
        if s.claim_issue_slot(inc.job_key, f"tok{i}", 900, hw.utcnow()):
            with lock:
                wins.append(i)
        s.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1


def test_watermark_roundtrip(store, clock):
    assert store.get_watermark("reconcile_end") is None
    store.set_watermark("reconcile_end", "2026-01-01T00:00:00+00:00", clock())
    store.set_watermark("reconcile_end", "2026-01-02T00:00:00+00:00", clock())
    assert store.get_watermark("reconcile_end") == "2026-01-02T00:00:00+00:00"


def test_same_pattern_lookup(store, clock):
    for jid in (1, 2, 3):
        store.claim_incident(make_incident(job_id=jid, run_id=jid, job_name=f"job{jid}"), CB, 900, clock(), NOSUP)
    store.claim_incident(make_incident(job_id=4, run_id=4, job_name="job4"), hw.Classification("CONFIG_ERROR", "STAGE_2", "git_checkout_failed"), 900, clock(), NOSUP)
    same = store.jobs_with_same_pattern("python_error", make_incident(job_id=1).job_key)
    assert sorted(j.job_name for j in same) == ["job2", "job3"]
    assert len(store.open_streaks()) == 4
    _ = Clock  # keep import used
