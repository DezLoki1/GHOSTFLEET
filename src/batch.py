"""Run several matches back to back with no clicking: every match restarts the loaded mission through the game first.
  python batch.py --n 4 --a claudecli:sonnet@low --b claudecli:haiku@low --swap --label sonnet_haiku --max-total-usd 6 -- --max-usd 3
--swap     alternate which side each model plays (match 1: A is side 0, match 2: A is side 1, ...), so a model comparison is not a side comparison
--max-total-usd  stop starting new matches once the combined reported cost passes this
Everything after a bare -- is handed to match.py (e.g. --max-usd, --scale, --windows). Writes runs/batch_<ts>/batch_report.txt with one row per match."""
import argparse, glob, json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    argv = sys.argv[1:]
    extra = argv[argv.index("--") + 1:] if "--" in argv else []
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--a", required=True, help="commander spec A")
    ap.add_argument("--b", required=True, help="commander spec B")
    ap.add_argument("--swap", action="store_true")
    ap.add_argument("--scenario", default=os.path.join(HERE, "scenarios", "action_in_the_gulf_of_sidra_1986.json"))
    ap.add_argument("--label", default="batch")
    ap.add_argument("--max-total-usd", type=float, default=0.0)
    a = ap.parse_args(argv[:argv.index("--")] if "--" in argv else argv)

    out = os.path.join(HERE, "runs", time.strftime("batch_%Y%m%d_%H%M%S") + "_" + a.label)
    os.makedirs(out)
    rows, total, per = [], 0.0, {}   # per: model -> list of that model's per-match score dicts
    for i in range(a.n):
        if a.max_total_usd and total >= a.max_total_usd:
            rows.append({"i": i + 1, "note": f"not started: batch cost cap {a.max_total_usd} reached"}); break
        s0, s1 = (a.b, a.a) if (a.swap and i % 2 == 1) else (a.a, a.b)
        label = f"{a.label}_{i + 1}"
        cmd = [sys.executable, os.path.join(HERE, "match.py"), "--scenario", a.scenario, "--side0", s0, "--side1", s1,
               "--restart", "--confirm-spend", "--label", label] + extra
        print(f"[batch] match {i + 1}/{a.n}: side0={s0} side1={s1}", flush=True)
        rc = subprocess.call(cmd)
        dirs = sorted(glob.glob(os.path.join(HERE, "runs", f"match_*_{label}")))
        row = {"i": i + 1, "side0": s0, "side1": s1, "rc": rc}
        if dirs and os.path.exists(os.path.join(dirs[-1], "summary.json")):
            with open(os.path.join(dirs[-1], "summary.json"), encoding="utf-8") as f:
                s = json.load(f)
            cost = sum(b["usd"] for b in s["budgets"].values())
            total += cost
            w = s["winner_side"]
            for side, spec in (("0", s0), ("1", s1)):
                if side in s.get("score", {}):
                    per.setdefault(spec, []).append(dict(s["score"][side], side=int(side)))
            row.update(winner_side=w, winner=(s0 if w == 0 else s1 if w == 1 else None), reason=s["reason"], windows=s["windows_played"],
                       cost=round(cost, 3), report=os.path.join(dirs[-1], "report.txt"))
        else:
            row["note"] = "no summary: the match failed to finish; stopping the batch"
            rows.append(row); break
        rows.append(row)

    lines = [f"BATCH {a.label}: {len(rows)} match(es), total reported cost ${total:.2f}", f"scenario: {os.path.basename(a.scenario)}   A={a.a}   B={a.b}   swap={a.swap}", ""]
    wins = {}
    for r in rows:
        if "side0" not in r:
            lines.append(f"match {r['i']}: {r['note']}"); continue
        if r.get("winner"):
            wins[r["winner"]] = wins.get(r["winner"], 0) + 1
        lines.append(f"match {r['i']}: side0={r['side0']} side1={r['side1']} -> winner side {r.get('winner_side')} ({r.get('winner') or 'no winner'}) | {r.get('reason', r.get('note'))} | "
                     f"{r.get('windows', '?')} windows | ${r.get('cost', '?')}\n    report: {r.get('report', '-')}")
    mean = lambda xs: sum(xs) / len(xs)
    lines += ["", "BY MODEL (game scoring units; margin = (kills-losses)/(kills+losses), +1 best; result 1 win / 0 loss / 0.5 no verdict):"]
    for spec, ms in per.items():
        lines.append(f"  {spec}: n={len(ms)}  result {mean([m['result'] for m in ms]):.2f}  margin {mean([m['margin'] for m in ms]):+.2f}  units lost {mean([m['units_lost'] for m in ms]):.1f}"
                     f"  game min {mean([m['game_s'] for m in ms]) / 60:.0f}  cost ${mean([m['usd'] for m in ms]):.2f}  (as Blue/side0: {sum(m['side'] == 0 for m in ms)}, side1: {sum(m['side'] == 1 for m in ms)})")
    lines += ["", "WINS BY MODEL: " + (", ".join(f"{k}: {v}" for k, v in wins.items()) or "none")]
    path = os.path.join(out, "batch_report.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines)); print("batch report:", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
