"""Offline stand-in for the plugin's HTTP bridge (same routes/keys, toy physics). Used only by tests."""
import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from bridge_client import nm, bearing
import math


class Unit:
    def __init__(self, uid, name, typ, lat, lon, kn=15.0):
        self.uid, self.name, self.type, self.lat, self.lon, self.kn = uid, name, typ, lat, lon, kn
        self.wp, self.alive, self.weapons, self.emcon = None, True, "tight", False


class World:
    def __init__(self):
        self.t, self.paused = 0.0, True
        self.units = {0: [Unit(8, "Yorktown CG-48", "Vessel", 12.94, 43.11, 12.8), Unit(116, "E-2C", "Aircraft", 12.84, 42.44, 220)],
                      1: [Unit(19, "Prj.183 Bolshevik", "Vessel", 12.87, 43.23), Unit(30, "Prj.159-A", "Vessel", 12.75, 43.13),
                          Unit(2, "SY-1 AShM Site", "LandUnit", 12.73, 43.47, 0)]}
        self.orders_log = []
        self.gone = False   # tests: the game ended the mission, bridge answers 503

    def step(self, seconds):
        for side in self.units.values():
            for u in side:
                if u.wp and u.alive and u.type != "LandUnit":
                    d = nm(u.lat, u.lon, *u.wp)
                    move = min(d, u.kn * seconds / 3600.0)
                    if d > 1e-6:
                        f = move / d
                        u.lat += (u.wp[0] - u.lat) * f; u.lon += (u.wp[1] - u.lon) * f
        self.t += seconds

    def state(self, side):
        other = self.units[1 - side]
        own = [{"unit_id": u.uid, "name": u.name, "type": u.type, "lat": u.lat, "lon": u.lon, "heading": 0.0, "speed_kn": u.kn,
                "weapons": u.weapons, "emcon": u.emcon, "radar_on": not u.emcon, "sonar_on": False, "controllable": True,
                "ammo": [{"id": "SM-2MR", "count": 8, "kind": "Missile", "max_range_nm": 60.0}] if u.type == "Vessel" else [],
                "waypoint": {"lat": u.wp[0], "lon": u.wp[1]} if u.wp else None} for u in self.units[side] if u.alive]
        mine = [u for u in self.units[side] if u.alive]
        contacts = []
        for i, o in enumerate(other):
            if o.alive and any(nm(m.lat, m.lon, o.lat, o.lon) < 25 for m in mine):
                contacts.append({"track_id": 7001 + i, "class": None, "side": None, "identified": False, "live": True, "age_s": 1.0,
                                 "lat": o.lat, "lon": o.lon, "sigma_major": 0, "sigma_minor": 0})
        return {"side": side, "side_name": f"Taskforce{side + 1}", "game_time_s": self.t, "paused": self.paused, "stepping": False,
                "own_units": own, "contacts": contacts}

    def truth(self):
        return {"game_time_s": self.t, "sides": [{"side": s, "name": f"Taskforce{s + 1}", "kill_score": 0, "loss_score": 0,
                "units": [{"unit_id": u.uid, "name": u.name, "type": u.type, "alive": u.alive, "lat": u.lat, "lon": u.lon} for u in self.units[s]]} for s in (0, 1)]}

    def apply(self, side, orders):
        res = []
        for o in orders:
            u = next((x for x in self.units[side] if x.uid == o.get("unit_id")), None)
            if u is None:
                res.append({"unit_id": o.get("unit_id"), "action": o.get("action"), "ok": False, "message": "unit not owned by this side"}); continue
            a = o["action"]
            if a == "move": u.wp = (o["lat"], o["lon"]); u.kn = max(u.kn, 15.0)
            elif a == "speed": u.kn = o["knots"]
            elif a == "emitters": u.emcon = o["status"] == "silent"
            elif a == "weapons": u.weapons = o["status"]
            elif a == "engage":
                if o.get("track_id") not in {c["track_id"] for c in self.state(side)["contacts"]}:
                    res.append({"unit_id": u.uid, "action": a, "ok": False, "message": "no such track in your picture"}); continue
            else:
                res.append({"unit_id": u.uid, "action": a, "ok": False, "message": "unknown action"}); continue
            self.orders_log.append((side, o))
            res.append({"unit_id": u.uid, "action": a, "ok": True, "message": "ok"})
        return {"results": res}


def serve(world, port):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _send(self, code, obj):
            b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

        def do_GET(self):
            u = urlparse(self.path); q = parse_qs(u.query)
            if world.gone: self._send(503, {"error": "no mission"})
            elif u.path == "/state": self._send(200, world.state(int(q["side"][0])))
            elif u.path == "/truth": self._send(200, world.truth())
            else: self._send(404, {"error": "unknown route"})

        def do_POST(self):
            u = urlparse(self.path); q = parse_qs(u.query)
            n = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(n).decode() if n else ""
            if u.path == "/orders":
                try: orders = json.loads(body)["orders"]
                except Exception: return self._send(400, {"error": "body must be exactly {\"orders\":[...]}"})
                self._send(200, world.apply(int(q["side"][0]), orders))
            elif u.path == "/mission" and q["cmd"][0] == "play":
                world.plays = getattr(world, "plays", 0) + 1
                self._send(200, {"ok": True, "playing": True})
            elif u.path == "/mission":
                world.__init__(); world.restarts = getattr(world, "restarts", 0) + 1
                self._send(200, {"ok": True, "restarting": True})
            elif u.path == "/time":
                cmd = q["cmd"][0]
                if cmd == "pause": world.paused = True
                elif cmd == "step":
                    world.step(float(q["seconds"][0])); world.paused = True
                    if getattr(world, "end_on_step", False): world.gone = True
                self._send(200, {"ok": True, "game_time_s": world.t, "paused": world.paused})
            else: self._send(404, {"error": "unknown route"})

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
