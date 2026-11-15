"""GhostFleet match runner: two commanders (LLM or scripted), one game, deterministic paused windows.

Usage examples (game running with the scenario loaded):
  python match.py --side0 scripted:patrol --side1 scripted:intercept --windows 4          # free, no model
  python match.py --side0 anthropic:<model> --side1 anthropic:<model> --confirm-spend      # spends API credit, see --max-usd
Spec forms: scripted:patrol | scripted:intercept | anthropic:<model-id> (API key from env) | claudecli:<model alias/id> (your Claude login via
the Claude Code CLI, tools disabled) | cmd:<command that reads stdin, prints JSON>
Commanders only ever receive their own side's /state. /truth is used for scoring and the spectator log only.
"""
import argparse, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor

from bridge_client import call, nm, check_state
import commander as C

HERE = os.path.dirname(os.path.abspath(__file__))


def make_commander(spec, side, sc, step, args):
    kind, _, arg = spec.partition(":")
    name = f"side{side}:{spec}"
    if kind == "scripted":
        return C.ScriptedCommander(name, arg or "patrol")
    budget = C.Budget(args.max_calls, args.max_in_tokens, args.max_out_tokens, args.max_usd, args.price_in, args.price_out)
    s = sc["sides"][str(side)]
    if kind == "anthropic":
        prov = C.AnthropicProvider(arg, max_tokens=args.max_reply_tokens)
    elif kind == "claudecli":
        model, _, effort = arg.partition("@")      # e.g. claudecli:sonnet@low
        prov = C.ClaudeCLIProvider(model, effort=effort or None)
    elif kind == "cmd":
        prov = C.CommandProvider(arg)
    else:
        sys.exit(f"unknown commander spec '{spec}'")
    return C.LLMCommander(name, prov, s["objective"], s["roe"], step, budget)


def evaluate(truth, sc):
    """(done, winner_side|None, reason) from omniscient truth. Scoring only."""
    status = truth.get("mission_status")
    if status in ("Victory", "Defeat"):   # the game's own verdict (set by whichever victory trigger fired) is authoritative and mission-independent
        p = truth["player_side"]
        opp = [s["side"] for s in truth["sides"] if s["name"] != "Neutral" and s["side"] != p]
        return True, (p if status == "Victory" else (opp[0] if opp else None)), f"game declared {status} for the player side"
    v = sc.get("victory")   # optional per-scenario backup rules (older plugin or a mission without a victory trigger)
    if not v:
        return (True, None, "game ended the mission without a victory trigger") if truth.get("mission_ended") else (False, None, "")
    sides = {s["side"]: s for s in truth["sides"]}
    ms, ra, an = v["must_survive"], v["reach_area"], v["annihilate"]
    key = [u for u in sides[ms["side"]]["units"] if ms["unit_name_contains"].lower() in u["name"].lower()]
    if truth.get("mission_ended"):
        lost = bool(key) and not any(u["alive"] for u in key)
        return True, (1 if lost else 0), "game declared mission end (key unit destroyed)" if lost else "game declared mission end (objective met)"
    if key and not any(u["alive"] for u in key):
        return True, 1, "key unit destroyed"
    for u in key:
        if u["alive"] and nm(u["lat"], u["lon"], ra["lat"], ra["lon"]) <= ra["radius_nm"]:
            return True, ra["side"], "reached marked area"
    foes = [u for u in sides[an["side"]]["units"] if u["type"] in an["types"]]
    if foes and not any(u["alive"] for u in foes):
        return True, 0, "all opposing ships and land units destroyed"
    return False, None, ""


def compact_truth(truth):
    return {"t": truth["game_time_s"], "sides": [{"side": s["side"], "name": s["name"], "kill": s["kill_score"], "loss": s["loss_score"],
            "alive": sum(u["alive"] for u in s["units"]), "total": len(s["units"]),
            "units": [[u["unit_id"], u["name"], u["type"], u["alive"], u["lat"], u["lon"], u.get("ammo_total")] for u in s["units"]]} for s in truth["sides"]]}


LAST_TRUTH = {}   # newest truth seen while a step runs: if the game ends the mission mid-step, score from this


def wait_paused(deadline_s=300):
    t0 = time.time()
    last_gt, still = None, 0
    while True:
        time.sleep(0.5)
        code, st = call("GET", "/state?side=0")
        if code != 200:
            return False
        # the game can pause itself at mission end while our step is unfinished: paused + clock frozen for 5 s = step is over
        still = still + 1 if (st["paused"] and st["game_time_s"] == last_gt) else 0
        last_gt = st["game_time_s"]
        if still >= 10:
            call("POST", "/time?cmd=pause")
            return True
        tc, tr = call("GET", "/truth")
        if tc == 200:
            LAST_TRUTH["t"] = tr
        if st["paused"] and not st["stepping"]:
            return True
        if time.time() - t0 > deadline_s:
            raise RuntimeError("step did not finish")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--side0", default="scripted:patrol")
    ap.add_argument("--side1", default="scripted:intercept")
    ap.add_argument("--scenario", default=os.path.join(HERE, "scenarios", "red_sea_breakout_1988.json"))
    ap.add_argument("--windows", type=int)
    ap.add_argument("--step", type=float)
    ap.add_argument("--max-game-s", type=float, help="stop after this many game seconds (default: the scenario's max_game_s); --windows then only caps the number of windows")
    ap.add_argument("--scale", type=float, default=4.0)
    ap.add_argument("--label", default="")
    ap.add_argument("--base", default=None, help="bridge base URL override (tests)")
    ap.add_argument("--max-calls", type=int, default=60)
    ap.add_argument("--max-in-tokens", type=int, default=1500000)
    ap.add_argument("--max-out-tokens", type=int, default=400000)   # Haiku at low effort writes ~8k tokens per call; --max-usd is the real spend cap
    ap.add_argument("--continue-when-exhausted", action="store_true", help="old behaviour: a commander whose budget ran out silently holds while the match goes on (invalidates comparisons)")
    ap.add_argument("--max-reply-tokens", type=int, default=1500)
    ap.add_argument("--max-usd", type=float, default=0.0)
    ap.add_argument("--price-in", type=float, default=0.0, help="USD per million input tokens (you supply)")
    ap.add_argument("--price-out", type=float, default=0.0, help="USD per million output tokens (you supply)")
    ap.add_argument("--confirm-spend", action="store_true", help="required for anthropic:/cmd: commanders")
    ap.add_argument("--restart", action="store_true", help="restart the loaded mission through the game before the match (no clicking needed)")
    ap.add_argument("--dry-prompt", action="store_true", help="print each side's first prompt and exit (no model calls, nothing ordered)")
    a = ap.parse_args()

    if a.base:
        import bridge_client; bridge_client.BASE = a.base
    with open(a.scenario, encoding="utf-8") as f:
        sc = json.load(f)
    step = a.step or sc["step_s"]
    max_game = a.max_game_s or sc.get("max_game_s")      # the mission budget is GAME time: commanders may ask for short windows, which must not eat the budget
    windows = a.windows or (sc.get("max_windows", 200) if max_game else sc["windows"])
    specs = {0: a.side0, 1: a.side1}
    if any(not s.startswith("scripted") for s in specs.values()) and not (a.confirm_spend or a.dry_prompt):
        sys.exit("refusing to start: model commanders can spend money. Re-run with --confirm-spend (and consider --max-usd/--price-in/--price-out).")
    cmdrs = {s: make_commander(specs[s], s, sc, step, a) for s in (0, 1)}
    for c in cmdrs.values():
        c.limit_s = max_game      # lets an LLM commander see "X of Y minutes used"

    if a.dry_prompt:
        for s in (0, 1):
            code, st = call("GET", f"/state?side={s}"); assert code == 200, st
            check_state(st)
            c = cmdrs[s]
            print(f"===== SIDE {s} SYSTEM PROMPT =====\n{getattr(c, 'system', '(scripted: no prompt)')}\n===== SIDE {s} FIRST USER PROMPT =====")
            print(c.user_prompt(st, 0, windows) if hasattr(c, "user_prompt") else "(scripted)")
        return 0

    base_dir = os.path.join(HERE, "runs", time.strftime("match_%Y%m%d_%H%M%S") + (f"_{a.label}" if a.label else ""))
    rundir, n = base_dir, 1
    while True:   # two matches can start within the same second
        try: os.makedirs(rundir); break
        except FileExistsError: n += 1; rundir = f"{base_dir}_{n}"
    log = open(os.path.join(rundir, "events.jsonl"), "a", encoding="utf-8")

    def emit(kind, **kw):
        log.write(json.dumps({"kind": kind, "wall": round(time.time(), 2), **kw}) + "\n"); log.flush()

    if a.restart:
        import bridge_client
        bridge_client.restart_mission()
    assert call("POST", "/time?cmd=pause")[0] == 200
    assert call("POST", f"/time?cmd=scale&value={a.scale}")[0] == 200
    call("POST", "/time?cmd=pause")
    emit("match_start", scenario=sc["name"], specs=specs, windows=windows, step=step, scale=a.scale, label=a.label,
         budgets={s: vars(cmdrs[s].budget) for s in (0, 1)})
    winner, reason, done_w = None, "window limit reached", windows
    t_start = None

    for w in range(windows):
        states = {}
        for s in (0, 1):
            code, st = call("GET", f"/state?side={s}")
            if code != 200:
                winner, reason, done_w = None, f"game ended or unavailable (HTTP {code})", w
                break
            check_state(st)
            states[s] = st
        else:
            code, truth = call("GET", "/truth")
            assert code == 200, truth
            emit("window", w=w, truth=compact_truth(truth))
            done, win, why = evaluate(truth, sc)
            if done:
                winner, reason, done_w = win, why, w
                break
            t_start = truth["game_time_s"] if t_start is None else t_start
            if max_game and truth["game_time_s"] - t_start >= max_game:
                winner, reason, done_w = None, "game-time limit reached", w
                break
            with ThreadPoolExecutor(2) as ex:   # both decide on the same frozen state
                futs = {s: ex.submit(cmdrs[s].decide, states[s], w, windows) for s in (0, 1)}
                decs = {s: f.result() for s, f in futs.items()}
            for s in (0, 1):
                d = decs[s]
                results = []
                if d.orders:
                    code, res = call("POST", f"/orders?side={s}", {"orders": d.orders})
                    results = res.get("results", []) if code == 200 else [{"unit_id": o["unit_id"], "action": o["action"], "ok": False, "message": f"HTTP {code} {res}"} for o in d.orders]
                cmdrs[s].feedback(results)
                emit("decision", w=w, side=s, assessment=d.assessment, notes=d.notes, orders=d.orders, rejected=[[o, r] for o, r in d.rejected],
                     results=results, receipts=[{"order_id": f"w{w + 1}-s{s}-o{i + 1}", "order": o, "ok": r["ok"], "message": r["message"]} for i, (o, r) in enumerate(zip(d.orders, results))],
                     spent_since_last_window=getattr(d, "spent", ""), usage=d.usage, error=d.error, budget=cmdrs[s].budget.snapshot(), prompt=d.raw, reply=d.reply)
                print(f"w{w + 1}/{windows} side{s}: {len(d.orders)} order(s), {len(d.rejected)} invalid, {sum(1 for r in results if not r['ok'])} refused" + (f"  ERROR {d.error}" if d.error else ""))
            out = [s for s in (0, 1) if getattr(cmdrs[s], "provider", None) is not None and cmdrs[s].budget.exhausted()]
            if out and not a.continue_when_exhausted:   # an idle commander makes the result meaningless: say so instead of playing on
                winner, reason, done_w = None, f"commander budget exhausted (side {out[0]}); stopped because the match would no longer be a fair contest", w + 1
                break
            # each side may ask for a shorter or longer run before its next decision; the shortest wish wins (default = the nominal step)
            run_s = min([decs[s].next_check_s or step for s in (0, 1)])
            emit("step", w=w, seconds=run_s, requested={s: decs[s].next_check_s for s in (0, 1)})
            assert call("POST", f"/time?cmd=step&seconds={run_s}")[0] == 200
            if not wait_paused():
                winner, reason, done_w = None, "game ended during step", w + 1
                break
            continue
        break   # inner for/else broke out (state unavailable)
    else:
        code, truth = call("GET", "/truth")
        if code == 200:
            emit("window", w=windows, truth=compact_truth(truth))
            done, win, why = evaluate(truth, sc)
            if done:
                winner, reason = win, why

    if winner is None and reason.startswith("game ended") and "t" in LAST_TRUTH:   # mission ended by the game itself
        done, win, why = evaluate(LAST_TRUTH["t"], sc)
        if done:
            winner, reason = win, why + " (game ended mission; scored from last truth)"
    summary = {"scenario": sc["name"], "specs": specs, "label": a.label, "windows_played": done_w, "winner_side": winner, "reason": reason,
               "budgets": {s: cmdrs[s].budget.snapshot() for s in (0, 1)}}
    emit("match_end", **summary)
    with open(os.path.join(rundir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    log.close()
    import report
    try:
        rep = report.write_report(rundir)
    except Exception as e:   # a report problem must never lose the match result
        rep = f"(report failed: {e})"
    print("RESULT:", json.dumps(summary), "\nlog dir:", rundir, "\nreport:", rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
