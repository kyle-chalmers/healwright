"""healwright propagate task.

Appended (opt-in) by tools/install_trigger.py as the task `__healwright_propagate` with
run_if=AT_LEAST_ONE_FAILED. Its only job is to fail, so that a run in which a real task failed
ends FAILED instead of SUCCESS_WITH_FAILURES once the healer leaf task succeeds. That keeps the
job's existing on_failure notifications firing exactly as before healwright was installed.

The healer never classifies this task: its task_key is a sentinel (see SENTINEL_TASK_KEYS).
"""

import sys

print("healwright propagate: an upstream task failed; failing this task so the run stays FAILED.", file=sys.stderr)
raise SystemExit("healwright_propagate: upstream task failed")
