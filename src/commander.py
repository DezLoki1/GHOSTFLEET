"""Commanders for GhostFleet: an LLM (or scripted) decision maker for ONE side.

A commander only ever receives that side's /state dict (own units + own-table tracks), its own previous orders/results and
its own scratch notes. It never sees /truth or the other commander's anything. Stdlib only.
"""
import json, os, re, shlex, subprocess, time, urllib.error, urllib.request
from dataclasses import dataclass, field

from bridge_client import nm

MAX_ORDERS = 12
ACTIONS = {
    "move": {"unit_id", "lat", "lon"},
    "speed": {"unit_id", "knots"},
    "emitters": {"unit_id", "status"},
    "weapons": {"unit_id", "status"},
    "engage": {"unit_id", "track_id"},
    "launch": {"unit_id", "aircraft", "loadout", "mission"},
    "rtb": {"unit_id"},
    "depth": {"unit_id", "status"},
    "sonar": {"unit_id", "status"},
    "decoy": {"unit_id"},
}
DEPTHS = ("surface", "periscope", "shallow", "above_layer", "below_layer", "deep", "very_deep")
OPTIONAL = {"engage": {"salvo", "weapon"}, "launch": {"count"}}
MISSIONS = {"CAP", "Intercept", "BombStrike", "MissileAttack", "SEAD", "Jamming", "AEW", "Recon", "MPA", "SearchAndDestroyASW", "SAR"}

SYSTEM_PROMPT = """You are the fleet commander for ONE side in a real-time naval simulation (Sea Power, 1980s missile age).

What you know: only your own units and the tracks your own sensors report. Track positions are estimates and can be stale or wrong
(see age_s, live, sigma); class and side may be null (unknown). You cannot see anything else. Do not invent contacts.
The simulation is PAUSED while you decide. Each decision window then runs {step} game-seconds with your orders in effect.
Your units' own AI handles piloting, sensors and self-defence; you give HIGH-LEVEL orders only.

Objective: {objective}
Rules of engagement: {roe}

Order types (JSON objects, unit_id must be one of YOUR units):
  {{"action":"move","unit_id":N,"lat":DEG,"lon":DEG}}      go to a position (replaces the unit's current waypoints)
  {{"action":"speed","unit_id":N,"knots":0-60}}            ships and submarines only
  {{"action":"emitters","unit_id":N,"status":"silent|active"}}   silent = search radars, active sonar and jammers OFF (harder to detect, but that unit sees less); active = search radars ON. Only units with a search radar are affected: if a unit's radar_on stays false after "active" it has none (e.g. missile launchers, guns, observation posts) - do not repeat the order
  {{"action":"weapons","unit_id":N,"status":"hold|tight|free"}}   hold = no fire, tight = fire only at positively hostile targets, free = fire at anything not known friendly/neutral
  {{"action":"engage","unit_id":N,"track_id":T,"salvo":1-4,"weapon":"<id from YOUR WEAPONS>"}}   order this unit to fire at one of your tracks NOW (salvo and weapon optional; without weapon the unit picks the best one itself). The track must be identified as hostile and your own plotting table must hold it; the result tells you whether a shot was actually queued (it is refused if the target is out of range, you have no suitable ammo, or the track is not an identified hostile). Missiles take time to arrive, so judge range using YOUR WEAPONS and the track positions.
  {{"action":"launch","unit_id":CARRIER,"aircraft":"<id>","loadout":"<name>","mission":"CAP|Intercept|BombStrike|MissileAttack|SEAD|Jamming|AEW|Recon|MPA|SearchAndDestroyASW|SAR","count":1-4}}   queue aircraft to launch from a carrier or ship with a flight deck. aircraft, loadout and mission are ALL required. Use only the aircraft and loadouts listed under YOUR AIR WING. Launching takes minutes; the aircraft then appear in YOUR UNITS and you give them move/engage orders. SEAD aircraft carry anti-radiation missiles for suppressing SAM and radar sites; check each loadout before you commit it.
  {{"action":"rtb","unit_id":N}}   an aircraft returns to base and lands (use when fuel is low or its weapons are spent)
  {{"action":"depth","unit_id":N,"status":"surface|periscope|shallow|above_layer|below_layer|deep|very_deep"}}   submarines only: dive or rise to a preset depth (feet per preset are in YOUR UNITS); the boat changes depth gradually
  {{"action":"sonar","unit_id":N,"status":"active|passive"}}   active = ping (finds targets at range, but everyone listening learns where you are); passive = listen only (the default, quiet)
  {{"action":"decoy","unit_id":N}}   submarines only: launch a noisemaker decoy (limited stock) when a torpedo is homing on you
Weapon economy: your stocks are finite and cannot be replenished. Use the cheapest weapon that does the job: guns against small boats inside gun range, anti-ship missiles only against larger ships you cannot reach with guns, SAMs for aircraft and incoming missiles. Check YOUR WEAPONS (range and count) before ordering, and the "ROUNDS YOU EXPENDED" line to see what you actually fired.
Standing postures: your units start with weapon and emitter postures chosen by the scenario designers. Keep them unless you have a specific reason; blanket-setting every unit to tight or hold can silence your own defenses.
Aircraft are fast but fragile and carry few weapons. Keep them outside the range of known SAM sites and fighters except to launch, and withdraw afterwards. engage with no weapon only designates the target and the aircraft AI may fly a close-in attack run. engage WITH a named weapon fires it once the target is inside that weapon's range, so position the aircraft just inside that range (see YOUR WEAPONS) and then move it away. Watch each aircraft's fuel range.
A carrier's air wing is not in YOUR UNITS until you launch it: check YOUR AIR WING for fighters, strike, SEAD (anti-radar) and electronic-warfare aircraft, and launch what the mission needs instead of fighting only with the aircraft already airborne. An anti-radiation missile may be refused against a radar that is not currently emitting; the refusal message lists what is usable. Ships can also queue gun and missile shots on land targets within range.
Submarines: your boat's own AI handles steering, contact analysis and torpedo evasion; you choose where it goes, how deep, how fast, whether to ping, and when to shoot. A submarine is blind and deaf beyond a few miles unless it listens well: going fast is noisy: it makes you easy to hear and makes you hear less; going slow is the reverse. Depth relative to the thermal layer (above_layer / below_layer) changes who can hear whom, and deep water may be better for survival and shallow water for reaching a target. Tracks you hold may be only a bearing or a rough area, not a position to shoot at.
Distances are large and ships are slow (10-30 kn). Each unit's status shows an ETA to its waypoint: plan with it, use speed orders when time matters, and use next_check_s to skip quiet periods.
Note: "weapons free" only lets a unit's own AI shoot when it decides to; it does not guarantee shots. Use engage to make sure the shots you want are fired, and to choose the target and weapon.
At most {max_orders} orders per window. Only order what you want to change; units keep their current orders otherwise.

Track "relation" is the game's own assessment for your side: HOSTILE tracks are enemies, FRIENDLY are yours, NEUTRAL are civilians/third parties, UNKNOWN is not yet classified.

Reply with ONE JSON object and nothing else:
{{"assessment":"<=300 chars: your read of the situation","notes":"<=600 chars: memory for yourself next window","orders":[ ... ],"next_check_s":<optional, 30-600>}}
next_check_s = how many game seconds to run before you decide again (default {step}). Ask for longer during a quiet transit and shorter when contact is close; the shorter of both sides' requests is used.
"""


@dataclass
class Budget:
    max_calls: int = 60
    max_in_tokens: int = 400_000
    max_out_tokens: int = 60_000
    max_usd: float = 0.0           # 0 = not enforced (needs price_in/price_out per million tokens)
    price_in: float = 0.0
    price_out: float = 0.0
    calls: int = 0
    in_tokens: int = 0
    out_tokens: int = 0
    reported_usd: float = 0.0      # cost reported by the provider itself (CLI), added to any token-price estimate

    @property
    def usd(self):
        return self.in_tokens / 1e6 * self.price_in + self.out_tokens / 1e6 * self.price_out + self.reported_usd

    def exhausted(self):
        if self.calls >= self.max_calls or self.in_tokens >= self.max_in_tokens or self.out_tokens >= self.max_out_tokens:
            return True
        return bool(self.max_usd) and self.usd >= self.max_usd

    def snapshot(self):
        return {"calls": self.calls, "in_tokens": self.in_tokens, "out_tokens": self.out_tokens, "usd": round(self.usd, 4)}


# ---------------------------------------------------------------- providers
class AnthropicProvider:
    """Anthropic Messages API. Key is read from the environment only (never logged)."""
    def __init__(self, model, max_tokens=1500, key_env="ANTHROPIC_API_KEY", timeout=120):
        self.model, self.max_tokens, self.key_env, self.timeout = model, max_tokens, key_env, timeout

    def complete(self, system, user):
        key = os.environ.get(self.key_env)
        if not key:
            raise RuntimeError(f"environment variable {self.key_env} is not set")
        body = json.dumps({"model": self.model, "max_tokens": self.max_tokens, "system": system,
                           "messages": [{"role": "user", "content": user}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST",
                                     headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        last = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    d = json.loads(r.read().decode())
                text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
                u = d.get("usage", {})
                return text, {"in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0)}
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}: {e.read().decode()[:300]}"
                if e.code not in (429, 500, 502, 503, 529):
                    break
            except Exception as e:  # network/timeouts
                last = repr(e)
            time.sleep(2 ** attempt)
        raise RuntimeError("anthropic call failed: " + str(last))


class ClaudeCLIProvider:
    """Runs the Claude Code CLI in print mode, so it uses the user's own Claude login exactly as Claude Code does (no token is read or
    forwarded by this code). Locked down to a plain text commander: no tools, no settings/hooks/MCP/skills, no session saved, empty cwd.
    Usage counts against the user's Claude plan limits; check Anthropic's terms for automated use before long runs."""
    def __init__(self, model, timeout=300, exe="claude", effort=None):
        self.model, self.timeout, self.exe, self.effort = model, timeout, exe, effort
        import tempfile
        self.cwd = tempfile.mkdtemp(prefix="ghostfleet_cli_")

    def complete(self, system, user):
        argv = [self.exe, "-p", "--output-format", "json", "--tools", "", "--restricted", "--strict-mcp-config", "--disable-slash-commands",
                "--no-session-persistence", "--system-prompt", system]
        if self.model and self.model != "default":
            argv += ["--model", self.model]
        if self.effort:
            argv += ["--effort", self.effort]
        p = subprocess.run(argv, input=user, capture_output=True, text=True, encoding="utf-8", timeout=self.timeout, cwd=self.cwd)
        if p.returncode != 0:
            raise RuntimeError(f"claude exited {p.returncode}: {(p.stderr or p.stdout)[:300]}")
        d = json.loads(p.stdout)
        if d.get("is_error"):
            raise RuntimeError("claude error: " + str(d.get("result"))[:300])
        u = d.get("usage", {})
        tin = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
        return d.get("result", ""), {"in": tin, "out": u.get("output_tokens", 0), "usd": d.get("total_cost_usd", 0.0)}


class OpenRouterProvider:
    """OpenRouter (OpenAI-compatible chat completions), any model slug e.g. openrouter:google/gemini-2.5-pro. Key from env OPENROUTER_API_KEY only
    (never logged, never in a run folder). Cost is the provider-reported usage.cost."""
    def __init__(self, model, max_tokens=1500, key_env="OPENROUTER_API_KEY", timeout=180):
        self.model, self.max_tokens, self.key_env, self.timeout = model, max_tokens, key_env, timeout

    def complete(self, system, user):
        key = os.environ.get(self.key_env)
        if not key:
            raise RuntimeError(f"environment variable {self.key_env} is not set")
        body = json.dumps({"model": self.model, "max_tokens": self.max_tokens, "usage": {"include": True},
                           "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode()
        req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=body, method="POST",
                                     headers={"Authorization": "Bearer " + key, "content-type": "application/json"})
        last = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    d = json.loads(r.read().decode())
                if d.get("error"):
                    raise RuntimeError(str(d["error"])[:300])
                u = d.get("usage") or {}
                return d["choices"][0]["message"].get("content") or "", {"in": u.get("prompt_tokens", 0), "out": u.get("completion_tokens", 0), "usd": u.get("cost", 0.0) or 0.0}
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}: {e.read().decode()[:300]}"
                if e.code not in (429, 500, 502, 503, 529):
                    break
            except Exception as e:
                last = repr(e)
            time.sleep(2 ** attempt)
        raise RuntimeError("openrouter call failed: " + str(last))


class CommandProvider:
    """Runs any CLI that reads the prompt on stdin and prints the reply on stdout (e.g. an agent CLI in print mode).
    Token use is only estimated (chars/4). Check that provider's terms before automating it."""
    def __init__(self, command, timeout=300):
        self.argv, self.timeout = shlex.split(command), timeout

    def complete(self, system, user):
        p = subprocess.run(self.argv, input=system + "\n\n" + user, capture_output=True, text=True, timeout=self.timeout)
        if p.returncode != 0:
            raise RuntimeError(f"command exited {p.returncode}: {p.stderr[:300]}")
        return p.stdout, {"in": (len(system) + len(user)) // 4, "out": len(p.stdout) // 4}


# ---------------------------------------------------------------- prompt
def _fmt_ll(o):
    return f"{o['lat']:.3f},{o['lon']:.3f}" if o.get("lat") is not None else "-"


def _quality(c):
    # shortcut: sigma units are unverified (radar tracks ~0.01, an ESM-only track ~28800), so expose only a coarse label
    # (>1000 assumed metres); replace once the units are confirmed in-game.
    s = c.get("sigma_major")
    if s is None:
        return "no position"
    return f"coarse (~{s / 1000:.0f} km uncertainty)" if s > 1000 else "good"


STALE_AFTER_S = 180


def ammo_map(st):
    return {(u["unit_id"], a["id"]): a["count"] for u in st["own_units"] for a in u.get("ammo", [])}


def unit_status(u):
    """Compact extras for a unit row: ETA to waypoint, damage, aircraft altitude/fuel range, pending shots."""
    bits = []
    wp = u.get("waypoint")
    if wp and u.get("speed_kn", 0) > 1 and u.get("lat") is not None:
        bits.append(f"eta {nm(u['lat'], u['lon'], wp['lat'], wp['lon']) / u['speed_kn'] * 60:.0f} min")
    if u.get("damage_pct", 0) >= 1:
        bits.append(f"DAMAGED {u['damage_pct']:.0f}%")
    if u.get("altitude_ft") is not None:
        bits.append(f"alt {u['altitude_ft']:.0f} ft")
    if u.get("range_left_nm") is not None:
        bits.append(f"fuel range {u['range_left_nm']:.0f} nm")
    sub = u.get("sub")
    if sub:
        # the plugin's "setting" is only meaningful after a depth order (a mission start does not set it), so it is not shown; battery is 0 on nuclear boats
        bits.append(f"depth {sub['depth_ft']:.0f} ft" + (f"; battery {sub['battery_pct']:.0f}%" if sub.get("battery_pct", 0) > 0 else "") + ("; SNORKEL MAST OUT" if sub.get("snorkel") else "")
                    + "; sonar " + ("ACTIVE (pinging)" if u.get("sonar_on") else "passive"))
    t = u.get("tasks") or {}
    for e in t.get("engage", []):
        bits.append(f"PENDING SHOT {e['weapon']}->track {e['track_id'] if e['track_id'] else '?'}")
    if t.get("designated_track"):
        bits.append(f"attack designated on track {t['designated_track']}")
    return "; ".join(bits)


def render_state(st, prev_tracks, last_orders, last_results, notes, window, windows, dead=frozenset(), spent="", events="", clock="", invalid=()):
    L = [f"WINDOW {window + 1}  game_time={st['game_time_s']:.0f}s  you={st['side_name']}" + (f"  {clock}" if clock else f"  (of at most {windows} windows)")]
    L.append("YOUR UNITS (id | name | type | lat,lon | hdg | kn | weapons | emcon | waypoint | status):")
    for u in st["own_units"]:
        wp = f"{u['waypoint']['lat']:.3f},{u['waypoint']['lon']:.3f}" if u.get("waypoint") else "none"
        L.append(f"  {u['unit_id']} | {u['name']} | {u['type']} | {_fmt_ll(u)} | {u['heading']:.0f} | {u['speed_kn']:.0f} | {u['weapons']} | {'SILENT' if u['emcon'] else 'emitting'} | {wp} | {unit_status(u) or 'ok'}")
    for u in st["own_units"]:
        if u.get("sub"):
            L.append(f"DEPTH PRESETS for {u['name']} (unit {u['unit_id']}), feet: " + ", ".join(f"{k} {v}" for k, v in u["sub"]["presets_ft"].items()))
    if events:
        L.append("EVENTS SINCE LAST WINDOW: " + events)
    shootable = lambda u: [a for a in u.get("ammo", []) if a["kind"] not in ("Chaff", "Noisemaker", "Decoy", "Flare") and "_tank_" not in a["id"]]   # decoys and drop tanks are not engage weapons
    armed = [u for u in st["own_units"] if shootable(u)]
    if armed:
        L.append("YOUR WEAPONS (unit: weapon id x count (kind, max range nm, what it can hit: air/surface/sub/land)):")
        for u in armed:
            L.append(f"  {u['unit_id']}: " + "; ".join(f"{a['id']} x{a['count']} ({a['kind']}, {a['max_range_nm']:.0f} nm, hits {a.get('targets', '?')})" for a in shootable(u)))
    for u in st["own_units"]:
        deck = u.get("deck")
        if deck and deck.get("aircraft"):
            L.append(f"YOUR AIR WING on {u['name']} (unit {u['unit_id']}){' - launch in progress' if deck.get('launching') else ''}: aircraft id / loadout xavailable")
            for ac in deck["aircraft"]:
                L.append(f"  {ac['aircraft']} ({ac['name']}): " + "; ".join(f"{l['loadout']} x{l['available']}" for l in ac["loadouts"]))
    L.append("YOUR TRACKS (track | class | side | relation to you | identified | live/stale | age_s | est. lat,lon | position quality):")
    shown = [c for c in st["contacts"] if c["track_id"] not in dead]
    if not shown:
        L.append("  (none)")
    for c in shown:
        fresh = c["age_s"] <= STALE_AFTER_S   # age alone decides: the game keeps a destroyed object's track flagged live while its age grows, and sonar tracks are never flagged live
        L.append(f"  {c['track_id']} | {c['class'] or '?'} | {c['side'] or '?'} | {str(c.get('relation', 'unknown')).upper()} | {'ID' if c['identified'] else 'unid'} | {'live' if fresh else 'STALE'} | {c['age_s']:.0f} | {_fmt_ll(c)} | {_quality(c)}")
    if dead:
        L.append(f"REMOVED TRACKS (the game refused your engage because they are destroyed or lost; do not target them or plan around them): {sorted(dead)}")
    now = {c["track_id"] for c in st["contacts"]}
    if prev_tracks is not None:
        new, lost = sorted(now - prev_tracks), sorted(prev_tracks - now)
        L.append(f"CHANGES: new tracks {new or 'none'}; lost tracks {lost or 'none'}")
    if last_orders:
        pairs = list(zip(last_orders, last_results or []))
        bad = [(o, r) for o, r in pairs if not r.get("ok")]
        L.append(f"YOUR LAST ORDERS: {len(last_orders)} sent, {len(bad)} rejected"
                 + "".join(f"\n  rejected: {json.dumps(o)} -> {r['message']}" for o, r in bad))
    if invalid:   # orders that never reached the game: without this the model cannot know they were dropped
        L.append(f"ORDERS NOT SENT (malformed, fix and resend if still wanted): {len(invalid)}"
                 + "".join(f"\n  {json.dumps(o)} -> {r}" for o, r in invalid))
    if spent:
        L.append("ROUNDS EXPENDED SINCE LAST WINDOW (unit:weapon x count): " + spent)
    if notes:
        L.append("YOUR NOTES FROM LAST WINDOW: " + notes)
    L.append("Decide now. Reply with the single JSON object only.")
    return "\n".join(L)


# ---------------------------------------------------------------- output parsing / validation
def extract_json(text):
    """First balanced {...} object in text (tolerates code fences / chatter). Raises ValueError."""
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    start = s.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            esc = (ch == "\\") and not esc
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start:i + 1])
    raise ValueError("unterminated JSON object")


def validate_orders(orders, own_ids):
    """Returns (good, rejected[(order, reason)]). Never trusts model output; the plugin re-validates ownership/legality."""
    good, bad = [], []
    if not isinstance(orders, list):
        return [], [(orders, "orders must be a list")]
    for o in orders[:MAX_ORDERS * 4]:
        if len(good) >= MAX_ORDERS:
            bad.append((o, f"over {MAX_ORDERS} orders"))
            continue
        try:
            if not isinstance(o, dict) or o.get("action") not in ACTIONS:
                raise ValueError("unknown action")
            allowed = ACTIONS[o["action"]] | OPTIONAL.get(o["action"], set())
            if set(o) - allowed - {"action"}:
                raise ValueError("unexpected keys " + str(sorted(set(o) - allowed - {"action"})))
            missing = sorted(ACTIONS[o["action"]] - set(o))
            if missing:
                raise ValueError("missing fields: " + ", ".join(missing))
            if isinstance(o["unit_id"], bool) or not isinstance(o["unit_id"], int) or o["unit_id"] not in own_ids:
                raise ValueError("unit_id is not one of your units")
            a = o["action"]
            num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
            if a == "move" and not (num(o["lat"]) and num(o["lon"]) and abs(o["lat"]) <= 85 and abs(o["lon"]) <= 180):
                raise ValueError("bad coordinates")
            if a == "speed" and not (num(o["knots"]) and 0 <= o["knots"] <= 60):
                raise ValueError("knots out of range")
            if a == "emitters" and o["status"] not in ("silent", "active"):
                raise ValueError("status must be silent|active")
            if a == "weapons" and o["status"] not in ("hold", "tight", "free"):
                raise ValueError("status must be hold|tight|free")
            if a == "depth" and o["status"] not in DEPTHS:
                raise ValueError("status must be one of " + ", ".join(DEPTHS))
            if a == "sonar" and o["status"] not in ("active", "passive"):
                raise ValueError("status must be active|passive")
            if a == "launch":
                if not all(isinstance(o[k], str) and 0 < len(o[k]) <= 80 for k in ("aircraft", "loadout", "mission")):
                    raise ValueError("aircraft, loadout and mission must be strings")
                if o["mission"] not in MISSIONS:
                    raise ValueError("mission must be one of " + ", ".join(sorted(MISSIONS)))
                if "count" in o and (isinstance(o["count"], bool) or not isinstance(o["count"], int) or not 1 <= o["count"] <= 4):
                    raise ValueError("count must be an integer 1-4")
            if a == "engage":
                if isinstance(o["track_id"], bool) or not isinstance(o["track_id"], int) or o["track_id"] <= 0:
                    raise ValueError("track_id must be a positive integer")
                if "salvo" in o and (isinstance(o["salvo"], bool) or not isinstance(o["salvo"], int) or not 1 <= o["salvo"] <= 4):
                    raise ValueError("salvo must be an integer 1-4")
                if "weapon" in o and not (isinstance(o["weapon"], str) and 0 < len(o["weapon"]) <= 80):
                    raise ValueError("weapon must be a weapon id string")
            good.append(o)
        except ValueError as e:
            bad.append((o, str(e)))
    return good, bad


@dataclass
class Decision:
    orders: list = field(default_factory=list)
    assessment: str = ""
    notes: str = ""
    rejected: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    raw: str = ""      # the prompt sent (user turn)
    reply: str = ""    # the model's full reply text, as received
    spent: str = ""    # rounds this side's units expended since the previous window (from its own state)
    next_check_s: float = 0.0   # how long this side wants to run before deciding again (0 = the default step)
    error: str = ""


# ---------------------------------------------------------------- commanders
class LLMCommander:
    def __init__(self, name, provider, objective, roe, step, budget=None):
        self.name, self.provider, self.budget = name, provider, budget or Budget()
        self.system = SYSTEM_PROMPT.format(step=int(step), objective=objective, roe=roe, max_orders=MAX_ORDERS)
        self.prev_tracks, self.last_orders, self.last_results, self.notes = None, [], [], ""
        self.dead, self.prev_ammo, self.prev_units = set(), None, None
        self.t0, self.limit_s = None, None   # mission clock (set by the runner when the scenario has a game-time limit)   # dead: tracks the game refused as destroyed/lost (learned from this side's own refusals only)

    def user_prompt(self, st, window, windows, spent="", events=""):
        if self.t0 is None:
            self.t0 = st["game_time_s"]
        clock = f"MISSION CLOCK: {(st['game_time_s'] - self.t0) / 60:.0f} of {self.limit_s / 60:.0f} minutes used" if self.limit_s else ""
        return render_state(st, self.prev_tracks, self.last_orders, self.last_results, self.notes, window, windows, frozenset(self.dead), spent, events, clock,
                            getattr(self, "last_rejected", ()))

    def _commanded(self):
        """(unit, weapon) pairs this commander's accepted engage orders covered last window; weapon None = any weapon of that unit."""
        c = set()
        for o, r in zip(self.last_orders, self.last_results):
            if o.get("action") == "engage" and r.get("ok"):
                c.add((o["unit_id"], o.get("weapon")))
        return c

    def _events(self, st):
        """Own losses and new damage since the previous window, from this side's own state only."""
        cur = {u["unit_id"]: (u["name"], u.get("damage_pct", 0)) for u in st["own_units"]}
        ev = []
        for uid, (name, dmg) in (self.prev_units or {}).items():
            if uid not in cur:
                ev.append(f"LOST {name} ({uid}) - no longer in your force (destroyed)")
            elif cur[uid][1] - dmg >= 3:
                ev.append(f"DAMAGED {name} ({uid}): {dmg:.0f}% -> {cur[uid][1]:.0f}%")
        self.prev_units = cur
        return "; ".join(ev)

    def decide(self, st, window, windows):
        cur = ammo_map(st)
        ordered, auto = [], []
        cmd = self._commanded()
        for (u, w), p in (self.prev_ammo or {}).items():
            n = p - cur.get((u, w), p)
            if n > 0:
                (ordered if (u, w) in cmd or (u, None) in cmd else auto).append(f"{u}:{w} x{n}")
        spent = ""
        if ordered or auto:
            spent = f"you ordered: {', '.join(ordered) or 'none'}; fired automatically by the units' own AI: {', '.join(auto) or 'none'}"
        self.prev_ammo = cur
        events = self._events(st)
        user = self.user_prompt(st, window, windows, spent, events)
        own_ids = {u["unit_id"] for u in st["own_units"]}
        self.prev_tracks = {c["track_id"] for c in st["contacts"]}
        if self.budget.exhausted():
            return Decision(error="budget exhausted: holding", raw=user)
        d = Decision(raw=user, spent=spent)
        try:
            text, usage = self.provider.complete(self.system, user)
            self.budget.calls += 1
            self.budget.in_tokens += usage.get("in", 0)
            self.budget.out_tokens += usage.get("out", 0)
            self.budget.reported_usd += usage.get("usd", 0.0)
            d.usage, d.reply = usage, text
            obj = extract_json(text)
            d.assessment = str(obj.get("assessment", ""))[:300]
            d.notes = str(obj.get("notes", ""))[:600]
            d.orders, d.rejected = validate_orders(obj.get("orders", []), own_ids)
            self.notes = d.notes
            nc = obj.get("next_check_s")
            if isinstance(nc, (int, float)) and not isinstance(nc, bool):
                d.next_check_s = min(600.0, max(30.0, float(nc)))   # the bridge refuses single steps over 600 s
        except Exception as e:   # bad reply, timeout, provider error: hold this window, keep the match going
            d.error = f"{type(e).__name__}: {e}"
        self.last_orders, self.last_rejected = d.orders, d.rejected
        return d

    def feedback(self, results):
        self.last_results = results
        for o, r in zip(self.last_orders, results):
            if o.get("action") == "engage" and not r.get("ok") and "destroyed" in r.get("message", ""):
                self.dead.add(o["track_id"])


class ScriptedCommander:
    """Deterministic baseline commander (no model). Same decide()/feedback() interface."""
    def __init__(self, name, kind, points=None):
        self.name, self.kind, self.points, self.pstate = name, kind, points or [(13.20, 43.00), (12.95, 43.10)], {}
        self.budget = Budget()

    def decide(self, st, window, windows):
        orders = []
        own = st["side_name"]
        cands = [c for c in st["contacts"] if c.get("lat") is not None and c["side"] not in (own, "Neutral")]
        for u in st["own_units"]:
            if u["type"] != "Vessel" or not u["controllable"]:
                continue
            wp = u["waypoint"]
            if self.kind == "intercept":
                if not cands:
                    continue
                c = min(cands, key=lambda c: nm(u["lat"], u["lon"], c["lat"], c["lon"]))
                tgt = (c["lat"], c["lon"])
                if wp and nm(wp["lat"], wp["lon"], *tgt) <= 1.0:
                    continue
            else:  # patrol
                i = self.pstate.setdefault(u["unit_id"], 0)
                tgt = self.points[i % len(self.points)]
                if nm(u["lat"], u["lon"], *tgt) < 2.0:
                    i += 1; self.pstate[u["unit_id"]] = i; tgt = self.points[i % len(self.points)]
                if wp and nm(wp["lat"], wp["lon"], *tgt) <= 0.5:
                    continue
            orders.append({"action": "move", "unit_id": u["unit_id"], "lat": tgt[0], "lon": tgt[1]})
        return Decision(orders=orders, assessment=f"scripted {self.kind}")

    def feedback(self, results):
        pass


# ---------------------------------------------------------------- ruleset identity
RULESET_ID = "v0"   # bump deliberately when anything the models see or may do changes; results are only comparable within one ruleset


def ruleset_hash():
    """Short hash of everything that shapes what a commander sees and may do (prompt, order set, limits, state rendering, validation).
    shortcut: provider code and match.py defaults are not hashed (they are recorded in each run's manifest); extend if they start to matter."""
    import hashlib, inspect
    parts = [SYSTEM_PROMPT, repr(sorted((k, sorted(v)) for k, v in ACTIONS.items())), repr(sorted((k, sorted(v)) for k, v in OPTIONAL.items())),
             repr(sorted(MISSIONS)), str(MAX_ORDERS), str(STALE_AFTER_S), inspect.getsource(render_state), inspect.getsource(validate_orders)]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:12]
