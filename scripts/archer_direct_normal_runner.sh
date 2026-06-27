#!/usr/bin/env zsh
set -e

RUN_ID="${ARCHER_RUN_ID:?set ARCHER_RUN_ID}"
DURATION_SECONDS="${ARCHER_DURATION_SECONDS:-86400}"
PROFILE="${ARCHER_INITIAL_PROFILE:-overnight_balanced_low_churn}"
REASON="${ARCHER_INITIAL_REASON:-direct normal-spread run; adaptive disabled}"
LOG_DIR="/app/logs/adaptive-12h-${RUN_ID}"

APPROVAL_RESULT="$(
  ARCHER_REQUESTED_PROFILE="$PROFILE" ARCHER_REQUESTED_REASON="$REASON" python3 - <<'PY'
import os
import sys

sys.path.insert(0, "/app")
from scripts.archer_adaptive_12h import resolve_profile_approval

profile, reason = resolve_profile_approval(
    os.environ["ARCHER_REQUESTED_PROFILE"],
    os.environ["ARCHER_REQUESTED_REASON"],
    source="direct_runner:start",
)
print(profile)
print(reason)
PY
)"
PROFILE="$(printf '%s\n' "$APPROVAL_RESULT" | sed -n '1p')"
REASON="$(printf '%s\n' "$APPROVAL_RESULT" | sed -n '2,$p')"
export ARCHER_INITIAL_PROFILE="$PROFILE"
export ARCHER_INITIAL_REASON="$REASON"

DEFAULT_PREFLIGHT_MIN_SPREAD="$(
  ARCHER_INITIAL_PROFILE="$PROFILE" python3 - <<'PY'
import os
import sys

sys.path.insert(0, "/app")
from scripts.archer_adaptive_12h import profile_validation_defaults

print(profile_validation_defaults(os.environ["ARCHER_INITIAL_PROFILE"])["preflight_min_effective_spread_bps"])
PY
)"
PREFLIGHT_MIN_EFFECTIVE_SPREAD_BPS="${ARCHER_PREFLIGHT_MIN_EFFECTIVE_SPREAD_BPS:-$DEFAULT_PREFLIGHT_MIN_SPREAD}"

mkdir -p "$LOG_DIR"

python3 - <<'PY'
import os
import sys

sys.path.insert(0, "/app")
from scripts.archer_adaptive_12h import AdaptiveController

run_id = os.environ["ARCHER_RUN_ID"]
duration = int(os.environ.get("ARCHER_DURATION_SECONDS", "86400"))
profile = os.environ.get("ARCHER_INITIAL_PROFILE", "overnight_balanced_low_churn")
reason = os.environ.get("ARCHER_INITIAL_REASON", "direct normal-spread run; adaptive disabled")

c = AdaptiveController(run_id, duration, 1800, profile, reason)
c.run_dir.mkdir(parents=True, exist_ok=True)
c.log(f"Starting direct Archer run_id={run_id} profile={profile} duration_seconds={duration}")
c.write_active_config(profile, reason)
c.log("Wrote active config; direct runner will clear on exit only if live levels remain")
PY

USE_ADAPTIVE_CONTROLLER="$(printf '%s' "${ARCHER_DIRECT_USE_ADAPTIVE_CONTROLLER:-true}" | tr '[:upper:]' '[:lower:]')"
if [[ "$USE_ADAPTIVE_CONTROLLER" == "1" || "$USE_ADAPTIVE_CONTROLLER" == "true" || "$USE_ADAPTIVE_CONTROLLER" == "yes" || "$USE_ADAPTIVE_CONTROLLER" == "on" ]]; then
  exec python3 scripts/archer_adaptive_12h.py \
    --run-id "$RUN_ID" \
    --duration-seconds "$DURATION_SECONDS" \
    --evaluation-seconds "${ARCHER_EVALUATION_SECONDS:-1800}" \
    --initial-profile "$PROFILE" \
    --initial-reason "$REASON"
fi

PREFLIGHT_URL="${ARCHER_PREFLIGHT_METRICS_URL:-http://archer-dashboard:8787/api/metrics}"
python3 scripts/archer_preflight_gate.py \
  --metrics-url "$PREFLIGHT_URL" \
  --expected-run-id "$RUN_ID" \
  --expected-profile "$PROFILE" \
  --min-effective-spread-bps "$PREFLIGHT_MIN_EFFECTIVE_SPREAD_BPS"

cleanup() {
  python3 - <<'PY'
import os
import sys

sys.path.insert(0, "/app")
from scripts.archer_adaptive_12h import AdaptiveController

run_id = os.environ["ARCHER_RUN_ID"]
duration = int(os.environ.get("ARCHER_DURATION_SECONDS", "86400"))
profile = os.environ.get("ARCHER_INITIAL_PROFILE", "overnight_balanced_low_churn")
reason = os.environ.get("ARCHER_INITIAL_REASON", "direct normal-spread run; adaptive disabled")

c = AdaptiveController(run_id, duration, 1800, profile, reason)
c.run_dir.mkdir(parents=True, exist_ok=True)
c.log("Direct runner exiting; checking Archer book before cleanup")
c.clear_book()
PY
}

trap cleanup EXIT INT TERM

timeout "${DURATION_SECONDS}s" /app/target/release/archer-market-maker run \
  --config /app/config/adaptive-live.toml \
  --live >> "$LOG_DIR/bot.log" 2>&1
