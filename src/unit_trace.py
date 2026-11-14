"""Follow chosen units through a match: position/distance to a reference point per window plus every order that touched them.
python unit_trace.py runs/match_<ts> 369 377 --ref 31.34 16.93 [--side 0]"""
import argparse, json
from bridge_client import nm

ap = argparse.ArgumentParser()
ap.add_argument("dir"); ap.add_argument("units", nargs="+", type=int)
ap.add_argument("--ref", nargs=2, type=float, help="lat lon to measure distance from")
ap.add_argument("--side", type=int, default=0)
a = ap.parse_args()
ev = [json.loads(l) for l in open(a.dir + "/events.jsonl", encoding="utf-8")]
for e in ev:
    if e["kind"] == "window":
        t = e["truth"]
        u = {x[0]: x for s in t["sides"] for x in s["units"]}
        def d(i):
            x = u.get(i)
            if x is None: return "gone"
            if not x[3]: return "DEAD"
            return f"{nm(x[4], x[5], *a.ref):.0f}nm" if a.ref else f"{x[4]:.2f},{x[5]:.2f}"
        print(f"W{e['w'] + 1} t={t['t']:.0f}  " + "  ".join(f"{i}:{d(i)}" for i in a.units) + "  | " + " ".join(f"{s['name']} alive {s['alive']} loss {s['loss']:g}" for s in t["sides"][:2]))
    elif e["kind"] == "decision" and e["side"] == a.side:
        for rc in e.get("receipts", []):
            o = rc["order"]
            if o.get("unit_id") in a.units:
                extra = {k: v for k, v in o.items() if k not in ("action", "unit_id")}
                print(f"     {rc['order_id']} {o['unit_id']} {o['action']} {extra} -> {'OK' if rc['ok'] else 'REFUSED'}: {rc['message'][:120]}")
