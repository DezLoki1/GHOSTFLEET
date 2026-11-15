"""Shared helpers for talking to the GhostFleet loopback bridge (stdlib only)."""
import json, math, urllib.error, urllib.request

BASE = "http://127.0.0.1:8765"
CONTACT_KEYS = {"track_id", "class", "side", "relation", "identified", "live", "age_s", "lat", "lon", "sigma_major", "sigma_minor"}
UNIT_KEYS = {"unit_id", "name", "type", "lat", "lon", "heading", "speed_kn", "weapons", "emcon", "radar_on", "sonar_on", "controllable", "waypoint", "ammo", "damage_pct", "altitude_ft", "range_left_nm", "tasks", "deck", "sub"}
STATE_KEYS = {"side", "side_name", "game_time_s", "paused", "stepping", "own_units", "contacts"}


def call(method, path, body=None, timeout=15, base=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request((base or BASE) + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def restart_mission(timeout=120):
    """Ask the game to restart the current mission (the pause menu's Restart Mission), wait until the rebuilt world answers with a stable
    unit list, pause it, and return the side-0 state. Works from a running mission or from the debrief screen."""
    import time
    code, res = call("POST", "/mission?cmd=restart")
    if code != 200:
        raise RuntimeError(f"restart refused: HTTP {code} {res}")
    t0, last_n = time.time(), None
    time.sleep(2)
    while time.time() - t0 < timeout:
        try:
            c, s = call("GET", "/state?side=0", timeout=5)
        except Exception:
            c, s = 0, None
        if c == 200 and s["own_units"]:
            n = len(s["own_units"])
            if n == last_n:            # the unit list fills in over a few frames; two equal readings in a row = settled
                call("POST", "/mission?cmd=play")      # the restarted mission waits behind the briefing screen until Play is pressed
                call("POST", "/time?cmd=pause")
                return s
            last_n = n
        else:
            last_n = None
        time.sleep(1)
    raise RuntimeError("mission did not come back after restart")


def nm(lat1, lon1, lat2, lon2):
    """Flat-earth distance in nautical miles (fine for the ranges involved)."""
    dlat = (lat2 - lat1) * 60.0
    dlon = (lon2 - lon1) * 60.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dlat, dlon)


def bearing(lat1, lon1, lat2, lon2):
    dlat = lat2 - lat1
    dlon = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    return (math.degrees(math.atan2(dlon, dlat)) + 360.0) % 360.0


def check_state(st):
    """Fail loudly if a side view carries anything beyond the whitelist (oracle-leak guard)."""
    assert set(st) <= STATE_KEYS, f"unexpected state keys {set(st) - STATE_KEYS}"
    for c in st["contacts"]:
        assert set(c) <= CONTACT_KEYS, f"unexpected contact keys {set(c) - CONTACT_KEYS}"
    for u in st["own_units"]:
        assert set(u) <= UNIT_KEYS, f"unexpected unit keys {set(u) - UNIT_KEYS}"
