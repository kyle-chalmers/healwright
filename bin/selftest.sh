#!/usr/bin/env bash
# healwright self-test: THE gate. Run before every commit; CI runs it on three Python versions.
#   ruff -> pytest -> import check of the copyable template -> leak scan -> skill surface checks
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3}"
if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; fi

echo "==> ruff"
"$PY" -m ruff check . || { echo "FAIL: ruff"; exit 1; }

echo "==> pytest"
"$PY" -m pytest -q || { echo "FAIL: pytest"; exit 1; }

echo "==> template imports standalone"
( cd templates/healer && "$PY" -c "import healwright_core, sys; print('healwright_core', healwright_core.__version__)" ) \
  || { echo "FAIL: templates/healer/healwright_core.py does not import on its own"; exit 1; }
"$PY" tools/install_trigger.py --job-json tests/fixtures/job_before.json --healer-job-id 999 >/dev/null \
  || { echo "FAIL: install_trigger offline dry run"; exit 1; }

echo "==> leak scan (shapes)"
DENY_ARG=()
if [ -n "${HEALWRIGHT_LEAK_DENYLIST:-}" ]; then
  DENY_ARG=(--denylist "$HEALWRIGHT_LEAK_DENYLIST")
  echo "    (plus private denylist)"
fi
"$PY" bin/leak_scan.py --git . ${DENY_ARG[@]+"${DENY_ARG[@]}"} || exit 1

echo "==> skills"
for s in self-healing-audit self-healing-build self-healing-install-trigger; do
  [ -f "skills/$s/SKILL.md" ] || { echo "FAIL: missing skill: $s"; exit 1; }
  last="$(grep -E '^## ' "skills/$s/SKILL.md" | tail -1)"
  [ "$last" = "## Next" ] || { echo "FAIL: skills/$s/SKILL.md must END with a '## Next' section (last is '$last')"; exit 1; }
done
extra="$(ls -d skills/*/ | grep -Ev '/(self-healing-audit|self-healing-build|self-healing-install-trigger)/$' || true)"
[ -z "$extra" ] || { echo "FAIL: unexpected skill folder: $extra"; exit 1; }

echo "==> workflow lint (zizmor, if installed)"
if command -v zizmor >/dev/null 2>&1; then
  zizmor --min-severity medium .github/workflows || { echo "FAIL: zizmor"; exit 1; }
elif "$PY" -c "import zizmor" 2>/dev/null; then
  "$PY" -m zizmor --min-severity medium .github/workflows || { echo "FAIL: zizmor"; exit 1; }
else
  echo "    zizmor not installed; skipped locally (CI runs it)"
fi

echo "OK: healwright selftest passed"
