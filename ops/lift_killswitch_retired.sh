#!/bin/bash
set -u

# Permanently retired: no process may remove KILL_SWITCH automatically. Re-arming requires an
# explicit human action after verifying the account, broker, runtime and protective-exit service.
LOG="$HOME/Library/Logs/killswitch-lift.log"
mkdir -p "$(dirname "$LOG")"
echo "$(date) automatic KILL_SWITCH lift REFUSED (feature permanently retired)" >> "$LOG"
exit 78
