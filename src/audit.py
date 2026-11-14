"""Audit a match dir for two things: (1) whether commanders keep ordering shots at dead/lost tracks, (2) weapon spend by type per side.
python audit.py runs/match_<ts>"""
import json, re, sys

ev = [json.loads(l) for l in open(sys.argv[1] + "/events.jsonl", encoding="utf-8")]
dec = [e for e in ev if e["kind"] == "decision"]
for side in (0, 1):
    print(f"=== side {side}")
    prev_weap, spent = None, {}
    for e in [d for d in dec if d["side"] == side]:
        p = e["prompt"]
        tracks = {int(m.group(1)): m.group(0) for m in re.finditer(r"^\s+(\d{4,}) \| .*$", p, re.M)}
        engages = [(o, r) for o, r in zip(e["orders"], e["results"]) if o["action"] == "engage"]
        dead = [(o["track_id"], r["message"]) for o, r in engages if not r["ok"] and "destroyed" in r["message"]]
        in_table = [t for t, _ in dead if t in tracks]
        print(f"w{e['w'] + 1}: engage {len(engages)} (ok {sum(r['ok'] for _, r in engages)}), refused-as-dead {len(dead)}, of which still listed in the prompt's track table: {len(in_table)}")
        weap = {}
        for m in re.finditer(r"^\s+(\d+): (.+)$", p.split("YOUR TRACKS")[0].split("YOUR WEAPONS")[-1], re.M):
            for w in re.finditer(r"(\S+) x(\d+) \((\w+)", m.group(2)):
                weap[(int(m.group(1)), w.group(1))] = int(w.group(2))
        if prev_weap:
            for k, v in prev_weap.items():
                if weap.get(k, v) < v:
                    spent[k] = spent.get(k, 0) + v - weap[k]
        prev_weap = weap
    print("spent between windows (unit, weapon): count ->", dict(sorted(spent.items(), key=lambda kv: -kv[1])[:10]))
