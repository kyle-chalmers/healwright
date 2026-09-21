import copy
import json
import os

import install_trigger as it
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def load_before():
    with open(os.path.join(HERE, "fixtures", "job_before.json"), encoding="utf-8") as fh:
        return json.load(fh)["settings"]


def test_patch_appends_healer_depending_on_every_task():
    before = load_before()
    after = it.patch_settings(before, 999)
    keys = [t["task_key"] for t in after["tasks"]]
    assert keys == ["extract", "transform", "load", it.HEALER_TASK_KEY]
    healer = after["tasks"][-1]
    assert sorted(d["task_key"] for d in healer["depends_on"]) == ["extract", "load", "transform"]
    assert healer["run_if"] == "AT_LEAST_ONE_FAILED"
    assert healer["run_job_task"]["job_id"] == 999
    assert healer["run_job_task"]["job_parameters"]["parent_run_id"] == "{{job.run_id}}"
    assert healer["run_job_task"]["job_parameters"]["mode"] == "leaf-task"
    assert "new_cluster" not in healer and "job_cluster_key" not in healer  # Run Job tasks need no compute
    assert healer["max_retries"] == 0
    # original tasks untouched
    assert after["tasks"][:3] == before["tasks"]
    assert before["tasks"][-1]["task_key"] == "load"  # input not mutated


def test_patch_matches_golden_fixture():
    before = load_before()
    after = it.patch_settings(before, 999, it.parse_propagate_compute("job_cluster_key:small"), "jobs/self_healing/propagate.py")
    golden_path = os.path.join(HERE, "fixtures", "job_after.json")
    if not os.path.exists(golden_path):  # first run writes the golden; reviewed and committed
        with open(golden_path, "w", encoding="utf-8") as fh:
            json.dump(after, fh, indent=2, sort_keys=True)
            fh.write("\n")
    with open(golden_path, encoding="utf-8") as fh:
        assert json.load(fh) == json.loads(json.dumps(after))


def test_patch_is_idempotent():
    before = load_before()
    once = it.patch_settings(before, 999)
    twice = it.patch_settings(once, 999)
    assert once == twice
    assert it.is_installed(once) and not it.is_installed(before)


def test_propagate_task_shapes():
    before = load_before()
    for spec, expected in [("serverless:hw", {"environment_key": "hw"}), ("job_cluster_key:small", {"job_cluster_key": "small"}),
                           ("existing_cluster_id:abc-123", {"existing_cluster_id": "abc-123"})]:
        after = it.patch_settings(before, 999, it.parse_propagate_compute(spec), "jobs/self_healing/propagate.py")
        prop = after["tasks"][-1]
        assert prop["task_key"] == it.PROPAGATE_TASK_KEY
        assert prop["run_if"] == "AT_LEAST_ONE_FAILED" and prop["max_retries"] == 0
        assert sorted(d["task_key"] for d in prop["depends_on"]) == ["extract", "load", "transform"]
        assert prop["spark_python_task"] == {"python_file": "jobs/self_healing/propagate.py", "source": "GIT"}
        for k, v in expected.items():
            assert prop[k] == v
    assert it.parse_propagate_compute("none") is None
    with pytest.raises(it.InstallError):
        it.parse_propagate_compute("bogus:thing")
    with pytest.raises(it.InstallError):
        it.patch_settings(before, 999, {"environment_key": "x"}, None)


def test_unpatch_returns_fields_to_remove_and_original_tasks():
    before = load_before()
    after = it.patch_settings(before, 999, {"environment_key": "x"}, "p.py")
    restored, fields = it.unpatch_settings(after)
    assert restored["tasks"] == before["tasks"]
    assert fields == ["tasks/__healwright", "tasks/__healwright_propagate"]
    assert it.unpatch_settings(before)[1] == []


def test_preflight_blocks_bad_targets():
    before = load_before()
    errors, warnings = it.preflight(before, 999, "healwright-healer", job_id=111)
    assert errors == [] and warnings == []
    errors, _ = it.preflight(dict(before, name="healwright-healer"), 999, "healwright-healer")
    assert any("healer job itself" in e for e in errors)
    errors, _ = it.preflight(before, 111, "healwright-healer", job_id=111)
    assert any("healer_job_id" in e for e in errors)
    errors, _ = it.preflight(dict(before, tasks=[]), 999)
    assert any("no tasks" in e for e in errors)
    already = copy.deepcopy(before)
    already["tasks"].append({"task_key": "x", "run_job_task": {"job_id": 999}})
    errors, warnings = it.preflight(already, 999)
    assert any("already calls the healer" in e for e in errors) and any("nesting" in w for w in warnings)
    handler = copy.deepcopy(before)
    handler["tasks"][2]["run_if"] = "ALL_DONE"
    _, warnings = it.preflight(handler, 999)
    assert any("run_if=ALL_DONE" in w for w in warnings)


def test_fingerprint_detects_drift():
    before = load_before()
    fp = it.fingerprint(before)
    drifted = copy.deepcopy(before)
    drifted["tasks"][0]["max_retries"] = 2
    assert it.fingerprint(drifted) != fp
    assert it.fingerprint(json.loads(json.dumps(before))) == fp


def test_permission_check():
    perms = {"access_control_list": [
        {"service_principal_name": "sp-example", "all_permissions": [{"permission_level": "CAN_MANAGE_RUN"}]},
        {"group_name": "admins", "all_permissions": [{"permission_level": "CAN_MANAGE"}]}]}
    ok, _ = it.check_permission(perms, "sp-example")
    assert ok is True
    ok, msg = it.check_permission(perms, "sp-other")
    assert ok is False and "CAN_MANAGE_RUN" in msg
    assert it.check_permission(perms, None)[0] is None


class FakeJobs:
    def __init__(self, settings, drift=False):
        self._settings = settings
        self.reads = 0
        self.updates = []
        self.drift = drift

    def get(self, job_id):
        self.reads += 1
        s = copy.deepcopy(self._settings)
        if self.drift:
            s["tasks"][0]["max_retries"] = 5

        class S:
            def as_dict(self_inner):
                return s

        class J:
            settings = S()

        return J()

    def update(self, job_id, new_settings=None, fields_to_remove=None):
        self.updates.append({"job_id": job_id, "new_settings": new_settings.as_dict() if new_settings else None, "fields_to_remove": fields_to_remove})


class FakeW:
    def __init__(self, jobs):
        self.jobs = jobs


def test_apply_update_aborts_on_drift(monkeypatch):
    pytest.importorskip("databricks.sdk")
    before = load_before()
    w = FakeW(FakeJobs(before, drift=True))
    fp = it.fingerprint(before)
    new = it.patch_settings(before, 999)
    with pytest.raises(it.InstallError, match="drift"):
        it.apply_update(w, 111, new, fp)
    assert w.jobs.updates == []


def test_apply_update_sends_only_tasks():
    pytest.importorskip("databricks.sdk")
    before = load_before()
    w = FakeW(FakeJobs(before))
    new = it.patch_settings(before, 999)
    it.apply_update(w, 111, new, it.fingerprint(before))
    sent = w.jobs.updates[0]["new_settings"]
    assert set(sent.keys()) == {"tasks"}
    assert [t["task_key"] for t in sent["tasks"]][-1] == it.HEALER_TASK_KEY


def test_apply_uninstall_uses_fields_to_remove():
    w = FakeW(FakeJobs(load_before()))
    it.apply_uninstall(w, 111, ["tasks/__healwright"])
    assert w.jobs.updates == [{"job_id": 111, "new_settings": None, "fields_to_remove": ["tasks/__healwright"]}]


def test_cli_offline_dry_run(tmp_path, capsys):
    out = tmp_path / "after.json"
    rc = it.main(["--job-json", os.path.join(HERE, "fixtures", "job_before.json"), "--healer-job-id", "999", "--out", str(out)])
    assert rc == 0
    printed = capsys.readouterr().out
    assert "+++ " in printed and "__healwright" in printed and "nothing written" in printed
    assert it.is_installed(json.loads(out.read_text()))
    rc = it.main(["--job-json", str(out), "--healer-job-id", "999", "--uninstall"])
    assert rc == 0 and "fields_to_remove" in capsys.readouterr().out


def test_cli_refuses_apply_without_tty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = it.main(["--job-id", "1", "--healer-job-id", "999", "--apply"])
    assert rc == 2


def test_sentinel_removals_when_reinstalling_without_propagate():
    before = load_before()
    with_prop = it.patch_settings(before, 999, {"environment_key": "x"}, "p.py")
    without = it.patch_settings(with_prop, 999)
    assert it.sentinel_removals(with_prop, without) == ["tasks/__healwright_propagate"]
    assert it.sentinel_removals(before, without) == []


def test_apply_update_names_removed_sentinels():
    pytest.importorskip("databricks.sdk")
    before = load_before()
    live = it.patch_settings(before, 999, {"environment_key": "x"}, "p.py")
    w = FakeW(FakeJobs(live))
    new = it.patch_settings(live, 999)
    it.apply_update(w, 111, new, it.fingerprint(live))
    assert w.jobs.updates[0]["fields_to_remove"] == ["tasks/__healwright_propagate"]



def test_get_job_prefers_effective_run_as_identity():
    pytest.importorskip("databricks.sdk")

    class J:
        run_as_user_name = "sp-effective"
        creator_user_name = "someone-else"

        class settings:  # noqa: N801
            @staticmethod
            def as_dict():
                return {"name": "x", "tasks": []}

    class Jobs:
        def get(self, job_id):
            return J()

    settings, identity = it._get_job(FakeW(Jobs()), 1)
    assert identity == "sp-effective" and settings["name"] == "x"
