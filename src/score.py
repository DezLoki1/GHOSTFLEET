"""Margin scoring: win/loss is too coarse when one side usually wins. Per side, from the game's own kill/loss scores:
  net      = kills - losses                    (game scoring units)
  margin   = (kills - losses) / (kills + losses), -1..+1, 0 if nothing happened
  result   = 1 win, 0 loss, 0.5 no verdict
plus game seconds, cost, units lost (dead units stay in the game's unit list, so total - alive). Used by match.py (summary.json "score") and by batch.py for per-model means.
Rescore an old run:  python score.py runs/match_<ts>"""
import json, os, sys


def score_match(first_truth, final_truth, winner, usd):
    """first/final_truth are compact_truth dicts: {"t":..,"sides":[{"side","name","kill","loss","alive","total"}]}"""
    secs = round(final_truth["t"] - first_truth["t"])
    out = {}
    for s in final_truth["sides"]:
        if s["name"] == "Neutral":
            continue
        k, l = s["kill"], s["loss"]
        out[str(s["side"])] = {"result": 0.5 if winner is None else (1.0 if winner == s["side"] else 0.0),
                               "kills": k, "losses": l, "net": round(k - l, 2), "margin": round((k - l) / (k + l), 3) if k + l else 0.0,
                               "units_lost": s["total"] - s["alive"], "game_s": secs, "usd": usd.get(str(s["side"]), 0.0)}
    return out


def rescore(rundir):
    """Score a finished run folder from its events.jsonl (last window event = final state; may lag one step if the game ended mid-step)."""
    with open(os.path.join(rundir, "summary.json"), encoding="utf-8") as f:
        summ = json.load(f)
    wins = []
    with open(os.path.join(rundir, "events.jsonl"), encoding="utf-8") as f:
        for line in f:
            e = json.loads(line)
            if e["kind"] == "window":
                wins.append(e["truth"])
    return score_match(wins[0], wins[-1], summ["winner_side"], {k: v["usd"] for k, v in summ["budgets"].items()})


if __name__ == "__main__":
    print(json.dumps(rescore(sys.argv[1]), indent=2))
