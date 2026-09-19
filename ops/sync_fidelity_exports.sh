#!/bin/bash
set -uo pipefail

SRC="$HOME/fidelity-exports"
DOWNLOADS="$HOME/Downloads"
STAGE="$HOME/scripts/fidelity_stage.sh"
HEARTBEAT="$SRC/.staged_at"
STALE_AFTER=129600                              # 36h without a staging pass is a real fault
REMOTE="trader_host"
REMOTE_DIR="fidelity-exports"
STAMP="$HOME/.fidelity-sync.state"

SSH_IDENTITY="$HOME/.ssh/id_ed25519.pub"
SSH_OPTS=(
  -o BatchMode=yes
  -o ConnectTimeout=15
  -o IdentitiesOnly=yes
  -o IdentityFile="$SSH_IDENTITY"
)
RSYNC_RSH="ssh -o BatchMode=yes -o ConnectTimeout=15 -o IdentitiesOnly=yes -o IdentityFile=$SSH_IDENTITY"

echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') fidelity export sync ==="

[ -x "$STAGE" ] && "$STAGE" --force 2>/dev/null

if ! /bin/ls "$SRC" >/dev/null 2>&1; then
  echo "FATAL: cannot read $SRC" >&2
  exit 1
fi

if [ -f "$HEARTBEAT" ]; then
  age=$(( $(date +%s) - $(cat "$HEARTBEAT" 2>/dev/null || echo 0) ))
else
  age=999999
fi
if [ "$age" -gt "$STALE_AFTER" ] && ! /bin/ls "$DOWNLOADS" >/dev/null 2>&1; then
  hrs=$(( age / 3600 ))
  echo "WARNING: staging has not run in ${hrs}h and Downloads is unreadable here (TCC)." >&2
  ssh "${SSH_OPTS[@]}" "$REMOTE" \
    "cd ~/exitmgr-app && PYTHONPATH=\$HOME/exitmgr-app \$HOME/ib-grader-venv/bin/python \
     \$HOME/exitmgr-app/fidelity_freshness.py --sync-blocked \
     'No Fidelity export has been staged out of ~/Downloads in '"${hrs}"'h. Opening any \
terminal stages them automatically; run ~/scripts/fidelity_stage.sh --force to do it now.'" 2>&1 | tail -3
fi

shopt -s nullglob
files=("$SRC"/Accounts_History*.csv)
if [ ${#files[@]} -eq 0 ]; then
  echo "no Accounts_History*.csv in $SRC -- nothing to sync"
  exit 0
fi
echo "found ${#files[@]} export(s)"

sync_primary() {
  local attempt rc
  for attempt in 1 2 3; do
    if rsync -a --times --checksum -e "$RSYNC_RSH" \
      "${files[@]}" "$REMOTE:$REMOTE_DIR/"; then
      return 0
    else
      rc=$?
    fi
    if [ "$attempt" -lt 3 ]; then
      echo "primary rsync attempt $attempt failed; retrying in $((attempt * 5))s" >&2
      sleep $((attempt * 5))
    fi
  done
  return "$rc"
}

if ! sync_primary; then
  echo "FATAL: rsync to $REMOTE failed" >&2
  exit 1
fi
echo "synced ${#files[@]} export(s) to $REMOTE:$REMOTE_DIR/"

rsync -a --times --checksum -e "$RSYNC_RSH" "${files[@]}" "$REMOTE:Downloads/" \
  || echo "note: cross_book mirror to ~/Downloads failed (pipeline unaffected)" >&2

ssh "${SSH_OPTS[@]}" "$REMOTE" '~/exitmgr-app/run_fidelity_curate.sh' 2>&1 \
  | tail -12
rc=${PIPESTATUS[0]}

date +%s > "$STAMP"
echo "remote curate exit=$rc"
[ "$rc" -le 1 ] && exit 0 || exit "$rc"
