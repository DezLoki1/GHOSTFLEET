"""Offline tests: commander parsing/validation/budget, prompt side-scoping, and a full match against the mock bridge.
Run: python test_match.py"""
import glob, json, os, shutil, sys, unittest

import bridge_client, commander as C, match, mock_bridge
from commander import Budget, LLMCommander, extract_json, validate_orders

PORT = 18765


class FakeProvider:
    def __init__(self, replies): self.replies, self.i, self.prompts = replies, 0, []

    def complete(self, system, user):
        self.prompts.append(user)
        r = self.replies[min(self.i, len(self.replies) - 1)]; self.i += 1
        if isinstance(r, Exception): raise r
        return r, {"in": 1000, "out": 200}


GOOD = '```json\n{"assessment":"go","notes":"n1","orders":[{"action":"move","unit_id":8,"lat":12.476,"lon":43.635},{"action":"weapons","unit_id":8,"status":"free"}]}\n```'


class Parsing(unittest.TestCase):
    def test_extract(self):
        self.assertEqual(extract_json('chatter {"a": {"b": "}"}} tail')["a"]["b"], "}")
        with self.assertRaises(ValueError): extract_json("no json here")
        with self.assertRaises(ValueError): extract_json('{"a": 1')

    def test_validate(self):
        good, bad = validate_orders([
            {"action": "move", "unit_id": 8, "lat": 13, "lon": 43},
            {"action": "move", "unit_id": 99, "lat": 13, "lon": 43},             # not mine
            {"action": "move", "unit_id": 8, "lat": 99, "lon": 43},              # bad coord
            {"action": "speed", "unit_id": 8, "knots": 200},                     # range
            {"action": "weapons", "unit_id": 8, "status": "nuke"},               # enum
            {"action": "emitters", "unit_id": 8, "status": "loud"},              # enum
            {"action": "emitters", "unit_id": 8, "status": "silent"},
            {"action": "engage", "unit_id": 8, "track_id": 7001, "salvo": 2, "weapon": "SM-2MR"},
            {"action": "engage", "unit_id": 8, "track_id": 7001},
            {"action": "engage", "unit_id": 8, "track_id": 0},                   # bad track
            {"action": "engage", "unit_id": 8, "track_id": 7001, "salvo": 9},    # salvo range
            {"action": "engage", "unit_id": 8},                                  # missing track
            {"action": "move", "unit_id": 8, "lat": 13, "lon": 43, "evil": 1},   # extra key
            {"action": "launch", "unit_id": 8},                                  # unknown action
            "string", {"action": "move", "unit_id": 8}], {8, 116})
        self.assertEqual([o["action"] for o in good], ["move", "emitters", "engage", "engage"])
        self.assertEqual(len(bad), 12)
        self.assertEqual(validate_orders("x", {8})[0], [])

    def test_order_cap(self):
        good, bad = validate_orders([{"action": "speed", "unit_id": 8, "knots": 10}] * (C.MAX_ORDERS + 5), {8})
        self.assertEqual(len(good), C.MAX_ORDERS); self.assertEqual(len(bad), 5)


def _state(count, age):
    return {"side": 0, "side_name": "Taskforce1", "game_time_s": 1.0, "paused": True, "stepping": False,
            "own_units": [{"unit_id": 8, "name": "Y", "type": "Vessel", "lat": 1.0, "lon": 1.0, "heading": 0, "speed_kn": 10, "weapons": "free", "emcon": False,
                           "radar_on": True, "sonar_on": False, "controllable": True, "waypoint": None,
                           "ammo": [{"id": "gun", "count": count, "kind": "Projectile", "max_range_nm": 12.0}]}],
            "contacts": [{"track_id": 7001, "class": "p6", "side": "Taskforce2", "identified": True, "live": True, "age_s": age,
                          "lat": 1.1, "lon": 1.1, "sigma_major": 0, "sigma_minor": 0}]}


class Feedback(unittest.TestCase):
    def test_dead_track_removed_and_spend_reported(self):
        prov = FakeProvider(['{"assessment":"a","notes":"n","orders":[{"action":"engage","unit_id":8,"track_id":7001}]}'])
        c = LLMCommander("s", prov, "obj", "roe", 60, Budget())
        c.decide(_state(100, 10), 0, 3)
        c.feedback([{"unit_id": 8, "action": "engage", "ok": False, "message": "track lost or target destroyed"}])
        d = c.decide(_state(90, 500), 1, 3)
        p = prov.prompts[1]
        self.assertIn("REMOVED TRACKS", p); self.assertNotIn("7001 | p6", p)          # dead track no longer offered as a target
        self.assertIn("8:gun x10", p); self.assertEqual(d.spent, "8:gun x10")            # rounds actually expended are reported back
        self.assertIn('rejected: {"action": "engage", "unit_id": 8, "track_id": 7001}', p)   # refusal names the exact order

    def test_old_live_track_labelled_stale(self):
        prov = FakeProvider([GOOD])
        LLMCommander("s", prov, "obj", "roe", 60, Budget()).decide(_state(100, 500), 0, 3)
        self.assertIn("STALE", prov.prompts[0]); self.assertNotIn("| live |", prov.prompts[0])


class Budgets(unittest.TestCase):
    def test_exhaust(self):
        b = Budget(max_calls=2); b.calls = 2; self.assertTrue(b.exhausted())
        b = Budget(max_usd=1.0, price_in=10, price_out=10); b.in_tokens = 100_000; self.assertTrue(b.exhausted())
        self.assertFalse(Budget().exhausted())


class FullMatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.world = mock_bridge.World(); cls.srv = mock_bridge.serve(cls.world, PORT)
        cls.base = f"http://127.0.0.1:{PORT}"
        bridge_client.BASE = cls.base
        cls.created = []

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        for d in cls.created: shutil.rmtree(d, ignore_errors=True)   # keep real runs/ clean

    def run_match(self, side0, windows=3, step=60):
        self.world.__init__()
        match.make_commander = lambda spec, side, sc, step, a: side0 if side == 0 else C.ScriptedCommander("s1", "intercept")
        before = set(glob.glob(os.path.join(match.HERE, "runs", "match_*")))
        sys.argv = ["match.py", "--base", self.base, "--windows", str(windows), "--step", str(step), "--side0", "scripted:x", "--side1", "scripted:intercept"]
        match.main()
        new = sorted(set(glob.glob(os.path.join(match.HERE, "runs", "match_*"))) - before)[0]
        self.created.append(new)
        with open(os.path.join(new, "events.jsonl"), encoding="utf-8") as f:
            return new, [json.loads(l) for l in f]

    def test_llm_match_and_prompt_scoping(self):
        prov = FakeProvider([GOOD, "I refuse to answer in JSON", GOOD])
        d, ev = self.run_match(LLMCommander("side0:fake", prov, "obj", "roe", 60, Budget(max_calls=3)), windows=3)
        decs = [e for e in ev if e["kind"] == "decision" and e["side"] == 0]
        self.assertEqual(len(decs), 3)
        self.assertEqual(len(decs[0]["orders"]), 2)
        self.assertTrue(decs[1]["error"]); self.assertEqual(decs[1]["orders"], [])      # garbage reply -> hold, match continues
        self.assertTrue(decs[2]["results"][0]["ok"])
        self.assertIn("YOUR NOTES FROM LAST WINDOW: n1", prov.prompts[1])                # memory fed back
        for p in prov.prompts:                                                            # side scoping: nothing about the opponent
            for leak in ("Bolshevik", "Prj.159", "SY-1", "Taskforce2"):
                self.assertNotIn(leak, p)
        self.assertTrue(os.path.exists(os.path.join(d, "summary.json")))
        self.assertEqual([e for e in ev if e["kind"] == "match_end"][0]["windows_played"], 3)

    def test_budget_exhaustion_holds(self):
        prov = FakeProvider([GOOD])
        _, ev = self.run_match(LLMCommander("side0:fake", prov, "obj", "roe", 60, Budget(max_calls=1)), windows=3)
        decs = [e for e in ev if e["kind"] == "decision" and e["side"] == 0]
        self.assertEqual(len(prov.prompts), 1)
        self.assertIn("budget exhausted", decs[1]["error"]); self.assertEqual(decs[2]["orders"], [])

    def test_victory_detected(self):
        prov = FakeProvider(['{"assessment":"dash","notes":"","orders":[{"action":"speed","unit_id":8,"knots":60},{"action":"move","unit_id":8,"lat":12.476,"lon":43.635}]}'])
        _, ev = self.run_match(LLMCommander("side0:fake", prov, "obj", "roe", 60, Budget(max_calls=50)), windows=12, step=600)   # 60 kn * 10 min = 10 nm/window; ~41 nm to the circle
        end = [e for e in ev if e["kind"] == "match_end"][0]
        self.assertEqual(end["winner_side"], 0); self.assertEqual(end["reason"], "reached marked area")
        self.assertLess(end["windows_played"], 12)

    def test_game_ends_mid_step_scored_from_last_truth(self):
        prov = FakeProvider([GOOD])
        dead = self.world.truth()
        dead["sides"][0]["units"][0]["alive"] = False                       # Yorktown sunk before the game ended the mission
        match.LAST_TRUTH["t"] = dead
        self.world.__init__(); self.world.end_on_step = True
        try:
            d, ev = self.run_match(LLMCommander("side0:fake", prov, "obj", "roe", 60, Budget(max_calls=5)), windows=3)
        finally:
            self.world.end_on_step = False; self.world.gone = False
        end = [e for e in ev if e["kind"] == "match_end"][0]
        self.assertEqual(end["winner_side"], 1); self.assertIn("key unit destroyed", end["reason"])

    def test_spend_refused_without_confirm(self):
        sys.argv = ["match.py", "--base", self.base, "--side0", "anthropic:some-model"]
        with self.assertRaises(SystemExit) as cm: match.main()
        self.assertIn("--confirm-spend", str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
