"""Compact per-window view of a match: orders by action per side, losses (names) per window, last assessment.
python timeline.py runs/match_<ts>"""
import collections, json, sys

ev = [json.loads(l) for l in open(sys.argv[1] + "/events.jsonl", encoding="utf-8")]
alive_prev = {}
for e in ev:
    if e["kind"] == "window":
        t = e["truth"]
        now = {(s["side"], u[0]): (u[1], u[3]) for s in t["sides"] for u in s["units"]}
        died = [f"s{k[0]}:{v[0]}" for k, v in now.items() if not v[1] and alive_prev.get(k, (None, True))[1]]
        gone = [f"s{k[0]}:{v[0]}(removed)" for k, v in alive_prev.items() if k not in now and v[1]]
        alive_prev = now
        print(f"\n== window {e['w'] + 1} t={t['t']:.0f}s  " + " | ".join(f"{s['name']} {s['alive']}/{s['total']}" for s in t["sides"]) + (f"  LOST: {died + gone}" if died or gone else ""))
    elif e["kind"] == "decision":
        c = collections.Counter(o["action"] for o in e["orders"])
        refused = [rc["message"][:60] for rc in e.get("receipts", []) if not rc["ok"]]
        print(f"  side{e['side']}: {dict(c) or 'no orders'}" + (f" refused={refused[:3]}" if refused else "") + (f" ERR {e['error']}" if e["error"] else "")
              + f"\n      {e['assessment'][:230]}")
