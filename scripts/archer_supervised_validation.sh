#!/usr/bin/env bash
set -euo pipefail

RUN_ID="${ARCHER_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
DURATION_SECONDS="${ARCHER_DURATION_SECONDS:-900}"
PROFILE="${ARCHER_INITIAL_PROFILE:-overnight_balanced_low_churn}"
REASON="${ARCHER_INITIAL_REASON:-supervised gated validation run}"
COMPOSE_FILE="${ARCHER_COMPOSE_FILE:-docker-compose.tuxedo.yml}"
RUN_DIR="logs/adaptive-12h-${RUN_ID}"

read -r DEFAULT_PREFLIGHT_MIN_SPREAD DEFAULT_POST_GATE_SPREAD_FLOOR DEFAULT_POST_GATE_MIN_NET DEFAULT_POST_GATE_MAX_BREAK_EVEN DEFAULT_POST_GATE_MAX_TX_PER_HOUR < <(
  ARCHER_INITIAL_PROFILE="$PROFILE" python3 - <<'PY'
import os
import sys

sys.path.insert(0, os.getcwd())
from scripts.archer_adaptive_12h import profile_validation_defaults

defaults = profile_validation_defaults(os.environ["ARCHER_INITIAL_PROFILE"])
print(
    defaults["preflight_min_effective_spread_bps"],
    defaults["post_gate_spread_floor_bps"],
    defaults["post_gate_min_net_usdc"],
    defaults["post_gate_max_break_even_bps"],
    defaults["post_gate_max_tx_per_hour"],
)
PY
)

PREFLIGHT_MIN_EFFECTIVE_SPREAD_BPS="${ARCHER_PREFLIGHT_MIN_EFFECTIVE_SPREAD_BPS:-$DEFAULT_PREFLIGHT_MIN_SPREAD}"
POST_GATE_SPREAD_FLOOR_BPS="${ARCHER_POST_GATE_SPREAD_FLOOR_BPS:-$DEFAULT_POST_GATE_SPREAD_FLOOR}"
POST_GATE_MIN_NET_USDC="${ARCHER_POST_GATE_MIN_NET_USDC:-$DEFAULT_POST_GATE_MIN_NET}"
POST_GATE_MAX_BREAK_EVEN_BPS="${ARCHER_POST_GATE_MAX_BREAK_EVEN_BPS:-$DEFAULT_POST_GATE_MAX_BREAK_EVEN}"
POST_GATE_MAX_TX_PER_HOUR="${ARCHER_POST_GATE_MAX_TX_PER_HOUR:-$DEFAULT_POST_GATE_MAX_TX_PER_HOUR}"
DRY_RUN="${ARCHER_SUPERVISED_DRY_RUN:-false}"
LIVE_CONFIRM="${ARCHER_CONFIRM_LIVE_VALIDATION:-false}"

mkdir -p "$RUN_DIR"

controller_state="$(docker inspect -f '{{.State.Running}}' archer-controller 2>/dev/null || true)"
runner_state="$(docker inspect -f '{{.State.Running}}' archer-normal-runner 2>/dev/null || true)"
if [[ "$controller_state" == "true" ]]; then
  echo "Refusing to start: archer-controller is running"
  exit 1
fi
if [[ "$runner_state" == "true" ]]; then
  echo "Refusing to start: archer-normal-runner is already running"
  exit 1
fi
if docker inspect archer-normal-runner >/dev/null 2>&1; then
  echo "Removing stopped archer-normal-runner container before supervised start"
  docker rm archer-normal-runner >/dev/null
fi
if [[ "$DRY_RUN" != "true" && "$LIVE_CONFIRM" != "true" ]]; then
  echo "Refusing to start live validation without ARCHER_CONFIRM_LIVE_VALIDATION=true"
  echo "Use ARCHER_SUPERVISED_DRY_RUN=true to validate gates without starting the runner."
  exit 1
fi

run_preflight() {
  docker exec archer-dashboard python3 scripts/archer_preflight_gate.py \
    --metrics-url http://127.0.0.1:8787/api/metrics \
    --wait-seconds "${ARCHER_PREFLIGHT_WAIT_SECONDS:-45}" \
    --expected-run-id "$RUN_ID" \
    --expected-profile "$PROFILE" \
    --min-effective-spread-bps "$PREFLIGHT_MIN_EFFECTIVE_SPREAD_BPS"
}

run_post_gate() {
  if ! docker exec archer-dashboard test -f "/app/${RUN_DIR}/dashboard-samples.jsonl"; then
    echo "Post-run gate cannot run: /app/${RUN_DIR}/dashboard-samples.jsonl is missing"
    exit 1
  fi
  docker exec archer-dashboard python3 scripts/analyze_dashboard_samples.py \
    --spread-floors "$POST_GATE_SPREAD_FLOOR_BPS" \
    --gate \
    --gate-spread-floor-bps "$POST_GATE_SPREAD_FLOOR_BPS" \
    --min-spread-floor-net-usdc "$POST_GATE_MIN_NET_USDC" \
    --max-break-even-floor-bps "$POST_GATE_MAX_BREAK_EVEN_BPS" \
    --max-tx-per-hour "$POST_GATE_MAX_TX_PER_HOUR" \
    --max-failed-tx 0 \
    --max-priority-fee-sampling-failures 0 \
    --max-rpc-429 0 \
    --max-price-feed-stale 0 \
    "/app/${RUN_DIR}"
}

echo "Preparing active profile config for run_id=${RUN_ID}"
echo "Validation defaults: preflight_min_effective_spread_bps=${PREFLIGHT_MIN_EFFECTIVE_SPREAD_BPS} post_gate_spread_floor_bps=${POST_GATE_SPREAD_FLOOR_BPS} post_gate_max_break_even_bps=${POST_GATE_MAX_BREAK_EVEN_BPS} post_gate_max_tx_per_hour=${POST_GATE_MAX_TX_PER_HOUR}"
python3 - <<'PY'
import os
import sys

sys.path.insert(0, os.getcwd())
from scripts.archer_adaptive_12h import AdaptiveController

run_id = os.environ["ARCHER_RUN_ID"]
duration = int(os.environ.get("ARCHER_DURATION_SECONDS", "900"))
profile = os.environ.get("ARCHER_INITIAL_PROFILE", "overnight_balanced_low_churn")
reason = os.environ.get("ARCHER_INITIAL_REASON", "supervised gated validation run")

c = AdaptiveController(run_id, duration, 1800, profile, reason)
c.run_dir.mkdir(parents=True, exist_ok=True)
c.write_active_config(profile, reason)
c.log(f"Prepared active config for supervised validation profile={profile} duration_seconds={duration}")
PY

echo "Preparing dashboard for run_id=${RUN_ID}"
ARCHER_RUN_ID="$RUN_ID" \
ARCHER_DURATION_SECONDS="$DURATION_SECONDS" \
ARCHER_DASHBOARD_COMMAND_TIMEOUT="${ARCHER_DASHBOARD_COMMAND_TIMEOUT:-8}" \
ARCHER_DASHBOARD_RPC_TIMEOUT="${ARCHER_DASHBOARD_RPC_TIMEOUT:-4}" \
  docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate archer-dashboard

echo "Running preflight gate"
run_preflight

if [[ "$DRY_RUN" == "true" ]]; then
  echo "Dry-run mode: skipping live runner start"
  echo "Running post-run profitability/churn gate against existing samples"
  run_post_gate
  echo "Supervised dry-run completed: run_id=${RUN_ID}"
  exit 0
fi

echo "Starting supervised direct runner for ${DURATION_SECONDS}s"
set +e
ARCHER_RUN_ID="$RUN_ID" \
ARCHER_DURATION_SECONDS="$DURATION_SECONDS" \
ARCHER_INITIAL_PROFILE="$PROFILE" \
ARCHER_INITIAL_REASON="$REASON" \
ARCHER_PREFLIGHT_METRICS_URL="${ARCHER_PREFLIGHT_METRICS_URL:-http://archer-dashboard:8787/api/metrics}" \
  docker compose -f "$COMPOSE_FILE" --profile direct up --abort-on-container-exit --exit-code-from archer-normal-runner archer-normal-runner
runner_exit=$?
set -e

echo "Refreshing dashboard after runner exit"
ARCHER_RUN_ID="$RUN_ID" \
ARCHER_DURATION_SECONDS="$DURATION_SECONDS" \
ARCHER_DASHBOARD_COMMAND_TIMEOUT="${ARCHER_DASHBOARD_COMMAND_TIMEOUT:-8}" \
ARCHER_DASHBOARD_RPC_TIMEOUT="${ARCHER_DASHBOARD_RPC_TIMEOUT:-4}" \
  docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate archer-dashboard

if [[ "$runner_exit" -ne 0 && "$runner_exit" -ne 124 ]]; then
  echo "Runner exited with unexpected status ${runner_exit}; leaving logs for review"
  exit "$runner_exit"
fi

echo "Running post-run profitability/churn gate"
run_post_gate

echo "Supervised validation completed: run_id=${RUN_ID}"
