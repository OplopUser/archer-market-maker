#!/usr/bin/env bash
set -u

ROOT="/Users/jeromehainaut/propamm/archer-market-maker"
PORT="${ARCHER_DASHBOARD_PORT:-8787}"
HOST="${ARCHER_DASHBOARD_HOST:-127.0.0.1}"
CONFIG="${ARCHER_DASHBOARD_CONFIG:-$ROOT/config/live-usdc-style-12h.toml}"
RUN_DIR="${ARCHER_DASHBOARD_RUN_DIR:-}"

cd "$ROOT" || exit 1

args=(
  "--host" "$HOST"
  "--port" "$PORT"
  "--config" "$CONFIG"
)

if [ -n "$RUN_DIR" ]; then
  args+=("--run-dir" "$RUN_DIR")
fi

python3 dashboard/server.py "${args[@]}"
