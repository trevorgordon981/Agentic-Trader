# Trading operations safety

`lift_killswitch_retired.sh` is the canonical replacement for the former monthly LaunchAgent
target. It never deletes `KILL_SWITCH`; it records and refuses every invocation. Production must
also keep `ai.alfred.killswitch-lift` disabled and its plist outside `~/Library/LaunchAgents`.
