#!/usr/bin/env bash
set -u

ROOT="/Users/jeromehainaut/propamm/archer-market-maker"
cd "$ROOT" || exit 1

python3 scripts/archer_adaptive_12h.py "$@"
