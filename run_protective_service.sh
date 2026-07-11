#!/usr/bin/env bash
# CPU-only, model-free protective exit service. Safe to keep running while training is using the GPU.
set -uo pipefail
cd "$HOME/exitmgr-app" || exit 1

# TRADING_DOWN blocks risk-adding BUYs, never protective SELLs. This process has no entry loop.
export PYTHONUNBUFFERED=1
export EXITMGR_ORDER_LOCK="${EXITMGR_ORDER_LOCK:-$HOME/.local/var/exitmgr/order-mutation.lock}"
exec "$HOME/ib-grader-venv/bin/python" run_trader.py --arm --loop --mode protective \
  --client-id "${PROTECTIVE_IB_CLIENT_ID:-189}" \
  --protective-interval 30
