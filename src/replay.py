"""Spectator/AAR text replay of a recorded match: python replay.py runs/match_<ts>/ [--raw] [--prompts]
--raw adds each model's full reply; --prompts adds the exact input each commander saw (matches from before the reply was logged have no raw replies).
Shows, per window: omniscient force status (from /truth, spectator only), each commander's assessment, orders, refusals, tokens."""
import json, os, sys


def main(path, show_prompts=False, show_raw=False):
    with open(os.path.join(path, "events.jsonl"), encoding="utf-8") as f:
        ev = [json.loads(l) for l in f]
    for e in ev:
        k = e["kind"]
        if k == "match_start":
            print(f"MATCH {e['scenario']}  windows={e['windows']} step={e['step']}s scale={e['scale']}  side0={e['specs']['0']}  side1={e['specs']['1']}")
        elif k == "window":
            t = e["truth"]
            print(f"\n--- window {e['w'] + 1}  t={t['t']:.0f}s  " + "  ".join(f"{s['name']}: {s['alive']}/{s['total']} alive (kills {s['kill']:.0f}, losses {s['loss']:.0f})" for s in t["sides"]))
        elif k == "decision":
            u = e["usage"]
            print(f"  side{e['side']}: {len(e['orders'])} order(s), {len(e['rejected'])} invalid, {sum(1 for r in e['results'] if not r['ok'])} refused"
                  + (f", tokens in/out {u.get('in', 0)}/{u.get('out', 0)}" if u else "") + (f"  ERROR {e['error']}" if e["error"] else ""))
            if e["assessment"]:
                print(f"     assessment: {e['assessment']}")
            if e.get("notes"):
                print(f"     notes (memory for next window): {e['notes']}")
            for o in e["orders"]:
                print("     order:", json.dumps(o))
            for o, why in e["rejected"]:
                print(f"     INVALID {json.dumps(o)}: {why}")
            for rc in e.get("receipts", []):
                if not rc["ok"]:
                    print(f"     REFUSED {rc['order_id']} {json.dumps(rc['order'])}: {rc['message']}")
            if not e.get("receipts"):   # matches logged before receipts existed
                for r in e["results"]:
                    if not r["ok"]:
                        print(f"     REFUSED unit {r['unit_id']} {r['action']}: {r['message']}")
            if e.get("spent_since_last_window"):
                print(f"     rounds expended since last window: {e['spent_since_last_window']}")
            if show_raw and e.get("reply"):
                print("     --- raw model reply ---\n" + "\n".join("     > " + l for l in e["reply"].splitlines()))
            if show_prompts:
                print("     --- prompt ---\n" + "\n".join("     | " + l for l in e["prompt"].splitlines()))
        elif k == "match_end":
            print(f"\nRESULT: winner side {e['winner_side']} ({e['reason']}); windows played {e['windows_played']}; budgets {e['budgets']}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1], "--prompts" in sys.argv, "--raw" in sys.argv)
