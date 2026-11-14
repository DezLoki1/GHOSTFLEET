"""Shared helpers for talking to the GhostFleet loopback bridge (stdlib only)."""
import json, math, urllib.error, urllib.request

BASE = "http://127.0.0.1:8765"
CONTACT_KEYS = {"track_id", "class", "side", "relation", "identified", "live", "age_s", "lat", "lon", "sigma_major", "sigma_minor"}
UNIT_KEYS = {"unit_id", "name", "type", "lat", "lon", "heading", "speed_kn", "weapons", "emcon", "radar_on", "sonar_on", "controllable", "waypoint", "ammo", "damage_pct", "altitude_ft", "range_left_nm", "tasks", "deck"}
STATE_KEYS = {"side", "side_name", "game_time_s", "paused", "stepping", "own_units", "contacts"}


def call(method, path, body=None, timeout=15, base=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request((base or BASE) + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


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
