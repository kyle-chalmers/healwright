import healwright_core as hw
import pytest
from conftest import make_incident

CASES = [
    # (state_message, error_trace, error_message, expected_category, expected_pattern)
    ("Snowflake connection timeout after 300s", "", None, "TRANSIENT", "warehouse_timeout"),
    ("KeyError: 'CUSTOMER_ID'", "", None, "CODE_BUG", "python_error"),
    ("Failed to checkout Git repository: UNAUTHENTICATED", "", None, "CONFIG_ERROR", "git_checkout_failed"),
    ("Workload failed, see run output.", "FileNotFoundError ... read_sftp_csv ... sftp.open(remote_file_path)", None,
     "UPSTREAM_DATA", "sftp_file_missing"),
    ("FileNotFoundError: [Errno 2] No such file or directory: '/tmp/foo.csv'", "", None, "CODE_BUG", "file_error"),
    ("PermissionError: [Errno 13] Permission denied", "", None, "CODE_BUG", "io_error"),
    ("OSError: handle is invalid", "", None, "CODE_BUG", "io_error"),
    ("Workload failed", "IncompatiblePeer: Incompatible ssh peer (no acceptable kex algorithm)", None,
     "CODE_BUG", "sftp_ssh_incompatible_peer"),
    ("", "paramiko.ssh_exception.BadHostKeyException: ...", None, "CODE_BUG", "sftp_ssh_error"),
    ("", "", "APIError: APIError: [503]: The service is currently unavailable.", "TRANSIENT", "http_transient"),
    ("502 Bad Gateway", "", None, "TRANSIENT", "http_transient"),
    ("HTTP 429 rate limit exceeded", "", None, "TRANSIENT", "rate_limit"),
    ("SQL compilation error: invalid identifier 'FOO'", "", None, "CODE_BUG", "sql_error"),
    ("empty DataFrame", "", None, "UPSTREAM_DATA", "empty_data"),
    ("Failed to checkout Git repository: GIT_UNKNOWN_REF: Commit ref refs/heads/x not found", "", None,
     "CONFIG_ERROR", "git_unknown_ref"),
    ("Something completely unexpected happened", "", None, "UNKNOWN", None),
]


@pytest.mark.parametrize("msg,trace,err,category,pattern", CASES)
def test_classification_goldens(classifier, msg, trace, err, category, pattern):
    inc = make_incident(msg=msg, trace=trace, err=err)
    c = classifier.classify(inc)
    assert (c.category, c.pattern) == (category, pattern)


def test_termination_code_wins_stage_one(classifier):
    inc = make_incident(msg="KeyError: 'x'", term="SPOT_INSTANCE_TERMINATION")
    c = classifier.classify(inc)
    assert (c.category, c.stage, c.pattern) == ("TRANSIENT", "STAGE_1", "SPOT_INSTANCE_TERMINATION")


def test_timed_out_result_state_is_transient(classifier):
    inc = make_incident(msg="", result_state="TIMEDOUT")
    assert classifier.classify(inc).category == "TRANSIENT"


def test_config_error_beats_code_bug_on_auth_words(classifier):
    inc = make_incident(msg="Failed to checkout Git repository: authentication failed, invalid credentials")
    assert classifier.classify(inc).category == "CONFIG_ERROR"


COLOURED_TRACE = "\n".join([
    "\x1b[0;31mAPIError\x1b[0m                                  Traceback (most recent call last)",
    "File \x1b[0;32m<command-1>\x1b[0m, line 10",
    "\x1b[0;32m---> 10\x1b[0m spreadsheet \x1b[38;5;241m=\x1b[39m gs_client\x1b[38;5;241m.\x1b[39mopen_by_key(spreadsheet_id)",
    "File \x1b[0;32m/site-packages/sheets_client/client.py:174\x1b[0m, in \x1b[0;36mClient.open_by_key\x1b[0;34m(self, key)\x1b[0m",
    "\x1b[1;32m    172\x1b[0m     \x1b[38;5;28;01mif\x1b[39;00m ex\x1b[38;5;241m.\x1b[39mresponse\x1b[38;5;241m.\x1b[39mstatus_code \x1b[38;5;241m==\x1b[39m HTTPStatus\x1b[38;5;241m.\x1b[39mFORBIDDEN:",
    "\x1b[1;32m    173\x1b[0m         \x1b[38;5;28;01mraise\x1b[39;00m \x1b[38;5;167;01mPermissionError\x1b[39;00m \x1b[38;5;28;01mfrom\x1b[39;00m \x1b[38;5;21;01mex\x1b[39;00m",
    "\x1b[0;32m--> 174\x1b[0m     \x1b[38;5;28;01mraise\x1b[39;00m ex",
    "\x1b[0;31mAPIError\x1b[0m: APIError: [503]: The service is currently unavailable.",
])


def test_quoted_raise_in_library_source_does_not_become_code_bug(classifier):
    inc = make_incident(msg="Workload failed, see run output for details.", trace=COLOURED_TRACE, err=None)
    c = classifier.classify(inc)
    assert (c.category, c.pattern) == ("TRANSIENT", "http_transient")


def test_strip_source_context():
    stripped = hw.strip_source_context(COLOURED_TRACE)
    assert "\x1b" not in stripped
    assert "PermissionError" not in stripped
    assert "raise ex" not in stripped
    assert "[503]: The service is currently unavailable" in stripped
    assert "open_by_key(spreadsheet_id)" in stripped
    assert "could not raise the quota" in hw.strip_source_context("ValueError: could not raise the quota")
    assert hw.strip_source_context(None) is None


def test_smart_truncate_keeps_tail():
    long = "HEAD" + "x" * 20000 + "TAIL_EXCEPTION"
    t = hw.smart_truncate(long, 1000)
    assert len(t) <= 1000 and t.startswith("HEAD") and t.endswith("TAIL_EXCEPTION") and "[TRUNCATED]" in t
    assert hw.smart_truncate(long, 20).endswith("TAIL_EXCEPTION")
    assert hw.smart_truncate(None, 10) == ""


def test_user_patterns_take_priority(tmp_path):
    base = hw._load_structured(hw.os.path.join(hw.os.path.dirname(hw.__file__), "patterns.yaml"))
    spec = hw.merge_pattern_specs(base, {"patterns": {"UPSTREAM_DATA": [{"name": "my_feed_late", "regex": "(?i)daily feed late"}]}})
    c = hw.Classifier(spec)
    assert c.classify_text("daily feed late").pattern == "my_feed_late"


def test_sentinel_tasks_are_ignored(classifier):
    inc = make_incident(msg="KeyError", tasks=False, result_state="SUCCESS_WITH_FAILURES")
    inc.tasks.append(hw.TaskFailure(hw.PROPAGATE_TASK_KEY, "FAILED", 1, "RuntimeError: propagate"))
    assert inc.failed_tasks == []
