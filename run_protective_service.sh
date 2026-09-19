#!/usr/bin/env bash
# CPU-only protective path; no model lease.
set -uo pipefail
cd "$HOME/exitmgr-app" || exit 1

export PYTHONUNBUFFERED=1
export EXITMGR_ORDER_LOCK="${EXITMGR_ORDER_LOCK:-$HOME/.local/var/exitmgr/order-mutation.lock}"
export TRADER_CAPTURE_EXIT_IDENTITY=1
export EXITMGR_ROTATE_SERVICE_LOGS=1
exec "$HOME/ib-grader-venv/bin/python" run_trader.py --arm --loop --mode protective \
  --client-id "${PROTECTIVE_IB_CLIENT_ID:-189}" \
  --quote-client-id "${PROTECTIVE_QUOTE_IB_CLIENT_ID:-118}" \
  --protective-interval 30
