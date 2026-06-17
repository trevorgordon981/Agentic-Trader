import json, os, time, subprocess, urllib.request, urllib.parse
CHANNEL="YOUR_SLACK_CHANNEL_ID"; APPROVERS={"YOUR_SLACK_USER_ID"}; PHRASE="liquidate everything"; POLL=15
STATE=os.path.expanduser("~/exitmgr-app/.liq_listener_state.json")
PYBIN=os.path.expanduser("~/ib-grader-venv/bin/python"); LIQ=os.path.expanduser("~/exitmgr-app/liquidate.py")
def tok():
    for l in open(os.path.expanduser("~/.hermes/.env")):
        if l.startswith("SLACK_BOT_TOKEN="): return l.split("=",1)[1].strip().strip('"').strip("'")
def hist(t):
    u="https://slack.com/api/conversations.history?"+urllib.parse.urlencode({"channel":CHANNEL,"limit":15})
    return json.load(urllib.request.urlopen(urllib.request.Request(u,headers={"Authorization":"Bearer "+t}),timeout=10))
def post(t,m):
    urllib.request.urlopen(urllib.request.Request("https://slack.com/api/chat.postMessage",
      data=json.dumps({"channel":CHANNEL,"text":m}).encode(),
      headers={"Authorization":"Bearer "+t,"Content-Type":"application/json"}),timeout=10)
def seen():
    try: return float(json.load(open(STATE)).get("ts","0"))
    except: return 0.0
def save(ts): json.dump({"ts":str(ts)},open(STATE,"w"))
t=tok(); s=seen() or time.time(); save(s)
print("liquidate_listener up",flush=True)
while True:
    try:
        for m in sorted(hist(t).get("messages",[]),key=lambda x:float(x.get("ts","0"))):
            ts=float(m.get("ts","0"))
            if ts<=s: continue
            if m.get("user","") in APPROVERS and (m.get("text","") or "").strip().lower()==PHRASE:
                s=ts; save(s); post(t,":rotating_light: Liquidate command received — closing ALL positions at market...")
                r=subprocess.run([PYBIN,LIQ,"--confirm","--client-id","91"],capture_output=True,text=True,timeout=200)
                print("liq rc",r.returncode,r.stdout[-300:],flush=True)
            else: s=max(s,ts)
        save(s)
    except Exception as e: print("poll err",e,flush=True)
    time.sleep(POLL)
EOF