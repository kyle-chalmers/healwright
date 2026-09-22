"""Structural guarantees of the reusable fix workflow (docs/guardrails.md)."""
import os
import re

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, ".github", "workflows", "fix.yml")
CALLER = os.path.join(ROOT, "examples", "caller-workflow.yml")


def load(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def job_steps(job):
    wf = load(FIX)
    return {s.get("id") or s.get("name") or s.get("uses"): s for s in wf["jobs"][job]["steps"]}, wf


def test_is_reusable_and_serialised_per_issue():
    _, wf = job_steps("investigate")
    on = wf.get("on") or wf.get(True)
    assert "workflow_call" in on
    assert set(on["workflow_call"]["secrets"]) >= {"app_id", "app_private_key"}
    assert on["workflow_call"]["inputs"]["bot_login"]["required"] is True
    assert "issue_number" in wf["concurrency"]["group"]
    assert wf["concurrency"]["cancel-in-progress"] is False
    assert wf["permissions"] == {}
    assert set(wf["jobs"]) == {"gate", "investigate", "publish"}
    assert wf["jobs"]["investigate"]["needs"] == "gate"
    assert wf["jobs"]["publish"]["needs"] == ["gate", "investigate"]
    assert "needs.gate.result == 'success'" in wf["jobs"]["publish"]["if"]


def test_untrusted_job_never_holds_any_write_scope():
    inv, wf = job_steps("investigate")
    assert wf["jobs"]["investigate"]["permissions"] == {"contents": "read", "issues": "read"}
    ro = inv["ro-token"]["with"]
    assert all(v == "read" for k, v in ro.items() if k.startswith("permission-")), ro
    text = yaml.safe_dump(wf["jobs"]["investigate"])
    assert "permission-contents: write" not in text and "rw-token" not in text
    claude = inv["claude"]
    assert claude["with"]["github_token"] == "${{ steps.ro-token.outputs.token }}"
    assert claude["with"]["allowed_bots"] == "${{ inputs.bot_login }}"
    assert claude.get("continue-on-error") is True
    assert claude["with"]["show_full_output"] == "${{ inputs.debug_output }}"
    args = claude["with"]["claude_args"]
    allowed = re.search(r'--allowed-tools "([^"]+)"', args).group(1).split(",")
    assert allowed == ["Read", "Grep", "Glob", "Edit(${{ needs.gate.outputs.allowed_folder }}**)", "Edit(.healwright-out/summary.md)"]
    disallowed = re.search(r'--disallowedTools "([^"]+)"', args).group(1).split(",")
    assert {"Bash", "Write", "WebFetch", "WebSearch", "NotebookEdit"} <= set(disallowed)
    assert "runner.temp" not in args
    assert "CLAUDE_BRANCH" not in str(claude)
    assert "Do NOT push" in claude["with"]["prompt"]
    assert "issue.md" in claude["with"]["prompt"] and "history.txt" in claude["with"]["prompt"]
    prep = inv["Prepare issue text, history, and the summary file"]["run"]
    assert "issue.md" in prep and '"log"' in prep and "gh" in prep


def test_only_a_patch_artifact_crosses_the_boundary():
    inv, _ = job_steps("investigate")
    order = list(inv)
    assert order.index("claude") < order.index("package") < order.index("Upload patch artifact")
    pkg = inv["package"]["run"]
    assert "diff" in pkg and "core.hooksPath=/dev/null" in pkg and ':!.healwright-out' in pkg and "REDACTED" in pkg
    assert inv["Upload patch artifact"]["with"]["retention-days"] == 1
    # nothing after the AI step in the untrusted job runs in bash or holds a token
    for name in order[order.index("claude") + 1:]:
        s = inv[name]
        if s.get("run"):
            assert s.get("shell", "").startswith("python -I"), name
        assert "token" not in yaml.safe_dump(s.get("env") or {}).lower(), name


def test_gate_snapshots_the_folder_before_the_ai_runs():
    gate, wf = job_steps("gate")
    assert "gh" in gate["issue"]["run"] and "Notebook Path" in gate["issue"]["run"]
    assert wf["jobs"]["gate"]["outputs"]["allowed_folder"] == "${{ steps.issue.outputs.allowed_folder }}"
    # the AI job and the publish job both consume the gate's snapshot, never the live issue
    inv_text = yaml.safe_dump(wf["jobs"]["investigate"])
    pub_text = yaml.safe_dump(wf["jobs"]["publish"])
    assert "needs.gate.outputs.allowed_folder" in inv_text and "needs.gate.outputs.allowed_folder" in pub_text
    assert "gh api" not in pub_text  # publish never re-reads the (mutable) issue for authority
    assert "needs.investigate.outputs.allowed_folder" not in pub_text


def test_trusted_job_validates_then_pushes():
    pub, wf = job_steps("publish")
    order = list(pub)
    assert order.index("Fresh checkout") < order.index("download") < order.index("scope") < order.index("rw-token") < order.index("pr")
    assert pub["Fresh checkout"]["with"]["persist-credentials"] is False
    scope = pub["scope"]["run"]
    assert "diff --git a/" in scope and "apply" in scope and "--check" in scope and "realpath" in scope
    assert '".github/", ".claude/", ".git/", ".healwright-out/"' in scope
    rw = pub["rw-token"]["with"]
    assert rw["permission-contents"] == "write" and rw["permission-pull-requests"] == "write"
    pr = pub["pr"]
    assert "scope_ok == 'true'" in pr["if"] and "has_changes == 'true'" in pr["if"] and "needs.gate.outputs.ok == 'true'" in pr["if"]
    assert "--draft" in pr["run"] and "core.hooksPath=/dev/null" in pr["run"]
    assert pr["env"]["GH_TOKEN"] == "${{ steps.rw-token.outputs.token }}"
    assert "Comment when there is no PR" in pub and "add-label" in pub["Comment when there is no PR"]["run"]
    red = pub["summary"]["run"]
    assert "REDACTED" in red and "```text" in red and "[:20000]" in red
    assert "high_entropy" in red and "ssn" in red and 'replace("`", "\'")' in red
    assert order.index("summary") < order.index("pr")
    assert "Fail the run on a scope violation" in pub
    assert wf["jobs"]["publish"]["env"]["ALLOWED_FOLDER"] == "${{ needs.gate.outputs.allowed_folder }}"


def test_untrusted_context_never_inlined_in_run():
    for job in ("gate", "investigate", "publish"):
        st, _ = job_steps(job)
        for name, s in st.items():
            run = s.get("run") or ""
            assert "${{ github.event" not in run, name
            assert "${{ inputs." not in run, name
            assert "${{ steps." not in run, name
            assert "${{ needs." not in run, name


def test_outputs_use_random_heredoc_delimiters():
    for job, names in (("gate", ("issue",)), ("publish", ("scope",))):
        st, _ = job_steps(job)
        for name in names:
            assert "<<EOF" not in st[name]["run"], name
            assert "uuid" in st[name]["run"], name


def test_every_action_is_sha_pinned_including_anthropic():
    with open(FIX, encoding="utf-8") as fh:
        for line in fh:
            m = re.search(r"uses:\s*(\S+)@(\S+)", line)
            if m:
                assert re.fullmatch(r"[0-9a-f]{40}", m.group(2)), line


def test_every_job_sets_up_python_for_self_hosted_runners():
    _, wf = job_steps("gate")
    for job in ("gate", "investigate", "publish"):
        uses = [s.get("uses", "") for s in wf["jobs"][job]["steps"]]
        assert any(u.startswith("actions/setup-python@") for u in uses), job


def test_caller_example_is_short_and_points_at_the_reusable_workflow():
    wf = load(CALLER)
    job = wf["jobs"]["fix"]
    assert job["uses"].startswith("kyle-chalmers/healwright/.github/workflows/fix.yml@")
    assert job["if"] == "github.event.label.name == 'self-healing-fix'"
    assert wf["permissions"] == {"contents": "read", "issues": "read"}
    assert set(job["secrets"]) >= {"app_id", "app_private_key"}
    with open(CALLER, encoding="utf-8") as fh:
        assert len([ln for ln in fh if ln.strip() and not ln.strip().startswith("#")]) <= 25


def test_prompt_and_gate_agree_on_folder_regex():
    with open(FIX, encoding="utf-8") as fh:
        text = fh.read()
    assert text.count(r"[A-Za-z0-9][A-Za-z0-9._-]*") == 1  # the gate job is the single derivation
