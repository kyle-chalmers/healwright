"""healwright_core: the reference self-healing job, in one importable file.

Copy the folder this file lives in (templates/healer/) into your jobs repo. The notebook
`healer.py` imports this module; `tests/` in the healwright repo exercise it; you can vendor it
and change it, because the record of how a failure was handled must outlive the tool.

Pipeline (docs/architecture.md):

    source -> RunIncident -> classify -> store.claim (lease) -> policy -> actions (receipts)

Sources
  leaf-task   a Run Job leaf task appended to each monitored job (run_if AT_LEAST_ONE_FAILED)
              calls the shared healer job with the parent's job id / run id. Trigger-based.
  reconcile   a low-frequency sweep of completed runs from a durable high-water mark.
  simulate    a fabricated incident for local testing.

Only the standard library is required. `databricks-sdk` (preinstalled on Databricks runtimes)
is imported lazily by DatabricksPlatform. PyYAML is used when config/patterns are YAML; JSON
works without it. `PyJWT` + `cryptography` are needed only for GitHub App authentication.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import fnmatch
import hashlib
import json
import math
import os
import re
import secrets as _secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

__version__ = "0.1.0"

UTC = dt.timezone.utc

# Task keys the installer adds. Their failures are never classified: the propagate task fails
# on purpose, and the healer task is us.
HEALER_TASK_KEY = "__healwright"
PROPAGATE_TASK_KEY = "__healwright_propagate"
SENTINEL_TASK_KEYS = frozenset({HEALER_TASK_KEY, PROPAGATE_TASK_KEY})

CATEGORIES = ("TRANSIENT", "UPSTREAM_DATA", "CONFIG_ERROR", "CODE_BUG", "UNKNOWN")

INCIDENT_MARKER = "healwright:incident:"


class HealwrightError(Exception):
    """Base class for configuration and integration errors."""


class ConfigError(HealwrightError):
    pass


# --------------------------------------------------------------------------------------------
# Time helpers. Everything is timezone-aware UTC. Stores persist ISO-8601 strings so SQLite,
# Delta and any future backend agree byte-for-byte; the mixed NTZ/UTC trap is not repeated.
# --------------------------------------------------------------------------------------------


def utcnow() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso(t: dt.datetime | None) -> str | None:
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    if isinstance(s, dt.datetime):
        return s if s.tzinfo else s.replace(tzinfo=UTC)
    t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def from_epoch_ms(ms: int | None) -> dt.datetime | None:
    if not ms:
        return None
    return dt.datetime.fromtimestamp(ms / 1000, tz=UTC)


# --------------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "platform": "databricks",
    "tenant": "",
    "healer_job_name": "healwright-healer",
    "jobs": {"include": ["*"], "exclude": [], "exclude_healer": True},
    "state": {"backend": "sqlite", "sqlite_path": "healwright.db", "delta_schema": "main.healwright"},
    "patterns_file": "patterns.yaml",
    "policy": {
        "dedup_window_hours": 4,
        "thread_reset_hours": 24,
        "max_alerts_per_run": 10,
        "max_issues_per_hour": 5,
        "escalation_threshold": 3,
        "lease_seconds": 900,
        "issue_categories": ["CODE_BUG"],
        "include_raw_trace": False,
        "shadow": False,
    },
    "redaction": {"extra_patterns": []},
    "slack": {"enabled": False, "channel": "", "bot_token": ""},
    "github": {
        "enabled": False,
        "repo": "",
        "api_base": "https://api.github.com",
        "auth": {"type": "token", "token": ""},
        "label": "self-healing-fix",
        "needs_human_label": "needs-human",
        "source_root": "jobs",
        "repo_checkout": "",
    },
    "reconcile": {"overlap_minutes": 30, "initial_lookback_minutes": 120, "max_window_minutes": 4320, "max_run_hours": 24},
}

_REF_RE = re.compile(r"^\$\{(env|secret):([^}]+)\}$")


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def resolve_refs(obj: Any, secret_getter: Callable[[str, str], str] | None = None) -> Any:
    """Replace `${env:NAME}` and `${secret:scope/key}` strings, recursively.

    Unresolvable references become "" rather than raising, so a config that mentions Slack can
    still load with Slack disabled. Anything that needs the value checks for emptiness.
    """
    if isinstance(obj, dict):
        return {k: resolve_refs(v, secret_getter) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_refs(v, secret_getter) for v in obj]
    if isinstance(obj, str):
        m = _REF_RE.match(obj.strip())
        if not m:
            return obj
        kind, ref = m.group(1), m.group(2)
        if kind == "env":
            return os.environ.get(ref, "")
        if secret_getter is None:
            return ""
        scope, _, key = ref.partition("/")
        try:
            return secret_getter(scope, key) or ""
        except Exception:
            return ""
    return obj


def _load_structured(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith(".json"):
        return json.loads(text)
    try:
        import yaml  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise ConfigError(f"PyYAML is required to read {path}; or use JSON") from e
    return yaml.safe_load(text) or {}


def load_config(path: str | None = None, overrides: dict | None = None,
                secret_getter: Callable[[str, str], str] | None = None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path:
        cfg = deep_merge(cfg, _load_structured(path))
        base_dir = os.path.dirname(os.path.abspath(path))
        pf = cfg.get("patterns_file") or "patterns.yaml"
        if not os.path.isabs(pf):
            cfg["patterns_file"] = os.path.join(base_dir, pf)
    if overrides:
        cfg = deep_merge(cfg, overrides)
    return resolve_refs(cfg, secret_getter)


# --------------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------------


@dataclass
class TaskFailure:
    task_key: str
    result_state: str = "FAILED"
    task_run_id: int | None = None
    state_message: str = ""
    termination_code: str | None = None
    error_message: str | None = None
    error_trace: str | None = None
    notebook_path: str | None = None
    start_time: dt.datetime | None = None
    end_time: dt.datetime | None = None
    attempt_number: int = 0


@dataclass
class RunIncident:
    """One failed run of one job. Task failures hang off it as children."""

    platform: str
    workspace_id: str
    job_id: int
    job_name: str
    run_id: int
    source: str
    result_state: str = "FAILED"
    life_cycle_state: str = ""
    state_message: str = ""
    termination_code: str | None = None
    error_message: str | None = None
    error_trace: str | None = None
    notebook_path: str | None = None
    run_start_time: dt.datetime | None = None
    run_end_time: dt.datetime | None = None
    workspace_url: str | None = None
    tenant: str = ""
    parent_run_id: int | None = None
    tasks: list[TaskFailure] = field(default_factory=list)

    @property
    def key(self) -> str:
        return incident_key(self.platform, self.tenant, self.workspace_id, self.job_id, self.run_id)

    @property
    def job_key(self) -> str:
        return job_key(self.platform, self.tenant, self.workspace_id, self.job_id)

    @property
    def run_url(self) -> str | None:
        if not self.workspace_url:
            return None
        return f"{self.workspace_url.rstrip('/')}/#job/{self.job_id}/run/{self.run_id}"

    @property
    def failed_tasks(self) -> list[TaskFailure]:
        return [t for t in self.tasks if t.task_key not in SENTINEL_TASK_KEYS]

    @property
    def primary_task(self) -> TaskFailure | None:
        return self.failed_tasks[0] if self.failed_tasks else None

    @property
    def effective_termination_code(self) -> str | None:
        if self.termination_code:
            return self.termination_code
        for t in self.failed_tasks:
            if t.termination_code:
                return t.termination_code
        return None

    @property
    def effective_notebook_path(self) -> str | None:
        if self.notebook_path:
            return self.notebook_path
        for t in self.failed_tasks:
            if t.notebook_path:
                return t.notebook_path
        return None

    def search_text(self) -> str:
        """Text the Stage 2 regexes run against, in signal-first order."""
        parts = [self.state_message or "", self.error_message or ""]
        for t in self.failed_tasks:
            parts.append(t.state_message or "")
            parts.append(t.error_message or "")
        parts.append(strip_source_context(self.error_trace) or "")
        for t in self.failed_tasks:
            parts.append(strip_source_context(t.error_trace) or "")
        return "\n".join(p for p in parts if p)

    def combined_trace(self) -> str:
        parts = [self.error_trace or ""] + [t.error_trace or "" for t in self.failed_tasks]
        return "\n".join(p for p in parts if p)


@dataclass
class RunSuccess:
    platform: str
    workspace_id: str
    job_id: int
    job_name: str
    run_id: int
    source: str = "reconcile"
    run_start_time: dt.datetime | None = None
    run_end_time: dt.datetime | None = None
    tenant: str = ""

    @property
    def key(self) -> str:
        return incident_key(self.platform, self.tenant, self.workspace_id, self.job_id, self.run_id)

    @property
    def job_key(self) -> str:
        return job_key(self.platform, self.tenant, self.workspace_id, self.job_id)


@dataclass
class Classification:
    category: str
    stage: str
    pattern: str | None


@dataclass
class JobState:
    job_key: str
    job_id: int
    job_name: str
    consecutive_failures: int = 0
    last_run_id: int | None = None
    last_status: str | None = None
    last_failure_time: dt.datetime | None = None
    last_success_time: dt.datetime | None = None
    last_classification: str | None = None
    last_error_pattern: str | None = None
    thread_ts: str | None = None
    thread_started_at: dt.datetime | None = None
    issue_number: int | None = None
    issue_url: str | None = None
    issue_claim_token: str | None = None
    issue_claim_at: dt.datetime | None = None
    fix_notified_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None


@dataclass
class ClaimResult:
    status: str  # new | resumed | busy | done
    incident_key: str
    consecutive_failures: int = 0
    alert_suppressed: bool = False
    suppress_reason: str = ""
    owner: str = ""  # lease token; complete/release are conditional on it


def incident_key(platform: str, tenant: str, workspace_id: Any, job_id: Any, run_id: Any) -> str:
    raw = f"{platform}|{tenant or ''}|{workspace_id}|{job_id}|{run_id}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def job_key(platform: str, tenant: str, workspace_id: Any, job_id: Any) -> str:
    return f"{platform}|{tenant or ''}|{workspace_id}|{job_id}"


# --------------------------------------------------------------------------------------------
# Text: ANSI, traceback source lines, truncation, sanitising, redaction
# --------------------------------------------------------------------------------------------

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# A traceback line that merely QUOTES a `raise` statement (gutter, arrow, line number, then
# `raise`). Libraries' own error-handling source otherwise pollutes the search text.
QUOTED_RAISE_RE = re.compile(r"^\s*(?:-+>\s*)?\d*\s*raise\b")


def strip_ansi(text: str | None) -> str | None:
    if not text:
        return text
    return ANSI_ESCAPE_RE.sub("", text)


def strip_source_context(trace: str | None) -> str | None:
    """Drop traceback lines that quote a `raise` statement; strip ANSI colour codes.

    A transient 503 from a Sheets client once classified as CODE_BUG because the library's own
    `raise PermissionError from ex` source line sat in the traceback. The terminal exception line
    survives this filter; only quoted `raise` statements go.
    """
    if not trace:
        return trace
    kept = []
    for line in trace.splitlines():
        plain = ANSI_ESCAPE_RE.sub("", line)
        if QUOTED_RAISE_RE.match(plain):
            continue
        kept.append(plain)
    return "\n".join(kept)


def smart_truncate(text: str | None, max_length: int, head_ratio: float = 0.3) -> str:
    """Keep the head (context) and the tail (the exception), with a marker between."""
    if not text or len(text) <= max_length:
        return text or ""
    marker = "\n... [TRUNCATED] ...\n"
    head_chars = int(max_length * head_ratio)
    tail_chars = max_length - head_chars - len(marker)
    if tail_chars <= 0:
        return text[-max_length:]
    return f"{text[:head_chars]}{marker}{text[-tail_chars:]}"


def sanitize(text: str | None, max_length: int) -> str:
    """Strip control characters, collapse blank runs, truncate head+tail."""
    if not text:
        return "(no message)"
    cleaned = re.sub(r"[\x00-\x09\x0b-\x0c\x0e-\x1f\x7f]", "", strip_ansi(str(text)) or "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return smart_truncate(cleaned, max_length) if len(cleaned) > max_length else cleaned


# Shapes, never values. Order matters: specific tokens before the generic assignment rule.
REDACTION_RULES: list[tuple[str, str]] = [
    ("pem", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ("slack_token", r"xox[baprs]-[0-9A-Za-z-]{10,}"),
    ("github_token", r"\bgh[pousr]_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    ("aws_key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ("bearer", r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    ("assignment", r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|client[_-]?secret)\b(\s*[:=]\s*|\s*[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]{4,}"),
    ("url_credentials", r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"),
    ("email", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ("ssn", r"\b\d{3}-\d{2}-\d{4}\b"),
    ("card", r"\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{1,7}\b"),
]
_HIGH_ENTROPY_RE = re.compile(r"\b[A-Za-z0-9_\-/+=]{32,}\b")


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _looks_like_secret(token: str) -> bool:
    """Long, mixed-class, high-entropy strings. Hex digests and UUIDs are left alone."""
    if re.fullmatch(r"[0-9a-fA-F]+", token) or re.fullmatch(r"[0-9a-fA-F-]{36}", token):
        return False
    has_upper = any(c.isupper() for c in token)
    has_lower = any(c.islower() for c in token)
    has_digit = any(c.isdigit() for c in token)
    if not (has_upper and has_lower and has_digit):
        return False
    return _shannon_entropy(token) >= 4.2


def redact(text: str | None, extra_patterns: Iterable[str] = ()) -> str:
    """Replace secret- and PII-shaped substrings with `[REDACTED:<kind>]`.

    Runs before anything leaves the healer (Slack, GitHub, logs of dry runs). It is a filter on
    shapes, so it will occasionally over-redact; that is the correct failure mode for a
    public-repo issue body.
    """
    if not text:
        return text or ""
    out = str(text)
    for label, pat in REDACTION_RULES:
        if label == "assignment":
            out = re.sub(pat, lambda m: f"{m.group(1)}{m.group(2)}[REDACTED:assignment]", out)
        elif label == "url_credentials":
            out = re.sub(pat, lambda m: f"{m.group(1)}[REDACTED:url_credentials]@", out)
        else:
            out = re.sub(pat, f"[REDACTED:{label}]", out)
    for pat in extra_patterns or ():
        out = re.sub(pat, "[REDACTED:custom]", out)
    out = _HIGH_ENTROPY_RE.sub(lambda m: "[REDACTED:high_entropy]" if _looks_like_secret(m.group(0)) else m.group(0), out)
    return out


# --------------------------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------------------------


class Classifier:
    def __init__(self, spec: dict):
        self.order: list[str] = list(spec.get("order") or ["TRANSIENT", "UPSTREAM_DATA", "CONFIG_ERROR", "CODE_BUG"])
        self.result_states: dict[str, str] = dict(spec.get("result_states") or {})
        self.termination_codes: dict[str, str] = {}
        for category, codes in (spec.get("termination_codes") or {}).items():
            for code in codes or []:
                self.termination_codes[str(code)] = category
        self.patterns: dict[str, list[tuple[re.Pattern[str], str]]] = {}
        for category, entries in (spec.get("patterns") or {}).items():
            compiled = []
            for entry in entries or []:
                compiled.append((re.compile(entry["regex"]), entry["name"]))
            self.patterns[category] = compiled

    @classmethod
    def from_file(cls, path: str, extra: dict | None = None) -> Classifier:
        spec = _load_structured(path)
        if extra:
            spec = merge_pattern_specs(spec, extra)
        return cls(spec)

    def classify(self, incident: RunIncident) -> Classification:
        code = incident.effective_termination_code
        if code and code in self.termination_codes:
            return Classification(self.termination_codes[code], "STAGE_1", code)
        rs = str(incident.result_state or "")
        for state, category in self.result_states.items():
            if state in rs:
                return Classification(category, "STAGE_1", state)
        text = incident.search_text()
        for category in self.order:
            for rx, name in self.patterns.get(category, []):
                if rx.search(text):
                    return Classification(category, "STAGE_2", name)
        return Classification("UNKNOWN", "STAGE_2", None)

    def classify_text(self, text: str, result_state: str = "FAILED", termination_code: str | None = None) -> Classification:
        inc = RunIncident("test", "0", 0, "test", 0, "simulate", result_state=result_state,
                          state_message=text, termination_code=termination_code)
        return self.classify(inc)


def merge_pattern_specs(base: dict, extra: dict) -> dict:
    out = json.loads(json.dumps(base))
    for category, entries in (extra.get("patterns") or {}).items():
        out.setdefault("patterns", {}).setdefault(category, [])
        # user patterns take priority within their category
        out["patterns"][category] = list(entries or []) + out["patterns"][category]
    for category, codes in (extra.get("termination_codes") or {}).items():
        out.setdefault("termination_codes", {}).setdefault(category, [])
        out["termination_codes"][category] = list(codes or []) + out["termination_codes"][category]
    if extra.get("order"):
        out["order"] = extra["order"]
    return out


# --------------------------------------------------------------------------------------------
# State store
# --------------------------------------------------------------------------------------------

_INCIDENT_COLS = [
    "incident_key", "platform", "tenant", "workspace_id", "job_id", "job_name", "run_id", "source",
    "result_state", "termination_code", "classification", "stage", "matched_pattern", "state_message",
    "error_trace", "run_start_time", "run_end_time", "status", "lease_until", "attempts",
    "consecutive_failures", "alert_suppressed", "suppress_reason", "created_at", "updated_at", "owner",
]


class StateStore:
    """Contract every backend implements. Timestamps are ISO-8601 UTC strings on disk."""

    def ensure_schema(self) -> None:
        raise NotImplementedError

    # incidents
    def claim_incident(self, incident: RunIncident, classification: Classification, lease_seconds: int,
                       now: dt.datetime, decide_suppress: Callable[[JobState | None], tuple[bool, str]]) -> ClaimResult:
        raise NotImplementedError

    def complete_incident(self, key: str, now: dt.datetime, owner: str = "") -> None:
        """Mark done. With `owner`, only if we still hold the lease (an expired worker cannot close a lease another worker took over)."""
        raise NotImplementedError

    def release_incident(self, key: str, now: dt.datetime, owner: str = "") -> None:
        """Give an incident back (status pending, no lease) so the next source retries its actions. Conditional on `owner` when given."""
        raise NotImplementedError

    def get_incident(self, key: str) -> dict | None:
        raise NotImplementedError

    # action receipts
    def record_action(self, key: str, action: str, detail: str | None, now: dt.datetime) -> bool:
        raise NotImplementedError

    def has_action(self, key: str, action: str) -> bool:
        raise NotImplementedError

    def count_actions_since(self, action: str, since: dt.datetime) -> int:
        raise NotImplementedError

    # per-job state
    def get_job_state(self, jkey: str) -> JobState | None:
        raise NotImplementedError

    def apply_success(self, success: RunSuccess, now: dt.datetime) -> tuple[JobState | None, bool]:
        raise NotImplementedError

    def set_thread(self, jkey: str, thread_ts: str, now: dt.datetime) -> None:
        raise NotImplementedError

    def claim_issue_slot(self, jkey: str, token: str, lease_seconds: int, now: dt.datetime) -> bool:
        raise NotImplementedError

    def set_issue(self, jkey: str, token: str, number: int, url: str, now: dt.datetime) -> bool:
        raise NotImplementedError

    def release_issue_slot(self, jkey: str, token: str) -> None:
        raise NotImplementedError

    def clear_issue(self, jkey: str, number: int) -> bool:
        """Forget a tracked issue, only if it is still the one we saw (conditional on its number)."""
        raise NotImplementedError

    def mark_fix_notified(self, jkey: str, now: dt.datetime) -> None:
        raise NotImplementedError

    def jobs_awaiting_fix_notice(self) -> list[JobState]:
        raise NotImplementedError

    def jobs_with_same_pattern(self, pattern: str, exclude_jkey: str) -> list[JobState]:
        raise NotImplementedError

    def open_streaks(self) -> list[JobState]:
        raise NotImplementedError

    # watermarks
    def get_watermark(self, name: str) -> str | None:
        raise NotImplementedError

    def set_watermark(self, name: str, value: str, now: dt.datetime) -> None:
        raise NotImplementedError


def _row_to_job_state(row: dict) -> JobState:
    return JobState(
        job_key=row["job_key"], job_id=int(row["job_id"]), job_name=row["job_name"],
        consecutive_failures=int(row.get("consecutive_failures") or 0),
        last_run_id=row.get("last_run_id"), last_status=row.get("last_status"),
        last_failure_time=parse_iso(row.get("last_failure_time")),
        last_success_time=parse_iso(row.get("last_success_time")),
        last_classification=row.get("last_classification"), last_error_pattern=row.get("last_error_pattern"),
        thread_ts=row.get("thread_ts"), thread_started_at=parse_iso(row.get("thread_started_at")),
        issue_number=int(row["issue_number"]) if row.get("issue_number") is not None else None,
        issue_url=row.get("issue_url"), issue_claim_token=row.get("issue_claim_token"),
        issue_claim_at=parse_iso(row.get("issue_claim_at")), fix_notified_at=parse_iso(row.get("fix_notified_at")),
        updated_at=parse_iso(row.get("updated_at")),
    )


class SqliteStore(StateStore):
    """Reference backend. Fully tested, including two writers racing on one file.

    Uses BEGIN IMMEDIATE so every claim is serialised by SQLite's file lock; that is the atomic
    insert-if-absent the design depends on.
    """

    def __init__(self, path: str = "healwright.db"):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self.ensure_schema()

    def _tx(self):
        return _SqliteTx(self)

    def ensure_schema(self) -> None:
        c = self._conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS incidents (
              incident_key TEXT PRIMARY KEY, platform TEXT, tenant TEXT, workspace_id TEXT,
              job_id INTEGER, job_name TEXT, run_id INTEGER, source TEXT, result_state TEXT,
              termination_code TEXT, classification TEXT, stage TEXT, matched_pattern TEXT,
              state_message TEXT, error_trace TEXT, run_start_time TEXT, run_end_time TEXT,
              status TEXT NOT NULL, lease_until TEXT, attempts INTEGER DEFAULT 0,
              consecutive_failures INTEGER DEFAULT 0, alert_suppressed INTEGER DEFAULT 0,
              suppress_reason TEXT, created_at TEXT, updated_at TEXT, owner TEXT);
            CREATE TABLE IF NOT EXISTS incident_tasks (
              incident_key TEXT, task_key TEXT, result_state TEXT, termination_code TEXT,
              state_message TEXT, error_message TEXT, notebook_path TEXT,
              PRIMARY KEY (incident_key, task_key));
            CREATE TABLE IF NOT EXISTS job_state (
              job_key TEXT PRIMARY KEY, job_id INTEGER, job_name TEXT,
              consecutive_failures INTEGER DEFAULT 0, last_run_id INTEGER, last_status TEXT,
              last_failure_time TEXT, last_success_time TEXT, last_classification TEXT,
              last_error_pattern TEXT, thread_ts TEXT, thread_started_at TEXT,
              issue_number INTEGER, issue_url TEXT, issue_claim_token TEXT, issue_claim_at TEXT,
              fix_notified_at TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS action_log (
              incident_key TEXT, action TEXT, detail TEXT, created_at TEXT,
              PRIMARY KEY (incident_key, action));
            CREATE TABLE IF NOT EXISTS watermarks (name TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
            """
        )

    # -- incidents -------------------------------------------------------------------------
    def claim_incident(self, incident, classification, lease_seconds, now, decide_suppress) -> ClaimResult:
        key = incident.key
        lease_until = iso(now + dt.timedelta(seconds=lease_seconds))
        with self._tx():
            row = self._conn.execute("SELECT * FROM incidents WHERE incident_key=?", (key,)).fetchone()
            if row is not None:
                row = dict(row)
                if row["status"] == "done":
                    return ClaimResult("done", key, row["consecutive_failures"], bool(row["alert_suppressed"]), row["suppress_reason"] or "")
                lease = parse_iso(row.get("lease_until"))
                if row["status"] == "processing" and lease and lease > now:
                    return ClaimResult("busy", key, row["consecutive_failures"], bool(row["alert_suppressed"]), row["suppress_reason"] or "")
                token = _secrets.token_hex(8)
                self._conn.execute(
                    "UPDATE incidents SET status='processing', lease_until=?, attempts=attempts+1, owner=?, updated_at=? WHERE incident_key=?",
                    (lease_until, token, iso(now), key))
                return ClaimResult("resumed", key, row["consecutive_failures"], bool(row["alert_suppressed"]), row["suppress_reason"] or "", token)

            pre = self.get_job_state(incident.job_key)
            suppressed, reason = decide_suppress(pre)
            consecutive = self._apply_failure_locked(incident, classification, pre, now)
            token = _secrets.token_hex(8)
            self._conn.execute(
                f"INSERT INTO incidents ({', '.join(_INCIDENT_COLS)}) VALUES ({', '.join('?' * len(_INCIDENT_COLS))})",
                (key, incident.platform, incident.tenant, str(incident.workspace_id), incident.job_id, incident.job_name,
                 incident.run_id, incident.source, incident.result_state, incident.effective_termination_code,
                 classification.category, classification.stage, classification.pattern,
                 sanitize(incident.state_message, 10000), smart_truncate(incident.combined_trace(), 10000),
                 iso(incident.run_start_time), iso(incident.run_end_time), "processing", lease_until, 1,
                 consecutive, int(suppressed), reason, iso(now), iso(now), token))
            for t in incident.failed_tasks:
                self._conn.execute(
                    "INSERT OR REPLACE INTO incident_tasks VALUES (?,?,?,?,?,?,?)",
                    (key, t.task_key, t.result_state, t.termination_code, sanitize(t.state_message, 4000),
                     sanitize(t.error_message, 2000) if t.error_message else None, t.notebook_path))
            return ClaimResult("new", key, consecutive, suppressed, reason, token)

    def _apply_failure_locked(self, incident, classification, pre: JobState | None, now) -> int:
        jkey = incident.job_key
        start = incident.run_start_time
        stale = bool(pre and pre.last_success_time and start and start < pre.last_success_time)
        if pre is None:
            consecutive = 1
            self._conn.execute(
                "INSERT INTO job_state (job_key, job_id, job_name, consecutive_failures, last_run_id, last_status, "
                "last_failure_time, last_classification, last_error_pattern, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (jkey, incident.job_id, incident.job_name, 1, incident.run_id, incident.result_state,
                 iso(incident.run_end_time or now), classification.category, classification.pattern, iso(now)))
            return consecutive
        if stale:
            # A newer success is already recorded (late reconciler event). Record, don't regress.
            return pre.consecutive_failures
        consecutive = pre.consecutive_failures + 1
        self._conn.execute(
            "UPDATE job_state SET job_name=?, consecutive_failures=?, last_run_id=?, last_status=?, last_failure_time=?, "
            "last_classification=?, last_error_pattern=?, updated_at=? WHERE job_key=?",
            (incident.job_name, consecutive, incident.run_id, incident.result_state, iso(incident.run_end_time or now),
             classification.category, classification.pattern, iso(now), jkey))
        return consecutive

    def complete_incident(self, key, now, owner="") -> None:
        with self._tx():
            self._conn.execute("UPDATE incidents SET status='done', lease_until=NULL, updated_at=? WHERE incident_key=? AND (?='' OR owner=?)",
                               (iso(now), key, owner, owner))

    def release_incident(self, key, now, owner="") -> None:
        with self._tx():
            self._conn.execute("UPDATE incidents SET status='pending', lease_until=NULL, updated_at=? WHERE incident_key=? AND status<>'done' AND (?='' OR owner=?)",
                               (iso(now), key, owner, owner))

    def get_incident(self, key) -> dict | None:
        row = self._conn.execute("SELECT * FROM incidents WHERE incident_key=?", (key,)).fetchone()
        return dict(row) if row else None

    # -- receipts --------------------------------------------------------------------------
    def record_action(self, key, action, detail, now) -> bool:
        with self._tx():
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO action_log (incident_key, action, detail, created_at) VALUES (?,?,?,?)",
                (key, action, detail, iso(now)))
            return cur.rowcount == 1

    def has_action(self, key, action) -> bool:
        return self._conn.execute("SELECT 1 FROM action_log WHERE incident_key=? AND action=?", (key, action)).fetchone() is not None

    def count_actions_since(self, action, since) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM action_log WHERE action=? AND created_at>=?", (action, iso(since))).fetchone()[0])

    # -- job state -------------------------------------------------------------------------
    def get_job_state(self, jkey) -> JobState | None:
        row = self._conn.execute("SELECT * FROM job_state WHERE job_key=?", (jkey,)).fetchone()
        return _row_to_job_state(dict(row)) if row else None

    def apply_success(self, success, now) -> tuple[JobState | None, bool]:
        with self._tx():
            pre = self.get_job_state(success.job_key)
            start = success.run_start_time or now
            if pre is None:
                self._conn.execute(
                    "INSERT INTO job_state (job_key, job_id, job_name, consecutive_failures, last_run_id, last_status, last_success_time, updated_at) VALUES (?,?,?,0,?, 'SUCCESS', ?, ?)",
                    (success.job_key, success.job_id, success.job_name, success.run_id, iso(start), iso(now)))
                return None, False
            if pre.last_failure_time and start < pre.last_failure_time:
                return pre, False  # older than the last failure: do not reset a newer streak
            self._conn.execute(
                "UPDATE job_state SET job_name=?, consecutive_failures=0, last_run_id=?, last_status='SUCCESS', last_success_time=?, "
                "thread_ts=NULL, thread_started_at=NULL, issue_number=NULL, issue_url=NULL, issue_claim_token=NULL, "
                "issue_claim_at=NULL, fix_notified_at=NULL, updated_at=? WHERE job_key=?",
                (success.job_name, success.run_id, iso(start), iso(now), success.job_key))
            return pre, pre.consecutive_failures > 0

    def set_thread(self, jkey, thread_ts, now) -> None:
        with self._tx():
            self._conn.execute("UPDATE job_state SET thread_ts=?, thread_started_at=?, updated_at=? WHERE job_key=?",
                               (str(thread_ts), iso(now), iso(now), jkey))

    def claim_issue_slot(self, jkey, token, lease_seconds, now) -> bool:
        cutoff = iso(now - dt.timedelta(seconds=lease_seconds))
        with self._tx():
            cur = self._conn.execute(
                "UPDATE job_state SET issue_claim_token=?, issue_claim_at=? WHERE job_key=? AND issue_number IS NULL "
                "AND (issue_claim_at IS NULL OR issue_claim_at < ?)", (token, iso(now), jkey, cutoff))
            if cur.rowcount != 1:
                return False
            row = self._conn.execute("SELECT issue_claim_token FROM job_state WHERE job_key=?", (jkey,)).fetchone()
            return bool(row and row[0] == token)

    def set_issue(self, jkey, token, number, url, now) -> bool:
        with self._tx():
            cur = self._conn.execute(
                "UPDATE job_state SET issue_number=?, issue_url=?, fix_notified_at=NULL, updated_at=? WHERE job_key=? AND issue_claim_token=?",
                (number, url, iso(now), jkey, token))
            return cur.rowcount == 1

    def release_issue_slot(self, jkey, token) -> None:
        with self._tx():
            self._conn.execute("UPDATE job_state SET issue_claim_token=NULL, issue_claim_at=NULL WHERE job_key=? AND issue_claim_token=? AND issue_number IS NULL", (jkey, token))

    def clear_issue(self, jkey, number) -> bool:
        with self._tx():
            cur = self._conn.execute(
                "UPDATE job_state SET issue_number=NULL, issue_url=NULL, issue_claim_token=NULL, issue_claim_at=NULL, fix_notified_at=NULL "
                "WHERE job_key=? AND issue_number=?", (jkey, int(number)))
            return cur.rowcount == 1

    def mark_fix_notified(self, jkey, now) -> None:
        with self._tx():
            self._conn.execute("UPDATE job_state SET fix_notified_at=?, updated_at=? WHERE job_key=?", (iso(now), iso(now), jkey))

    def jobs_awaiting_fix_notice(self) -> list[JobState]:
        rows = self._conn.execute("SELECT * FROM job_state WHERE issue_number IS NOT NULL AND fix_notified_at IS NULL").fetchall()
        return [_row_to_job_state(dict(r)) for r in rows]

    def jobs_with_same_pattern(self, pattern, exclude_jkey) -> list[JobState]:
        rows = self._conn.execute(
            "SELECT * FROM job_state WHERE consecutive_failures>0 AND last_error_pattern=? AND job_key<>? ORDER BY consecutive_failures DESC, job_name",
            (pattern, exclude_jkey)).fetchall()
        return [_row_to_job_state(dict(r)) for r in rows]

    def open_streaks(self) -> list[JobState]:
        rows = self._conn.execute("SELECT * FROM job_state WHERE consecutive_failures>0 ORDER BY last_failure_time").fetchall()
        return [_row_to_job_state(dict(r)) for r in rows]

    def get_watermark(self, name) -> str | None:
        row = self._conn.execute("SELECT value FROM watermarks WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    def set_watermark(self, name, value, now) -> None:
        with self._tx():
            self._conn.execute("INSERT INTO watermarks (name, value, updated_at) VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                               (name, value, iso(now)))

    def close(self) -> None:
        self._conn.close()


class _SqliteTx:
    def __init__(self, store: SqliteStore):
        self.store = store

    def __enter__(self):
        self.store._lock.acquire()
        self.store._conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.store._conn.execute("COMMIT")
            else:
                self.store._conn.execute("ROLLBACK")
        finally:
            self.store._lock.release()
        return False


class DeltaStore(StateStore):
    """Unity Catalog Delta tables via spark.sql. Same semantics as SqliteStore.

    Implemented against the documented MERGE / UPDATE ... WHERE behaviour of Delta and read-back
    verification. Concurrency between two healers on one Delta table relies on Delta's
    optimistic transactions raising on conflict, plus an owner token written with every insert and
    read back before the row is treated as ours; state is moved only after the read-back succeeds.
    STATUS: implemented, not yet verified live under concurrent writers (docs/live-validation-checklist.md).
    """

    def __init__(self, spark: Any, schema: str = "main.healwright"):
        self.spark = spark
        self.schema = schema
        self.ensure_schema()

    def _t(self, name: str) -> str:
        return f"{self.schema}.healwright_{name}"

    def _sql(self, q: str):
        return self.spark.sql(q)

    def _rows(self, q: str) -> list[dict]:
        return [r.asDict() for r in self._sql(q).collect()]

    @staticmethod
    def _q(v: Any) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return "TRUE" if v else "FALSE"
        if isinstance(v, (int, float)):
            return str(v)
        return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"

    def ensure_schema(self) -> None:
        cat_schema = self.schema
        if "." in cat_schema:
            self._sql(f"CREATE SCHEMA IF NOT EXISTS {cat_schema}")
        self._sql(f"""CREATE TABLE IF NOT EXISTS {self._t('incidents')} (
            incident_key STRING, platform STRING, tenant STRING, workspace_id STRING, job_id BIGINT,
            job_name STRING, run_id BIGINT, source STRING, result_state STRING, termination_code STRING,
            classification STRING, stage STRING, matched_pattern STRING, state_message STRING,
            error_trace STRING, run_start_time STRING, run_end_time STRING, status STRING, lease_until STRING,
            attempts INT, consecutive_failures INT, alert_suppressed BOOLEAN, suppress_reason STRING,
            created_at STRING, updated_at STRING, owner STRING) USING DELTA""")
        self._sql(f"""CREATE TABLE IF NOT EXISTS {self._t('incident_tasks')} (
            incident_key STRING, task_key STRING, result_state STRING, termination_code STRING,
            state_message STRING, error_message STRING, notebook_path STRING) USING DELTA""")
        self._sql(f"""CREATE TABLE IF NOT EXISTS {self._t('job_state')} (
            job_key STRING, job_id BIGINT, job_name STRING, consecutive_failures INT, last_run_id BIGINT,
            last_status STRING, last_failure_time STRING, last_success_time STRING, last_classification STRING,
            last_error_pattern STRING, thread_ts STRING, thread_started_at STRING, issue_number BIGINT,
            issue_url STRING, issue_claim_token STRING, issue_claim_at STRING, fix_notified_at STRING,
            updated_at STRING) USING DELTA""")
        self._sql(f"""CREATE TABLE IF NOT EXISTS {self._t('action_log')} (
            incident_key STRING, action STRING, detail STRING, created_at STRING, owner STRING) USING DELTA""")
        self._sql(f"""CREATE TABLE IF NOT EXISTS {self._t('watermarks')} (
            name STRING, value STRING, updated_at STRING) USING DELTA""")

    def claim_incident(self, incident, classification, lease_seconds, now, decide_suppress) -> ClaimResult:
        """Insert-if-absent with an owner token, then read back. Only the winner touches job_state."""
        key = incident.key
        q = self._q
        token = _secrets.token_hex(8)
        lease_until = iso(now + dt.timedelta(seconds=lease_seconds))
        rows = self._rows(f"SELECT * FROM {self._t('incidents')} WHERE incident_key={q(key)}")
        if rows:
            row = rows[0]
            if row["status"] == "done":
                return ClaimResult("done", key, row["consecutive_failures"] or 0, bool(row["alert_suppressed"]), row["suppress_reason"] or "")
            # conditional take-over of an expired or released lease, verified by owner read-back
            self._sql(f"""UPDATE {self._t('incidents')} SET status='processing', lease_until={q(lease_until)}, attempts=attempts+1,
                owner={q(token)}, updated_at={q(iso(now))}
                WHERE incident_key={q(key)} AND status<>'done' AND (status='pending' OR lease_until IS NULL OR lease_until < {q(iso(now))})""")
            back = self._rows(f"SELECT owner, consecutive_failures, alert_suppressed, suppress_reason FROM {self._t('incidents')} WHERE incident_key={q(key)}")
            r = back[0] if back else row
            status = "resumed" if r.get("owner") == token else "busy"
            return ClaimResult(status, key, r["consecutive_failures"] or 0, bool(r["alert_suppressed"]), r["suppress_reason"] or "", token if status == "resumed" else "")

        pre = self.get_job_state(incident.job_key)
        suppressed, reason = decide_suppress(pre)
        consecutive_guess = (pre.consecutive_failures if pre else 0) + 1
        vals = [key, incident.platform, incident.tenant, str(incident.workspace_id), incident.job_id, incident.job_name,
                incident.run_id, incident.source, incident.result_state, incident.effective_termination_code,
                classification.category, classification.stage, classification.pattern,
                sanitize(incident.state_message, 10000), smart_truncate(incident.combined_trace(), 10000),
                iso(incident.run_start_time), iso(incident.run_end_time), "processing", lease_until, 1,
                consecutive_guess, bool(suppressed), reason, iso(now), iso(now), token]
        src = ", ".join(f"{q(v)} AS {c}" for v, c in zip(vals, _INCIDENT_COLS, strict=True))
        try:
            self._sql(f"""MERGE INTO {self._t('incidents')} t USING (SELECT {src}) s ON t.incident_key = s.incident_key
                WHEN NOT MATCHED THEN INSERT *""")
        except Exception as e:  # Delta raises on a conflicting concurrent write: someone else is inserting
            if "concurrent" in (type(e).__name__ + str(e)).lower():
                return ClaimResult("busy", key, consecutive_guess, suppressed, reason)
            raise
        back = self._rows(f"SELECT owner FROM {self._t('incidents')} WHERE incident_key={q(key)}")
        if not back or back[0]["owner"] != token:
            return ClaimResult("busy", key, consecutive_guess, suppressed, reason)
        # we own the row: now, and only now, move the job's streak
        consecutive = self._apply_failure(incident, classification, pre, now)
        if consecutive != consecutive_guess:
            self._sql(f"UPDATE {self._t('incidents')} SET consecutive_failures={consecutive} WHERE incident_key={q(key)}")
        for t in incident.failed_tasks:
            self._sql(f"INSERT INTO {self._t('incident_tasks')} VALUES ({q(key)}, {q(t.task_key)}, {q(t.result_state)}, {q(t.termination_code)}, {q(sanitize(t.state_message, 4000))}, {q(sanitize(t.error_message, 2000) if t.error_message else None)}, {q(t.notebook_path)})")
        return ClaimResult("new", key, consecutive, suppressed, reason, token)

    def _apply_failure(self, incident, classification, pre, now) -> int:
        """Relative increment (`consecutive_failures + 1`) so two distinct incidents of one job that
        win their claims concurrently both count, instead of the later absolute write erasing one."""
        q = self._q
        jkey = incident.job_key
        start = incident.run_start_time
        if pre is None:
            self._sql(f"""MERGE INTO {self._t('job_state')} t USING (SELECT {q(jkey)} AS job_key) s ON t.job_key = s.job_key
                WHEN NOT MATCHED THEN INSERT (job_key, job_id, job_name, consecutive_failures, last_run_id, last_status, last_failure_time, last_classification, last_error_pattern, updated_at)
                VALUES ({q(jkey)}, {incident.job_id}, {q(incident.job_name)}, 1, {incident.run_id}, {q(incident.result_state)}, {q(iso(incident.run_end_time or now))}, {q(classification.category)}, {q(classification.pattern)}, {q(iso(now))})""")
            return 1
        if pre.last_success_time and start and start < pre.last_success_time:
            return pre.consecutive_failures
        self._sql(f"""UPDATE {self._t('job_state')} SET job_name={q(incident.job_name)}, consecutive_failures=consecutive_failures + 1, last_run_id={incident.run_id},
            last_status={q(incident.result_state)}, last_failure_time={q(iso(incident.run_end_time or now))}, last_classification={q(classification.category)},
            last_error_pattern={q(classification.pattern)}, updated_at={q(iso(now))} WHERE job_key={q(jkey)}""")
        rows = self._rows(f"SELECT consecutive_failures FROM {self._t('job_state')} WHERE job_key={q(jkey)}")
        return int(rows[0]["consecutive_failures"]) if rows else pre.consecutive_failures + 1

    def complete_incident(self, key, now, owner="") -> None:
        cond = f" AND owner={self._q(owner)}" if owner else ""
        self._sql(f"UPDATE {self._t('incidents')} SET status='done', lease_until=NULL, updated_at={self._q(iso(now))} WHERE incident_key={self._q(key)}{cond}")

    def release_incident(self, key, now, owner="") -> None:
        cond = f" AND owner={self._q(owner)}" if owner else ""
        self._sql(f"UPDATE {self._t('incidents')} SET status='pending', lease_until=NULL, updated_at={self._q(iso(now))} WHERE incident_key={self._q(key)} AND status<>'done'{cond}")

    def get_incident(self, key) -> dict | None:
        rows = self._rows(f"SELECT * FROM {self._t('incidents')} WHERE incident_key={self._q(key)}")
        return rows[0] if rows else None

    def record_action(self, key, action, detail, now) -> bool:
        q = self._q
        token = _secrets.token_hex(8)
        self._sql(f"""MERGE INTO {self._t('action_log')} t USING (SELECT {q(key)} AS incident_key, {q(action)} AS action) s
            ON t.incident_key = s.incident_key AND t.action = s.action
            WHEN NOT MATCHED THEN INSERT (incident_key, action, detail, created_at, owner) VALUES ({q(key)}, {q(action)}, {q(detail)}, {q(iso(now))}, {q(token)})""")
        rows = self._rows(f"SELECT owner FROM {self._t('action_log')} WHERE incident_key={q(key)} AND action={q(action)}")
        return bool(rows and rows[0]["owner"] == token)

    def has_action(self, key, action) -> bool:
        return bool(self._rows(f"SELECT 1 AS x FROM {self._t('action_log')} WHERE incident_key={self._q(key)} AND action={self._q(action)}"))

    def count_actions_since(self, action, since) -> int:
        rows = self._rows(f"SELECT COUNT(*) AS n FROM {self._t('action_log')} WHERE action={self._q(action)} AND created_at >= {self._q(iso(since))}")
        return int(rows[0]["n"]) if rows else 0

    def get_job_state(self, jkey) -> JobState | None:
        rows = self._rows(f"SELECT * FROM {self._t('job_state')} WHERE job_key={self._q(jkey)}")
        return _row_to_job_state(rows[0]) if rows else None

    def apply_success(self, success, now) -> tuple[JobState | None, bool]:
        q = self._q
        pre = self.get_job_state(success.job_key)
        start = success.run_start_time or now
        if pre is None:
            self._sql(f"""MERGE INTO {self._t('job_state')} t USING (SELECT {q(success.job_key)} AS job_key) s ON t.job_key = s.job_key
                WHEN NOT MATCHED THEN INSERT (job_key, job_id, job_name, consecutive_failures, last_run_id, last_status, last_success_time, updated_at)
                VALUES ({q(success.job_key)}, {success.job_id}, {q(success.job_name)}, 0, {success.run_id}, 'SUCCESS', {q(iso(start))}, {q(iso(now))})""")
            return None, False
        if pre.last_failure_time and start < pre.last_failure_time:
            return pre, False
        self._sql(f"""UPDATE {self._t('job_state')} SET job_name={q(success.job_name)}, consecutive_failures=0, last_run_id={success.run_id}, last_status='SUCCESS',
            last_success_time={q(iso(start))}, thread_ts=NULL, thread_started_at=NULL, issue_number=NULL, issue_url=NULL, issue_claim_token=NULL,
            issue_claim_at=NULL, fix_notified_at=NULL, updated_at={q(iso(now))} WHERE job_key={q(success.job_key)}""")
        return pre, pre.consecutive_failures > 0

    def set_thread(self, jkey, thread_ts, now) -> None:
        q = self._q
        self._sql(f"UPDATE {self._t('job_state')} SET thread_ts={q(str(thread_ts))}, thread_started_at={q(iso(now))}, updated_at={q(iso(now))} WHERE job_key={q(jkey)}")

    def claim_issue_slot(self, jkey, token, lease_seconds, now) -> bool:
        q = self._q
        cutoff = iso(now - dt.timedelta(seconds=lease_seconds))
        self._sql(f"""UPDATE {self._t('job_state')} SET issue_claim_token={q(token)}, issue_claim_at={q(iso(now))}
            WHERE job_key={q(jkey)} AND issue_number IS NULL AND (issue_claim_at IS NULL OR issue_claim_at < {q(cutoff)})""")
        rows = self._rows(f"SELECT issue_claim_token FROM {self._t('job_state')} WHERE job_key={q(jkey)}")
        return bool(rows and rows[0]["issue_claim_token"] == token)

    def set_issue(self, jkey, token, number, url, now) -> bool:
        q = self._q
        self._sql(f"UPDATE {self._t('job_state')} SET issue_number={number}, issue_url={q(url)}, fix_notified_at=NULL, updated_at={q(iso(now))} WHERE job_key={q(jkey)} AND issue_claim_token={q(token)}")
        rows = self._rows(f"SELECT issue_number FROM {self._t('job_state')} WHERE job_key={q(jkey)}")
        return bool(rows and rows[0]["issue_number"] == number)

    def release_issue_slot(self, jkey, token) -> None:
        q = self._q
        self._sql(f"UPDATE {self._t('job_state')} SET issue_claim_token=NULL, issue_claim_at=NULL WHERE job_key={q(jkey)} AND issue_claim_token={q(token)} AND issue_number IS NULL")

    def clear_issue(self, jkey, number) -> bool:
        q = self._q
        self._sql(f"UPDATE {self._t('job_state')} SET issue_number=NULL, issue_url=NULL, issue_claim_token=NULL, issue_claim_at=NULL, fix_notified_at=NULL WHERE job_key={q(jkey)} AND issue_number={int(number)}")
        rows = self._rows(f"SELECT issue_number FROM {self._t('job_state')} WHERE job_key={q(jkey)}")
        return bool(rows) and rows[0]["issue_number"] is None

    def mark_fix_notified(self, jkey, now) -> None:
        q = self._q
        self._sql(f"UPDATE {self._t('job_state')} SET fix_notified_at={q(iso(now))}, updated_at={q(iso(now))} WHERE job_key={q(jkey)}")

    def jobs_awaiting_fix_notice(self) -> list[JobState]:
        return [_row_to_job_state(r) for r in self._rows(f"SELECT * FROM {self._t('job_state')} WHERE issue_number IS NOT NULL AND fix_notified_at IS NULL")]

    def jobs_with_same_pattern(self, pattern, exclude_jkey) -> list[JobState]:
        q = self._q
        return [_row_to_job_state(r) for r in self._rows(
            f"SELECT * FROM {self._t('job_state')} WHERE consecutive_failures>0 AND last_error_pattern={q(pattern)} AND job_key<>{q(exclude_jkey)} ORDER BY consecutive_failures DESC, job_name")]

    def open_streaks(self) -> list[JobState]:
        return [_row_to_job_state(r) for r in self._rows(f"SELECT * FROM {self._t('job_state')} WHERE consecutive_failures>0 ORDER BY last_failure_time")]

    def get_watermark(self, name) -> str | None:
        rows = self._rows(f"SELECT value FROM {self._t('watermarks')} WHERE name={self._q(name)}")
        return rows[0]["value"] if rows else None

    def set_watermark(self, name, value, now) -> None:
        q = self._q
        self._sql(f"""MERGE INTO {self._t('watermarks')} t USING (SELECT {q(name)} AS name) s ON t.name = s.name
            WHEN MATCHED THEN UPDATE SET value={q(value)}, updated_at={q(iso(now))}
            WHEN NOT MATCHED THEN INSERT (name, value, updated_at) VALUES ({q(name)}, {q(value)}, {q(iso(now))})""")


def make_store(cfg: dict, spark: Any = None) -> StateStore:
    st = cfg.get("state") or {}
    backend = (st.get("backend") or "sqlite").lower()
    if backend == "sqlite":
        return SqliteStore(st.get("sqlite_path") or "healwright.db")
    if backend == "delta":
        if spark is None:
            raise ConfigError("state.backend=delta requires a SparkSession (run inside Databricks)")
        return DeltaStore(spark, st.get("delta_schema") or "main.healwright")
    raise ConfigError(f"unknown state backend: {backend}")


# --------------------------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------------------------


def should_suppress(classification: Classification, pre: JobState | None, now: dt.datetime, window_hours: float) -> tuple[bool, str]:
    """Same classification and pattern as the previous failure, inside the dedup window."""
    if pre is None or pre.consecutive_failures == 0:
        return False, ""
    if pre.last_classification != classification.category or pre.last_error_pattern != classification.pattern:
        return False, ""
    if pre.last_failure_time is None:
        return False, ""
    gap_h = (now - pre.last_failure_time).total_seconds() / 3600
    if gap_h <= window_hours:
        return True, f"same {classification.category}/{classification.pattern} within {window_hours}h"
    return False, ""


def needs_new_thread(state: JobState | None, now: dt.datetime, reset_hours: float) -> bool:
    if state is None or not state.thread_ts:
        return True
    if state.thread_started_at is None:
        return True
    return (now - state.thread_started_at).total_seconds() / 3600 > reset_hours


def job_matches(cfg: dict, job_name: str, job_id: int | None = None) -> bool:
    jobs = cfg.get("jobs") or {}
    if jobs.get("exclude_healer", True) and job_name == cfg.get("healer_job_name"):
        return False
    if job_id is not None and cfg.get("healer_job_id") and int(cfg["healer_job_id"]) == int(job_id):
        return False
    for pat in jobs.get("exclude") or []:
        if fnmatch.fnmatchcase(job_name, pat):
            return False
    include = jobs.get("include") or ["*"]
    return any(fnmatch.fnmatchcase(job_name, pat) for pat in include)


def source_folder(notebook_path: str | None, source_root: str = "jobs") -> str | None:
    """Map a platform notebook path to the repo folder that owns it.

    Tolerates stray whitespace ("jobs /Folder/nb"): several live jobs once carried that typo,
    which the platform resolved fine but an exact comparison silently excluded from remediation.
    Returns e.g. "jobs/My_Job/" or None when the notebook is outside `source_root`.
    """
    if not notebook_path or not source_root:
        return None
    parts = [p.strip() for p in notebook_path.lstrip("/").split("/")]
    root_parts = [p for p in source_root.strip("/").split("/") if p]
    if len(parts) < len(root_parts) + 2:
        return None
    if [p for p in parts[: len(root_parts)]] != root_parts:
        return None
    folder = parts[len(root_parts)]
    if folder in (".", "..") or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", folder):
        return None
    return "/".join(root_parts + [folder]) + "/"


def folder_exists(folder: str | None, repo_checkout: str | None) -> bool | None:
    """True/False when a checkout is available to check against, None when it is not."""
    if not folder or not repo_checkout:
        return None
    return os.path.isdir(os.path.join(repo_checkout, folder))


# --------------------------------------------------------------------------------------------
# Transports and clients (Slack, GitHub)
# --------------------------------------------------------------------------------------------


class Transport(Protocol):
    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> tuple[int, dict[str, str], bytes]: ...


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float = 30.0) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https hosts from config)
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()) if e.headers else {}, e.read() or b""


class RecordingTransport:
    """Test/dry-run transport: records every call, answers from a responder or a default."""

    def __init__(self, responder: Callable[[str, str, dict | None], tuple[int, Any]] | None = None):
        self.calls: list[dict] = []
        self.responder = responder

    def __call__(self, method, url, headers, body, timeout=30.0):
        parsed = json.loads(body.decode("utf-8")) if body else None
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": parsed})
        if self.responder:
            status, payload = self.responder(method, url, parsed)
        else:
            status, payload = 200, _default_fake_response(method, url, parsed)
        return status, {"Content-Type": "application/json"}, json.dumps(payload).encode("utf-8")


def _default_fake_response(method: str, url: str, body: dict | None) -> dict:
    if "slack.com" in url:
        return {"ok": True, "ts": f"{time.time():.6f}", "channel": (body or {}).get("channel", "")}
    if url.endswith("/issues") and method == "POST":
        return {"number": 1, "html_url": "https://example.com/issues/1", "state": "open"}
    if "/search/issues" in url:
        return {"total_count": 0, "items": []}
    if "/access_tokens" in url:
        return {"token": "dryrun-token"}
    if re.search(r"/issues/\d+/timeline", url):
        return []
    if re.search(r"/issues/\d+$", url):
        return {"number": 1, "state": "open", "labels": [], "html_url": "https://example.com/issues/1"}
    return {}


class SlackClient:
    API = "https://slack.com/api/chat.postMessage"

    def __init__(self, bot_token: str, transport: Transport | None = None):
        if not bot_token:
            raise ConfigError("slack.bot_token is empty")
        self.token = bot_token
        self.transport = transport or urllib_transport

    def post(self, channel: str, text: str, thread_ts: str | None = None) -> str:
        payload: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts:
            payload["thread_ts"] = str(thread_ts)
        status, _, raw = self.transport("POST", self.API,
                                        {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json; charset=utf-8"},
                                        json.dumps(payload).encode("utf-8"), 30.0)
        data = json.loads(raw or b"{}")
        if status != 200 or not data.get("ok"):
            raise HealwrightError(f"Slack post failed: HTTP {status} {data.get('error')}")
        return str(data["ts"])


class GitHubClient:
    def __init__(self, repo: str, auth: dict, transport: Transport | None = None, api_base: str = "https://api.github.com"):
        if not repo or "/" not in repo:
            raise ConfigError("github.repo must be 'owner/name'")
        self.repo = repo
        self.auth = auth or {}
        self.transport = transport or urllib_transport
        self.api_base = api_base.rstrip("/")
        self._token_cache: dict[str, Any] = {"token": None, "exp": 0}

    # -- auth ------------------------------------------------------------------------------
    def token(self) -> str:
        kind = (self.auth.get("type") or "token").lower()
        if kind == "token":
            tok = self.auth.get("token") or ""
            if not tok:
                raise ConfigError("github.auth.token is empty")
            return tok
        if kind != "app":
            raise ConfigError(f"unknown github.auth.type: {kind}")
        now = int(time.time())
        if self._token_cache["token"] and self._token_cache["exp"] - 300 > now:
            return self._token_cache["token"]
        try:
            import jwt as pyjwt  # type: ignore
        except ImportError as e:
            raise ConfigError("github.auth.type=app needs PyJWT and cryptography installed") from e
        app_id, inst, key = self.auth.get("app_id"), self.auth.get("installation_id"), self.auth.get("private_key")
        if not (app_id and inst and key):
            raise ConfigError("github.auth.app requires app_id, installation_id, private_key")
        app_jwt = pyjwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}, key, algorithm="RS256")
        status, _, raw = self.transport("POST", f"{self.api_base}/app/installations/{inst}/access_tokens",
                                        {"Authorization": f"Bearer {app_jwt}", "Accept": "application/vnd.github+json"}, None, 15.0)
        if status not in (200, 201):
            raise HealwrightError(f"GitHub App token mint failed: HTTP {status}")
        body = json.loads(raw)
        self._token_cache = {"token": body["token"], "exp": now + 55 * 60}
        return body["token"]

    def _req(self, method: str, path: str, body: dict | None = None, timeout: float = 20.0) -> tuple[int, Any]:
        url = path if path.startswith("http") else f"{self.api_base}{path}"
        headers = {"Authorization": f"Bearer {self.token()}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        status, _, raw = self.transport(method, url, headers, data, timeout)
        try:
            return status, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return status, raw.decode("utf-8", "replace")

    # -- issues ----------------------------------------------------------------------------
    def find_issue_by_marker(self, marker: str, label: str | None = None) -> dict | None:
        """Search open issues whose body carries the incident marker. Eventually consistent:
        a net for a crash between create and record, not the primary guard."""
        q = f'repo:{self.repo} is:issue is:open "{marker}" in:body'
        if label:
            q += f" label:{label}"
        status, data = self._req("GET", f"/search/issues?q={urllib.parse.quote(q)}&per_page=5")
        if status != 200 or not isinstance(data, dict):
            return None
        for item in data.get("items", []):
            if marker in (item.get("body") or ""):
                return item
        return (data.get("items") or [None])[0] if data.get("total_count") else None

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict:
        status, data = self._req("POST", f"/repos/{self.repo}/issues", {"title": title, "body": body, "labels": labels}, 30.0)
        if status not in (200, 201):
            raise HealwrightError(f"GitHub issue creation failed: HTTP {status} {str(data)[:200]}")
        return data

    def get_issue(self, number: int) -> dict | None:
        status, data = self._req("GET", f"/repos/{self.repo}/issues/{number}")
        if status == 404:
            return None
        if status != 200:
            raise HealwrightError(f"GitHub get issue failed: HTTP {status}")
        return data

    def is_issue_open(self, number: int) -> bool | None:
        """True/False, or None when it could not be determined (callers fail closed)."""
        try:
            issue = self.get_issue(number)
        except HealwrightError:
            return None
        if issue is None:
            return False
        return issue.get("state") == "open"

    def comment(self, number: int, body: str) -> None:
        status, _ = self._req("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})
        if status not in (200, 201):
            raise HealwrightError(f"GitHub comment failed: HTTP {status}")

    def add_labels(self, number: int, labels: list[str]) -> None:
        status, _ = self._req("POST", f"/repos/{self.repo}/issues/{number}/labels", {"labels": labels})
        if status not in (200, 201):
            raise HealwrightError(f"GitHub add labels failed: HTTP {status}")

    def fix_status(self, number: int, needs_human_label: str) -> dict:
        """What the fix pipeline did with an issue: a cross-referenced PR, a needs-human label, or nothing yet."""
        out = {"has_pr": False, "pr_url": None, "needs_human": False, "closed": False}
        issue = self.get_issue(number)
        if issue is None:
            out["closed"] = True
            return out
        labels = [lb.get("name") for lb in issue.get("labels", [])]
        out["needs_human"] = needs_human_label in labels
        out["closed"] = issue.get("state") != "open"
        status, events = self._req("GET", f"/repos/{self.repo}/issues/{number}/timeline?per_page=100")
        if status == 200 and isinstance(events, list):
            for ev in events:
                if ev.get("event") == "cross-referenced":
                    src = (ev.get("source") or {}).get("issue") or {}
                    if "pull_request" in src:
                        out["has_pr"], out["pr_url"] = True, src.get("html_url")
                        break
        return out


# --------------------------------------------------------------------------------------------
# Message formatting
# --------------------------------------------------------------------------------------------

EMOJI = {"TRANSIENT": ":large_yellow_circle:", "CODE_BUG": ":red_circle:", "UPSTREAM_DATA": ":large_orange_circle:",
         "CONFIG_ERROR": ":large_purple_circle:", "UNKNOWN": ":white_circle:", "RESOLVED": ":large_green_circle:"}


def _affected_summary(pattern: str | None, affected: list[JobState], solo_text: str) -> str:
    if not affected:
        return solo_text
    top = ", ".join(_field(j.job_name, redact) for j in affected[:3])
    more = f" (+ {len(affected) - 3} more)" if len(affected) > 3 else ""
    return f"Same `{pattern}` pattern hitting {len(affected)} other job{'s' if len(affected) != 1 else ''}: {top}{more}."


def _field(value: Any, redactor: Callable[[str | None], str], max_length: int = 200) -> str:
    """A platform-controlled string (job name, task key, path) made safe for a message or table cell:
    redacted, control characters stripped, and Markdown/table syntax neutralised."""
    text = redactor(sanitize(str(value), max_length)) if value not in (None, "") else "n/a"
    return text.replace("`", "'").replace("|", "/").replace("\n", " ").replace("<", "‹").replace(">", "›")


def format_alert(incident: RunIncident, cls: Classification, consecutive: int, affected: list[JobState] | None,
                 escalation_threshold: int, redactor: Callable[[str | None], str]) -> str:
    job = _field(incident.job_name, redactor)
    task = _field(incident.primary_task.task_key, redactor) if incident.primary_task else "single-task"
    pattern = _field(cls.pattern or "n/a", redactor)
    err = redactor(sanitize(incident.state_message or incident.error_message, 500))
    run = _field(incident.run_url, redactor, 300) if incident.run_url else f"run {incident.run_id}"
    when = iso(incident.run_end_time) or "n/a"
    e = EMOJI.get(cls.category, EMOJI["UNKNOWN"])
    marker = f"[{INCIDENT_MARKER}{incident.key[:12]}]"
    if cls.category == "TRANSIENT":
        return (f"{e} TRANSIENT | {job}\nLikely transient failure: retries exhausted without recovery.\n"
                f"Run: {run} | Pattern: {pattern}\nTime: {when}\nConsecutive failures: {consecutive} {marker}")
    if cls.category == "CODE_BUG":
        return (f"{e} CODE_BUG | {job}\nNeeds human attention: a code or configuration fix is required.\n\n"
                f"Error: {err}\nPattern: {pattern}\nTask: {task}\nRun: {run}\nConsecutive failures: {consecutive} {marker}")
    if cls.category == "UPSTREAM_DATA":
        text = (f"{e} UPSTREAM_DATA | {job}\nUpstream data dependency not ready. May resolve on the next scheduled run.\n\n"
                f"Error: {err}\nPattern: {pattern}\nRun: {run}\nConsecutive failures: {consecutive} {marker}")
        if consecutive >= escalation_threshold:
            text += (f"\n\n:warning: UPSTREAM SLA BREACH ({consecutive} consecutive UPSTREAM_DATA failures)\n"
                     + _affected_summary(pattern, affected or [], "Only this job affected: a partner- or source-specific issue is likely.")
                     + "\nNext: check the job's notes for the upstream owner, verify the endpoint or delivery, or pause the job if delivery is intentionally suspended.")
        return text
    if cls.category == "CONFIG_ERROR":
        text = (f"{e} CONFIG_ERROR | {job}\nJob-definition or workspace fix required: this is not a notebook bug. "
                f"Compare the live job configuration with the repo definition.\n\n"
                f"Error: {err}\nPattern: {pattern}\nTask: {task}\nRun: {run}\nConsecutive failures: {consecutive} {marker}")
        if consecutive >= escalation_threshold:
            text += (f"\n\n:rotating_light: ESCALATION ({consecutive} consecutive CONFIG_ERROR failures)\n"
                     + _affected_summary(pattern, affected or [], "No other jobs currently failing with this pattern.")
                     + "\nThis is usually workspace-level (expired git credential, drifted job JSON, missing secret). One fix likely clears all affected jobs.")
        return text
    return (f"{e} UNKNOWN | {job}\nUnclassified failure: review and add a pattern if this is a known failure type.\n\n"
            f"Result state: {incident.result_state}\nState message: {redactor(sanitize(incident.state_message, 1000))}\n"
            f"Termination code: {incident.effective_termination_code or 'n/a'}\nTask: {task}\nRun: {run}\nConsecutive failures: {consecutive} {marker}")


def format_resolution(job_name: str, pre: JobState, success: RunSuccess) -> str:
    return (f"{EMOJI['RESOLVED']} RESOLVED | {_field(job_name, redact)}\nRecovered after {pre.consecutive_failures} consecutive failure"
            f"{'s' if pre.consecutive_failures != 1 else ''} (was {pre.last_classification or 'n/a'}/{_field(pre.last_error_pattern or 'n/a', redact)}).\n"
            f"Run: {success.run_id}")


def format_flood_summary(items: list[tuple[RunIncident, Classification]], sent: int) -> str:
    top = "\n".join(f"  - {_field(i.job_name, redact)}: {c.category}" for i, c in items[:5])
    return (f":rotating_light: ALERT FLOOD | {len(items)} failures in one healer run ({sent} alerted individually)\n"
            f"Summary instead of individual alerts to protect the channel.\nTop failures:\n{top}\n"
            f"Query the incidents table for the full list.")


def format_issue(incident: RunIncident, cls: Classification, consecutive: int, thread_ts: str | None,
                 include_raw_trace: bool, redactor: Callable[[str | None], str]) -> tuple[str, str]:
    """Title and body for the fix-pipeline issue. Body fields are an allowlist; traces are redacted
    and included only when the config opts in."""
    nb = _field(incident.effective_notebook_path or "unknown", redactor, 300)
    task = _field(incident.primary_task.task_key, redactor) if incident.primary_task else "n/a"
    job = _field(incident.job_name, redactor)
    summary = _field((cls.pattern or "unknown error")[:80], redactor)
    title = f"[Self-Healing] {cls.category}: {job} - {summary}"
    err = redactor(sanitize(incident.error_message or incident.state_message, 3000))
    term = _field(incident.effective_termination_code or "n/a", redactor)
    run_url = _field(incident.run_url, redactor, 300) if incident.run_url else "n/a"
    trace_block = ""
    if include_raw_trace:
        trace_block = f"\n## Error Trace\n\n```\n{redactor(sanitize(incident.combined_trace(), 5000))}\n```\n"
    else:
        trace_block = "\n## Error Trace\n\nRaw traces are not attached (policy.include_raw_trace=false). Open the run link.\n"
    body = f"""## Error Summary

**Classification:** {cls.category} ({cls.stage})
**Pattern:** {summary}
**Consecutive failures:** {consecutive}

## Job Details

| Field | Value |
|-------|-------|
| Job Name | `{job}` |
| Job ID | `{int(incident.job_id)}` |
| Task Key | `{task}` |
| Notebook Path | `{nb}` |
| Run ID | `{int(incident.run_id)}` |
| Termination Code | `{term}` |

## Error Output

```
{err}
```
{trace_block}
## Run Link

{run_url}

## Suggested Investigation

1. Read the failing notebook source and the error output above
2. Check recent history for the file: `git log --oneline -10 -- {nb}`
3. Check whether the error matches a known pattern or a recent change

<!-- {INCIDENT_MARKER}{incident.key} -->
<!-- slack_thread_ts:{thread_ts or 'none'} -->
"""
    return title, body


# --------------------------------------------------------------------------------------------
# Platform adapters
# --------------------------------------------------------------------------------------------


class Platform(Protocol):
    platform: str
    workspace_id: str
    workspace_url: str | None

    def fetch_run(self, run_id: int, source: str) -> RunIncident | RunSuccess | None: ...
    def list_completed_runs(self, start_ms: int, end_ms: int) -> Iterable[RunIncident | RunSuccess]: ...


class DatabricksPlatform:
    """Jobs API 2.1 via databricks-sdk. Only imports the SDK when constructed."""

    platform = "databricks"

    def __init__(self, client: Any = None, workspace_url: str | None = None, workspace_id: str | None = None, tenant: str = ""):
        if client is None:
            from databricks.sdk import WorkspaceClient  # type: ignore

            client = WorkspaceClient()
        self.w = client
        self.tenant = tenant
        self.workspace_url = (workspace_url or getattr(getattr(client, "config", None), "host", None) or "").rstrip("/") or None
        self.workspace_id = str(workspace_id or self._detect_workspace_id() or "0")

    def _detect_workspace_id(self) -> str | None:
        try:
            return str(self.w.get_workspace_id())
        except Exception:
            return None

    # Jobs API 2.2 `status.termination_details.code` values that say nothing a classifier can use;
    # for these we fall back to the cluster's termination reason.
    _GENERIC_CODES = {"SUCCESS", "SUCCESS_WITH_FAILURES", "USER_CANCELED", "CANCELED", "RUN_EXECUTION_ERROR",
                      "MAX_JOB_QUEUE_SIZE_EXCEEDED", "SKIPPED", "UNKNOWN", "CLOUD_FAILURE", "CLUSTER_ERROR",
                      "DRIVER_ERROR", "INTERNAL_ERROR", "RUN_ERROR"}
    _CODE_TO_RESULT = {"SUCCESS": "SUCCESS", "SUCCESS_WITH_FAILURES": "SUCCESS_WITH_FAILURES", "USER_CANCELED": "CANCELED",
                       "CANCELED": "CANCELED", "SKIPPED": "SKIPPED", "INTERNAL_ERROR": "INTERNAL_ERROR"}

    @classmethod
    def _state_of(cls, obj) -> tuple[str, str, str]:
        """(result_state, life_cycle_state, message) from either the legacy `state` or the 2.2 `status`."""
        state = getattr(obj, "state", None)
        rs = getattr(state, "result_state", None)
        lcs = getattr(state, "life_cycle_state", None)
        msg = getattr(state, "state_message", None) or ""
        status = getattr(obj, "status", None)
        if rs is None and status is not None:
            td = getattr(status, "termination_details", None)
            code = str(getattr(getattr(td, "code", None), "value", getattr(td, "code", None)) or "")
            ttype = str(getattr(getattr(td, "type", None), "value", getattr(td, "type", None)) or "")
            msg = msg or getattr(td, "message", None) or ""
            lcs = lcs or getattr(status, "state", None)
            if code:
                if code in cls._CODE_TO_RESULT:
                    rs = cls._CODE_TO_RESULT[code]
                elif "TIMEOUT" in code or "TIMED_OUT" in code:
                    rs = "TIMEDOUT"
                elif ttype == "INTERNAL_ERROR":
                    rs = "INTERNAL_ERROR"
                else:
                    rs = "FAILED"  # CLIENT_ERROR / CLOUD_FAILURE and every other error code
        return (str(getattr(rs, "value", rs) or ""), str(getattr(lcs, "value", lcs) or ""), msg)

    def _termination_code(self, run) -> str | None:
        status = getattr(run, "status", None)
        td = getattr(status, "termination_details", None)
        code = getattr(td, "code", None)
        code = str(getattr(code, "value", code)) if code else None
        if code and code not in self._GENERIC_CODES:
            return code
        for task in getattr(run, "tasks", None) or []:
            ci = getattr(task, "cluster_instance", None)
            cid = getattr(ci, "cluster_id", None)
            if not cid:
                continue
            try:
                cl = self.w.clusters.get(cid)
                tr = getattr(cl, "termination_reason", None)
                if tr and getattr(tr, "code", None):
                    return str(getattr(tr.code, "value", tr.code))
            except Exception:
                continue
        return None

    def _output(self, task_run_id: int | None) -> tuple[str | None, str | None]:
        if not task_run_id:
            return None, None
        try:
            out = self.w.jobs.get_run_output(task_run_id)
        except Exception:
            return None, None
        err = getattr(out, "error", None)
        trace = getattr(out, "error_trace", None)
        return (smart_truncate(str(err), 2000) if err else None, smart_truncate(trace, 10000) if trace else None)

    _FAILED_TASK_STATES = {"FAILED", "TIMEDOUT", "INTERNAL_ERROR", "CANCELED"}
    _FAILED_RUN_STATES = {"FAILED", "TIMEDOUT", "CANCELED", "INTERNAL_ERROR", "SUCCESS_WITH_FAILURES",
                          "MAXIMUM_CONCURRENT_RUNS_REACHED", "UPSTREAM_FAILED"}

    def _incident_from_run(self, run, source: str) -> RunIncident | RunSuccess | None:
        """Build an incident from a run.

        In leaf-task mode the parent run is still RUNNING when the healer looks at it (the
        `__healwright` task is part of that run), so there is no run-level result state yet.
        Failed tasks are the signal; the run-level state is used when it exists.
        """
        rs, lcs, msg = self._state_of(run)
        job_name = getattr(run, "run_name", None) or f"job-{run.job_id}"
        tasks = list(getattr(run, "tasks", None) or [])
        failed = []
        for t in tasks:
            trs, _, tmsg = self._state_of(t)
            if trs in self._FAILED_TASK_STATES and t.task_key not in SENTINEL_TASK_KEYS:
                failed.append((t, trs, tmsg))
        if rs == "SUCCESS":
            return RunSuccess(self.platform, self.workspace_id, run.job_id, job_name, run.run_id, source,
                              from_epoch_ms(run.start_time), from_epoch_ms(run.end_time), self.tenant)
        in_progress = rs == "" or lcs in {"RUNNING", "PENDING", "QUEUED", "BLOCKED", "TERMINATING"}
        if in_progress and not failed:
            return None  # nothing has failed (yet); the reconciler will see the finished run
        if not in_progress and rs not in self._FAILED_RUN_STATES:
            return None
        if in_progress:
            rs = "FAILED"  # derived from the failed task(s); the run itself has not finished
        inc = RunIncident(self.platform, self.workspace_id, run.job_id, job_name, run.run_id, source,
                          result_state=rs, life_cycle_state=lcs, state_message=msg,
                          run_start_time=from_epoch_ms(run.start_time), run_end_time=from_epoch_ms(run.end_time),
                          workspace_url=self.workspace_url, tenant=self.tenant)
        if not failed and rs != "SUCCESS_WITH_FAILURES":
            # whole-run failure with no per-task state (e.g. cluster never launched): use the first real task
            for t in tasks:
                if t.task_key not in SENTINEL_TASK_KEYS:
                    failed.append((t, rs, msg))
                    break
        for t, trs, tmsg in failed:
            err, trace = self._output(getattr(t, "run_id", None))
            nb = getattr(getattr(t, "notebook_task", None), "notebook_path", None)
            inc.tasks.append(TaskFailure(t.task_key, trs, getattr(t, "run_id", None), tmsg, None, err, trace, nb,
                                         from_epoch_ms(getattr(t, "start_time", None)), from_epoch_ms(getattr(t, "end_time", None)),
                                         getattr(t, "attempt_number", 0) or 0))
        if not inc.tasks:
            return None  # only sentinel tasks failed: nothing for us
        if inc.run_end_time is None:
            inc.run_end_time = max((t.end_time for t in inc.tasks if t.end_time), default=None)
        inc.termination_code = self._termination_code(run)
        if inc.primary_task:
            inc.error_message = inc.primary_task.error_message
            inc.error_trace = inc.primary_task.error_trace
            inc.notebook_path = inc.primary_task.notebook_path
        return inc

    def fetch_run(self, run_id: int, source: str = "leaf-task") -> RunIncident | RunSuccess | None:
        run = self.w.jobs.get_run(int(run_id))
        return self._incident_from_run(run, source)

    def list_completed_runs(self, start_ms: int, end_ms: int) -> Iterable[RunIncident | RunSuccess]:
        for run in self.w.jobs.list_runs(completed_only=True, start_time_from=start_ms, start_time_to=end_ms, expand_tasks=True):
            if getattr(run, "state", None) is None and getattr(run, "status", None) is None:
                continue
            item = self._incident_from_run(run, "reconcile")
            if item is not None:
                yield item


# --------------------------------------------------------------------------------------------
# Healer
# --------------------------------------------------------------------------------------------


@dataclass
class HandleResult:
    outcome: str
    incident_key: str | None = None
    classification: str | None = None
    pattern: str | None = None
    consecutive: int = 0
    actions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


class Healer:
    def __init__(self, cfg: dict, store: StateStore, platform: Platform | None = None, classifier: Classifier | None = None,
                 slack: SlackClient | None = None, github: GitHubClient | None = None, now: Callable[[], dt.datetime] = utcnow,
                 log: Callable[[str], None] = print, dry_run: bool = False):
        self.cfg = cfg
        self.store = store
        self.platform = platform
        self.now = now
        self.log = log
        self.dry_run = dry_run
        self.policy = cfg.get("policy") or {}
        self.classifier = classifier or Classifier.from_file(cfg["patterns_file"], cfg.get("classification"))
        self.recorder: RecordingTransport | None = None
        transport: Transport | None = None
        if dry_run:
            self.recorder = RecordingTransport()
            transport = self.recorder
        self.slack = slack
        if self.slack is None and (cfg.get("slack") or {}).get("enabled"):
            self.slack = SlackClient((cfg["slack"].get("bot_token") or ("dry-run" if dry_run else "")), transport)
        self.github = github
        if self.github is None and (cfg.get("github") or {}).get("enabled"):
            gh = cfg["github"]
            auth = dict(gh.get("auth") or {})
            if dry_run and not auth.get("token") and (auth.get("type") or "token") == "token":
                auth["token"] = "dry-run"
            self.github = GitHubClient(gh["repo"], auth, transport, gh.get("api_base") or "https://api.github.com")
        self.shadow = bool(self.policy.get("shadow"))
        extra = (cfg.get("redaction") or {}).get("extra_patterns") or []
        self.redactor = lambda text: redact(text, extra)
        self._alerts_this_run = 0

    # -- entry points ----------------------------------------------------------------------
    def handle(self, item: RunIncident | RunSuccess) -> HandleResult:
        if isinstance(item, RunSuccess):
            return self.handle_success(item)
        return self.handle_incident(item)

    def run_leaf_task(self, parent_run_id: int) -> HandleResult:
        if self.platform is None:
            raise ConfigError("leaf-task mode needs a platform adapter")
        item = self.platform.fetch_run(int(parent_run_id), "leaf-task")
        if item is None:
            return HandleResult("nothing-to-do", notes=[f"run {parent_run_id} has no failed non-sentinel task"])
        return self.handle(item)

    def run_reconcile(self) -> list[HandleResult]:
        if self.platform is None:
            raise ConfigError("reconcile mode needs a platform adapter")
        rc = self.cfg.get("reconcile") or {}
        now = self.now()
        wm = parse_iso(self.store.get_watermark("reconcile_end"))
        # The Jobs API filters on run START time and we only see completed runs, so a run that
        # started before the last sweep and finished after it would be missed by a pure
        # watermark. Reach back far enough to cover the longest run you expect (max_run_hours).
        reach = dt.timedelta(hours=float(rc.get("max_run_hours", 24)))
        if wm is None:
            start = now - max(reach, dt.timedelta(minutes=int(rc.get("initial_lookback_minutes", 120))))
        else:
            start = wm - max(reach, dt.timedelta(minutes=int(rc.get("overlap_minutes", 30))))
        end = min(now, start + dt.timedelta(minutes=int(rc.get("max_window_minutes", 1440 * 3))))
        items = list(self.platform.list_completed_runs(int(start.timestamp() * 1000), int(end.timestamp() * 1000)))
        successes = sorted([i for i in items if isinstance(i, RunSuccess)], key=lambda i: i.run_start_time or now)
        incidents = sorted([i for i in items if isinstance(i, RunIncident)], key=lambda i: i.run_start_time or now)
        results = [self.handle_success(s) for s in successes]
        results += [self.handle_incident(i) for i in incidents]
        self.store.set_watermark("reconcile_end", iso(end) or "", now)
        self.log(f"reconcile: window {iso(start)} .. {iso(end)}: {len(successes)} successes, {len(incidents)} incidents")
        return results

    # -- successes -------------------------------------------------------------------------
    def handle_success(self, success: RunSuccess) -> HandleResult:
        if not job_matches(self.cfg, success.job_name, success.job_id):
            return HandleResult("skipped", notes=["job not monitored"])
        now = self.now()
        res = HandleResult("success", success.key)
        pre = self.store.get_job_state(success.job_key)
        recovered = bool(pre and pre.consecutive_failures > 0
                         and (not pre.last_failure_time or (success.run_start_time or now) >= pre.last_failure_time))
        if recovered and self._can_post() and not self.store.has_action(success.key, "slack_resolved"):
            try:
                self.slack.post(self.cfg["slack"]["channel"], format_resolution(self.redactor(success.job_name), pre, success), pre.thread_ts)  # type: ignore[union-attr]
                self.store.record_action(success.key, "slack_resolved", None, now)
                res.actions.append("slack_resolved")
            except HealwrightError as e:
                res.notes.append(f"slack resolved post failed: {e}")  # state still resets: it must reflect reality
        _, reset = self.store.apply_success(success, now)
        if reset:
            res.actions.append("streak_reset")
        return res

    # -- incidents -------------------------------------------------------------------------
    def handle_incident(self, incident: RunIncident) -> HandleResult:
        if not job_matches(self.cfg, incident.job_name, incident.job_id):
            return HandleResult("skipped", incident.key, notes=["job not monitored"])
        if incident.failed_tasks == [] and incident.result_state == "SUCCESS_WITH_FAILURES":
            return HandleResult("skipped", incident.key, notes=["only sentinel tasks failed"])
        now = self.now()
        cls = self.classifier.classify(incident)
        window = float(self.policy.get("dedup_window_hours", 4))
        claim = self.store.claim_incident(incident, cls, int(self.policy.get("lease_seconds", 900)), now,
                                          lambda pre: should_suppress(cls, pre, now, window))
        res = HandleResult(claim.status, incident.key, cls.category, cls.pattern, claim.consecutive_failures)
        if claim.status in ("done", "busy"):
            res.notes.append("already handled" if claim.status == "done" else "another healer holds the lease")
            return res
        self.log(f"{claim.status}: {_field(incident.job_name, self.redactor)} run {int(incident.run_id)} -> {cls.category}/{cls.pattern} (streak {claim.consecutive_failures})")
        try:
            self._alert(incident, cls, claim, res, now)
            self._open_issue(incident, cls, claim, res, now)
        except Exception:
            self.store.release_incident(incident.key, self.now(), claim.owner)
            raise
        if res.failed:
            # an external action failed: give the incident back so the reconciler retries it
            self.store.release_incident(incident.key, self.now(), claim.owner)
            res.outcome = "released"
            return res
        self.store.complete_incident(incident.key, self.now(), claim.owner)
        res.outcome = "handled" if claim.status == "new" else "resumed"
        return res

    def _can_post(self) -> bool:
        return self.slack is not None and not self.shadow

    def _alert(self, incident: RunIncident, cls: Classification, claim: ClaimResult, res: HandleResult, now: dt.datetime) -> None:
        if claim.alert_suppressed:
            res.notes.append(f"alert suppressed: {claim.suppress_reason}")
            return
        if not self._can_post():
            res.notes.append("slack disabled" if self.slack is None else "shadow mode: alert not posted")
            return
        if self._alerts_this_run >= int(self.policy.get("max_alerts_per_run", 10)):
            res.notes.append("alert cap for this run reached")
            return
        if self.store.has_action(incident.key, "slack_alert"):
            res.notes.append("alert already posted")
            return
        state = self.store.get_job_state(incident.job_key)
        affected: list[JobState] = []
        threshold = int(self.policy.get("escalation_threshold", 3))
        if cls.category in ("CONFIG_ERROR", "UPSTREAM_DATA") and claim.consecutive_failures >= threshold and cls.pattern:
            affected = self.store.jobs_with_same_pattern(cls.pattern, incident.job_key)
        text = format_alert(incident, cls, claim.consecutive_failures, affected, threshold, self.redactor)
        channel = self.cfg["slack"]["channel"]
        try:
            if needs_new_thread(state, now, float(self.policy.get("thread_reset_hours", 24))):
                ts = self.slack.post(channel, text)  # type: ignore[union-attr]
                self.store.set_thread(incident.job_key, ts, now)
            else:
                self.slack.post(channel, text, state.thread_ts)  # type: ignore[union-attr]
            self.store.record_action(incident.key, "slack_alert", None, now)
            self._alerts_this_run += 1
            res.actions.append("slack_alert")
        except HealwrightError as e:
            res.notes.append(f"slack alert failed: {e}")
            res.failed.append("slack_alert")

    def _open_issue(self, incident: RunIncident, cls: Classification, claim: ClaimResult, res: HandleResult, now: dt.datetime) -> None:
        if cls.category not in (self.policy.get("issue_categories") or ["CODE_BUG"]):
            return
        if self.github is None or self.shadow:
            res.notes.append("github disabled" if self.github is None else "shadow mode: issue not opened")
            return
        gh = self.cfg["github"]
        folder = source_folder(incident.effective_notebook_path, gh.get("source_root") or "jobs")
        if not folder:
            res.notes.append("no issue: notebook path outside source_root")
            return
        exists = folder_exists(folder, gh.get("repo_checkout"))
        if exists is False:
            res.notes.append(f"no issue: {folder} not found in repo checkout")
            return
        if self.store.has_action(incident.key, "github_issue"):
            res.notes.append("issue already recorded for this incident")
            return
        cap = int(self.policy.get("max_issues_per_hour", 5))
        if self.store.count_actions_since("github_issue", now - dt.timedelta(hours=1)) >= cap:
            res.notes.append(f"no issue: {cap}/hour cap reached")
            return
        state = self.store.get_job_state(incident.job_key)
        if state and state.issue_number is not None:
            still_open = self.github.is_issue_open(state.issue_number)
            if still_open is None:
                res.notes.append(f"no issue: tracked #{state.issue_number} state unknown (failing closed)")
                return
            if still_open:
                res.notes.append(f"no issue: tracked #{state.issue_number} still open")
                return
            # closed while the job still fails: forget it, but only if nobody replaced it meanwhile
            if not self.store.clear_issue(incident.job_key, state.issue_number):
                res.notes.append("no issue: tracked issue changed under us; another healer is handling it")
                return
        token = _secrets.token_hex(8)
        if not self.store.claim_issue_slot(incident.job_key, token, int(self.policy.get("lease_seconds", 900)), now):
            res.notes.append("no issue: another healer holds the per-job issue claim")
            return
        try:
            marker = f"{INCIDENT_MARKER}{incident.key}"
            existing = self.github.find_issue_by_marker(marker, gh.get("label"))
            if existing:
                self.store.set_issue(incident.job_key, token, int(existing["number"]), existing.get("html_url", ""), now)
                self.store.record_action(incident.key, "github_issue", existing.get("html_url"), now)
                res.notes.append(f"issue already exists: #{existing['number']}")
                return
            title, body = format_issue(incident, cls, claim.consecutive_failures, state.thread_ts if state else None,
                                       bool(self.policy.get("include_raw_trace")), self.redactor)
            issue = self.github.create_issue(title, body, [gh.get("label") or "self-healing-fix"])
            self.store.set_issue(incident.job_key, token, int(issue["number"]), issue.get("html_url", ""), now)
            self.store.record_action(incident.key, "github_issue", issue.get("html_url"), now)
            res.actions.append("github_issue")
            self.log(f"opened issue #{int(issue['number'])} for {_field(incident.job_name, self.redactor)}")
        except HealwrightError as e:
            self.store.release_issue_slot(incident.job_key, token)
            res.notes.append(f"issue creation failed: {e}")
            res.failed.append("github_issue")

    # -- follow-ups ------------------------------------------------------------------------
    def run_followups(self) -> list[HandleResult]:
        """Tell the channel what the fix pipeline did with each open issue (once)."""
        out: list[HandleResult] = []
        if self.github is None:
            return out
        now = self.now()
        needs_human = (self.cfg["github"].get("needs_human_label") or "needs-human")
        for st in self.store.jobs_awaiting_fix_notice():
            res = HandleResult("followup", None, notes=[_field(st.job_name, self.redactor)])
            try:
                status = self.github.fix_status(int(st.issue_number), needs_human)  # type: ignore[arg-type]
            except HealwrightError as e:
                res.notes.append(f"fix status failed: {e}")
                out.append(res)
                continue
            name = _field(st.job_name, self.redactor)
            if status["has_pr"]:
                text = f":robot_face: AI fix PR opened for {name}: {_field(status['pr_url'], self.redactor, 300)}. Draft PR: human review required before merge."
            elif status["needs_human"]:
                text = f":mag: AI analysis finished for {name} with no confident fix. Needs a human: {_field(st.issue_url, self.redactor, 300)}"
            else:
                continue
            if self._can_post():
                try:
                    self.slack.post(self.cfg["slack"]["channel"], text, st.thread_ts)  # type: ignore[union-attr]
                    res.actions.append("slack_followup")
                except HealwrightError as e:
                    res.notes.append(f"slack followup failed: {e}")
                    out.append(res)
                    continue
            self.store.mark_fix_notified(st.job_key, now)
            out.append(res)
        return out


# --------------------------------------------------------------------------------------------
# Simulation (local, no platform)
# --------------------------------------------------------------------------------------------


def simulate_incident(category_hint: str = "CODE_BUG", job_id: int = 4242, run_id: int = 100, job_name: str = "example_job",
                      workspace_id: str = "0", notebook_path: str = "/jobs/example_job/main") -> RunIncident:
    samples = {
        "CODE_BUG": ("Workload failed, see run output for details.", "KeyError: 'customer_id'",
                     "Traceback (most recent call last):\n  File \"main\", line 42, in <module>\n    df[\"customer_id\"]\nKeyError: 'customer_id'"),
        "TRANSIENT": ("Task failed with message: Connection reset by peer", "ConnectionResetError: [Errno 104] Connection reset by peer", None),
        "UPSTREAM_DATA": ("Workload failed", "FileNotFoundError: [Errno 2] No such file: sftp://partner/inbound/2026-01-01.csv", None),
        "CONFIG_ERROR": ("Failed to checkout Git repository: UNAUTHENTICATED", None, None),
        "UNKNOWN": ("Something nobody has seen before", None, None),
    }
    msg, err, trace = samples.get(category_hint, samples["UNKNOWN"])
    now = utcnow()
    inc = RunIncident("databricks", workspace_id, job_id, job_name, run_id, "simulate", state_message=msg,
                      error_message=err, error_trace=trace, notebook_path=notebook_path,
                      run_start_time=now - dt.timedelta(minutes=10), run_end_time=now, workspace_url="https://example.cloud.databricks.com")
    inc.tasks.append(TaskFailure("main", "FAILED", run_id * 10, msg, None, err, trace, notebook_path))
    return inc
