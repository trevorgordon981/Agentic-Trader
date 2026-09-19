"""Public API contract; production-derived narrative omitted."""
import json, os, shutil, subprocess, sys, datetime

APP = os.path.expanduser("~/exitmgr-app")
EXITS = os.path.join(APP, "exits.log")
PROP = os.path.join(APP, "data", "commission_backfill_proposal.json")



ps = subprocess.run(["pgrep", "-f", "run_trader.py"], capture_output=True, text=True).stdout.strip()
if ps:
    print("REFUSING: run_trader.py still running (pids %s). Stop the loops first."
          % ps.replace("\n", ","), file=sys.stderr)
    raise SystemExit(2)

doc = json.load(open(PROP))
props = {p["row"]: p for p in doc["proposals"]}
if not props:
    print("nothing to apply"); raise SystemExit(0)

rows = [json.loads(l) for l in open(EXITS) if l.strip()]
stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
backup = EXITS + ".pre-commission-backfill-" + stamp
shutil.copy2(EXITS, backup)
print("  backup: %s" % os.path.basename(backup))

applied = 0
for i, p in props.items():
    r = rows[i]
    for field, value in p["changes"].items():
        if r.get(field) is not None:
            print("  SKIP row %d %s: already present" % (i, field)); continue
        r[field] = value

        r[field + "_source"] = p["evidence"][field]["source"]
        r[field + "_trade_ids"] = p["evidence"][field]["trade_ids"]
        applied += 1

    r["commission_unknown"] = (r.get("entry_commission") is None
                               or r.get("exit_commission") is None)
    r["commission_backfilled_" + stamp[:8]] = True

tmp = EXITS + ".tmp"
with open(tmp, "w") as fh:
    for r in rows:
        fh.write(json.dumps(r) + "\n")
os.replace(tmp, EXITS)
print("  applied %d field(s) across %d row(s)" % (applied, len(props)))
print("  rows in file: %d (was %d)" % (sum(1 for _ in open(EXITS)), len(rows)))
