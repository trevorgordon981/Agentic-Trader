# IB Gateway + IBC setup (live, headless on your-host)

The whole software system is built, tested (101 green), and live-configured (port 4001,
#trading-approvals). The only thing left is a logged-in IB Gateway. This is hands-on because it
needs your IBKR credentials and — unavoidably for a **live** account — your 2FA.

## The 2FA reality (read first)
Live IBKR accounts require two-factor auth. IBC auto-enters your username/password, but IBKR
forces a re-login on a daily restart and pushes a prompt to your phone that **you tap**. So this
is "turnkey except ~one phone tap per day." That's IBKR's rule, not the system's.

## Steps
1. **Install IB Gateway (arm64) on your-host.** Download the *Stable* macOS gateway from
   https://www.interactivebrokers.com/en/trading/ibgateway-stable.php — run it once in the console
   GUI session, log in to your **live** account, approve the 2FA, and in Configure → API → Settings:
   enable ActiveX/Socket clients, Socket port = **4001**, Trusted IPs include 127.0.0.1,
   uncheck "Read-Only API". Note the install dir (usually `~/Jts`) and the version number.
2. **Fill IBC creds.** `cp ~/ibc/config.ini.template ~/ibc/config.ini`, put your live
   `IbLoginId` / `IbPassword` in it, set `TWS_MAJOR_VRSN` to the gateway version, then
   `chmod 600 ~/ibc/config.ini` (I never see or store these).
3. **Start it** (auto-login + auto-restart handled by IBC):
   `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.alfred.ibgateway.plist`
   Approve the 2FA on your phone. Confirm it's listening: `lsof -iTCP:4001 -sTCP:LISTEN`.
4. **Start the trader** (dry-run first cycle is fine — it just posts to Slack):
   `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.alfred.trader.plist`
   You'll get a proposal in #trading-approvals showing the exact order. Reply "send it" to fire it.

## Daily
- IB Gateway restarts ~11:45 PM and re-prompts 2FA → tap your phone. Everything else is automatic.
- Kill switch: `touch ~/exitmgr-app/KILL_SWITCH` halts all order placement next cycle.
