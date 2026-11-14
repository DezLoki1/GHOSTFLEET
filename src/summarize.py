"""Quick analysis of a match dir: python summarize.py runs/match_<ts>
Per side: how many engage orders were accepted/refused (and why), and total ammo before vs after (from the omniscient log)."""
import collections, json, sys

ev = [json.loads(l) for l in open(sys.argv[1] + "/events.jsonl", encoding="utf-8")]
use = collections.defaultdict(lambda: [0, 0, 0, []])
for e in ev:
    if e["kind"] != "decision":
        continue
    for o, r in zip(e["orders"], e["results"]):
        if o["action"] == "engage":
            u = use[e["side"]]
            u[0] += 1; u[1] += bool(r["ok"]); u[2] += not r["ok"]
            if not r["ok"] and r["message"] not in u[3] and len(u[3]) < 4:
                u[3].append(r["message"])
for s in (0, 1):
    print(f"side{s}: engage orders {use[s][0]}  accepted {use[s][1]}  refused {use[s][2]}  refusal reasons {use[s][3]}")
w = [e for e in ev if e["kind"] == "window"]
for fs, ls in zip(w[0]["truth"]["sides"][:2], w[-1]["truth"]["sides"][:2]):
    ammo = lambda side: sum(u[6] or 0 for u in side["units"])
    print(f"{fs['name']}: ammo {ammo(fs)} -> {ammo(ls)} (spent {ammo(fs) - ammo(ls)}), units alive {sum(u[3] for u in ls['units'])}/{len(fs['units'])}, kills {ls['kill']}, losses {ls['loss']}")
