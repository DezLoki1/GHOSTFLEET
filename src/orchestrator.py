"""GhostFleet Gate E orchestrator: scripted (non-LLM) commanders for two sides over the loopback bridge.

Loop per window: for each side GET /state -> policy -> POST /orders -> POST /time?cmd=step -> wait until paused.
Everything is appended to runs/run_<timestamp>.jsonl. Stdlib only. Run: python -I orchestrator.py --windows 6
"""
import argparse, json, os, sys, time

from bridge_client import call, nm, check_state


# ---- deterministic scripted policies ----
def policy_patrol(st, state, points):
    """Alternate own vessels between two points; switch every call when within 2 nm of the current one."""
    orders = []
    for u in st["own_units"]:
        if u["type"] != "Vessel" or not u["controllable"]:
            continue
        i = state.setdefault(u["unit_id"], 0)
        tgt = points[i % len(points)]
        if nm(u["lat"], u["lon"], *tgt) < 2.0:
            i += 1; state[u["unit_id"]] = i; tgt = points[i % len(points)]
        wp = u["waypoint"]
        if not wp or nm(wp["lat"], wp["lon"], *tgt) > 0.5:
            orders.append({"action": "move", "unit_id": u["unit_id"], "lat": tgt[0], "lon": tgt[1]})
    return orders


def policy_intercept(st, state, _points):
    """Each own vessel heads for the nearest tracked non-neutral contact; re-order only if the target moved >1 nm."""
    orders = []
    own = st["side_name"]
    cands = [c for c in st["contacts"] if "lat" in c and c["side"] not in (own, "Neutral")]
    for u in st["own_units"]:
        if u["type"] != "Vessel" or not u["controllable"] or not cands:
            continue
        c = min(cands, key=lambda c: nm(u["lat"], u["lon"], c["lat"], c["lon"]))
        wp = u["waypoint"]
        if not wp or nm(wp["lat"], wp["lon"], c["lat"], c["lon"]) > 1.0:
            orders.append({"action": "move", "unit_id": u["unit_id"], "lat": c["lat"], "lon": c["lon"]})
    return orders


POLICIES = {"patrol": policy_patrol, "intercept": policy_intercept}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", type=int, default=6)
    ap.add_argument("--step", type=float, default=30.0, help="game seconds per decision window")
    ap.add_argument("--scale", type=float, default=4.0, help="time compression during steps")
    ap.add_argument("--sides", default="0,1")
    ap.add_argument("--policy", default="0:patrol,1:intercept")
    ap.add_argument("--patrol", default="13.20,43.00;12.95,43.10", help="lat,lon;lat,lon points for patrol")
    ap.add_argument("--selftest", action="store_true", help="also send a cross-side order and expect rejection")
    a = ap.parse_args()

    sides = [int(s) for s in a.sides.split(",")]
    pol = {int(k): v for k, v in (p.split(":") for p in a.policy.split(","))}
    points = [tuple(map(float, p.split(","))) for p in a.patrol.split(";")]
    pstate = {s: {} for s in sides}
    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs"), exist_ok=True)
    logp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs", time.strftime("run_%Y%m%d_%H%M%S.jsonl"))
    log = open(logp, "a", encoding="utf-8")

    def emit(kind, **kw):
        log.write(json.dumps({"kind": kind, "wall": round(time.time(), 2), **kw}) + "\n"); log.flush()

    code, r = call("POST", "/time?cmd=pause"); assert code == 200, r
    code, r = call("POST", f"/time?cmd=scale&value={a.scale}"); assert code == 200, r
    call("POST", "/time?cmd=pause")
    emit("start", args=vars(a))
    own_ids = {}
    ok_all = True

    for w in range(a.windows):
        ordered = {}
        for s in sides:
            code, st = call("GET", f"/state?side={s}")
            assert code == 200, st
            check_state(st)
            own_ids[s] = {u["unit_id"] for u in st["own_units"]}
            orders = POLICIES[pol[s]](st, pstate[s], points)
            emit("state", window=w, side=s, game_time_s=st["game_time_s"], n_own=len(st["own_units"]), n_contacts=len(st["contacts"]), contacts=st["contacts"])
            if orders:
                code, res = call("POST", f"/orders?side={s}", {"orders": orders})
                emit("orders", window=w, side=s, sent=orders, http=code, result=res)
                assert code == 200, f"orders for side {s} failed: HTTP {code} {res}"  # fail fast, never mislabel
                if not all(x["ok"] for x in res["results"]):
                    ok_all = False
                ordered[s] = orders
        if a.selftest and w == 0 and len(sides) >= 2:
            s0, s1 = sides[0], sides[1]
            victim = sorted(own_ids[s0])[0]
            code, res = call("POST", f"/orders?side={s1}", {"orders": [{"action": "move", "unit_id": victim, "lat": 13.0, "lon": 43.0}]})
            assert code == 200, f"selftest request itself failed: HTTP {code} {res}"
            rejected = not res["results"][0]["ok"]
            emit("selftest_cross_side_order", victim=victim, ordering_side=s1, rejected=rejected, result=res)
            print(f"selftest: side {s1} ordering side {s0}'s unit {victim}: {'REJECTED (good)' if rejected else 'ACCEPTED (BAD)'}")
            ok_all &= rejected
            code, res = call("POST", f"/orders?side={s0}", {"orders": [{"action": "move", "unit_id": victim, "lat": 13.0, "lon": 43.0, "evil": 1}]})
            strict = code == 400
            emit("selftest_unknown_key", http=code, rejected=strict, result=res)
            print(f"selftest: unknown JSON key: HTTP {code} {'(rejected, good)' if strict else '(BAD)'}")
            ok_all &= strict
        code, r = call("POST", f"/time?cmd=step&seconds={a.step}"); assert code == 200, r
        t0 = time.time()
        while True:
            time.sleep(0.5)
            code, st = call("GET", f"/state?side={sides[0]}")
            if code == 200 and st["paused"] and not st["stepping"]:
                break
            assert time.time() - t0 < 300, "step did not finish"
        # verify orders took effect: next state's waypoint matches what we sent
        for s, orders in ordered.items():
            code, st = call("GET", f"/state?side={s}")
            by = {u["unit_id"]: u for u in st["own_units"]}
            applied = 0
            for o in orders:
                wp = by.get(o["unit_id"], {}).get("waypoint")
                if wp and abs(wp["lat"] - o["lat"]) < 1e-3 and abs(wp["lon"] - o["lon"]) < 1e-3:
                    applied += 1
            emit("applied_check", window=w, side=s, sent=len(orders), applied=applied)
            print(f"window {w} side {s}: sent {len(orders)} order(s), waypoint matches on {applied}; t={st['game_time_s']:.0f}s contacts={len(st['contacts'])}")
            ok_all &= applied == len(orders)
    emit("end", ok=ok_all)
    print("RESULT:", "OK" if ok_all else "PROBLEMS", "log:", logp)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
