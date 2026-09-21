import healwright_core as hw


def test_redacts_token_shapes():
    text = ("token xoxb-123456789012-abcdefghijkl and ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 and AKIAABCDEFGHIJKLMNOP "  # leak-scan-ok
            "and eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")
    out = hw.redact(text)
    for kind in ("slack_token", "github_token", "aws_key", "jwt"):
        assert f"[REDACTED:{kind}]" in out
    assert "xoxb-" not in out and "ghp_" not in out and "AKIA" not in out


def test_redacts_assignments_and_pii_but_keeps_run_ids():
    text = "password=hunter22 api_key: sk-abcd1234 user someone@example.com ssn 123-45-6789 run_id 903941277636413"
    out = hw.redact(text)
    assert "hunter22" not in out and "sk-abcd1234" not in out
    assert "someone@example.com" not in out and "123-45-6789" not in out
    assert "903941277636413" in out  # long digit run ids are not cards


def test_redacts_pem_and_url_credentials():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"  # leak-scan-ok
    out = hw.redact(f"key {pem} url https://user:pa55@host.example/x")  # leak-scan-ok
    assert "MIIabc" not in out and "[REDACTED:pem]" in out
    assert "pa55" not in out and "https://[REDACTED:url_credentials]@host.example/x" in out


def test_high_entropy_but_not_hex_digests():
    sha = "3f786850e387550fdab836ed7e6dc881de23001b" * 1
    secret = "aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3xY5zA7bC9dE1f"
    out = hw.redact(f"commit {sha} secretish {secret}")
    assert sha in out
    assert secret not in out and "[REDACTED:high_entropy]" in out


def test_extra_patterns():
    out = hw.redact("account ACME-CORP-4711 ok", extra_patterns=[r"ACME-CORP-\d+"])  # leak-scan-ok
    assert "[REDACTED:custom]" in out and "4711" not in out


def test_sanitize_strips_control_chars():
    assert hw.sanitize("a\x00b\x1b[31mc\n\n\n\nd", 100) == "abc\n\nd"
    assert hw.sanitize(None, 10) == "(no message)"
