using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Text;
using System.Threading;
using SeaPower;
using UnityEngine;

namespace GhostFleet
{
    // Gate E: loopback-only HTTP bridge. HTTP threads never touch game objects; every request becomes a Job
    // that runs on the Unity main thread inside Update() (PumpBridge).
    //   GET  /state?side=<Taskforce.Index>      side-scoped view (own units + that side's own plotting-table tracks)
    //   POST /orders?side=<idx>   {"orders":[{"action":"move","unit_id":8,"lat":13.2,"lon":43.0}, ...]}
    //   POST /time?cmd=pause|resume|step|scale&seconds=30&value=4
    // shortcut: unknown JSON keys are ignored (JsonUtility), no auth (127.0.0.1 only); upgrade before any non-local use.
    public partial class Plugin
    {
        const string Prefix = "http://127.0.0.1:8765/";
        const int MaxOrdersPerCall = 20, MaxBody = 64 * 1024, JobTimeoutMs = 5000;

        class Job { public Func<string> Fn; public string Result; public int Status = 200; public readonly ManualResetEventSlim Done = new ManualResetEventSlim(); }
#pragma warning disable 0649 // fields are filled by JsonUtility
        class OrderDto { public string action; public int unit_id; public double lat; public double lon; public double knots; public int on; public string status; public int track_id; public int salvo; public string weapon; }
#pragma warning restore 0649

        HttpListener _http;
        readonly ConcurrentQueue<Job> _jobs = new ConcurrentQueue<Job>();
        double _stepUntil;

        void StartBridge()
        {
            _http = new HttpListener();
            _http.Prefixes.Add(Prefix);
            _http.Start();
            new Thread(AcceptLoop) { IsBackground = true, Name = "GhostFleet-http" }.Start();
            Logger.LogInfo("Bridge listening on " + Prefix);
        }

        void StopBridge() { try { _http?.Close(); } catch { } }

        void AcceptLoop()
        {
            while (_http != null && _http.IsListening)
            {
                HttpListenerContext c;
                try { c = _http.GetContext(); } catch { break; }
                ThreadPool.QueueUserWorkItem(_ => Handle(c));
            }
        }

        void Reply(HttpListenerContext c, int code, string body)
        {
            try
            {
                var b = Encoding.UTF8.GetBytes(body);
                c.Response.StatusCode = code; c.Response.ContentType = "application/json";
                c.Response.OutputStream.Write(b, 0, b.Length); c.Response.Close();
            }
            catch { }
        }

        static string Err(string m) => "{\"error\":" + Q(m) + "}";
        static string Q(string s) => "\"" + (s ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\n", " ").Replace("\r", " ") + "\"";
        static string F(double d) => double.IsNaN(d) || double.IsInfinity(d) ? "null" : d.ToString("0.#####", CultureInfo.InvariantCulture);

        void Handle(HttpListenerContext c)
        {
            try
            {
                if (!IPAddress.IsLoopback(c.Request.RemoteEndPoint.Address)) { Reply(c, 403, Err("loopback only")); return; }
                string path = c.Request.Url.AbsolutePath.TrimEnd('/'), method = c.Request.HttpMethod;
                var q = c.Request.QueryString;
                Func<string> fn;
                if (path == "/state" && method == "GET")
                {
                    int side = int.Parse(q["side"] ?? "x");
                    fn = () => BuildState(side);
                }
                else if (path == "/truth" && method == "GET")
                {
                    fn = BuildTruth;   // OMNISCIENT: scoring/spectator only. Never include in a commander prompt.
                }
                else if (path == "/orders" && method == "POST")
                {
                    int side = int.Parse(q["side"] ?? "x");
                    if (c.Request.ContentLength64 > MaxBody) { Reply(c, 413, Err("body too large")); return; }
                    string body; using (var r = new StreamReader(c.Request.InputStream, Encoding.UTF8)) body = r.ReadToEnd();
                    if (body.Length > MaxBody) { Reply(c, 413, Err("body too large")); return; }
                    fn = () => ApplyOrders(side, body);
                }
                else if (path == "/time" && method == "POST")
                {
                    string cmd = q["cmd"]; double sec = double.Parse(q["seconds"] ?? "0", CultureInfo.InvariantCulture), val = double.Parse(q["value"] ?? "1", CultureInfo.InvariantCulture);
                    fn = () => TimeCmd(cmd, sec, val);
                }
                else { Reply(c, 404, Err("unknown route")); return; }

                var job = new Job { Fn = fn };
                _jobs.Enqueue(job);
                if (!job.Done.Wait(JobTimeoutMs)) { Reply(c, 504, Err("main thread did not respond (game not in a mission?)")); return; }
                Reply(c, job.Status, job.Result);
            }
            catch (FormatException) { Reply(c, 400, Err("bad query parameter")); }
            catch (Exception e) { Reply(c, 500, Err(e.Message)); }
        }

        // Runs on the Unity main thread.
        void PumpBridge()
        {
            if (_stepUntil > 0 && GameTime.missionElapsedTime >= _stepUntil) { GameTime.Pause(); _stepUntil = 0; }
            for (int n = 0; n < 20 && _jobs.TryDequeue(out var j); n++)
            {
                try { j.Result = j.Fn(); }
                catch (ArgumentException e) { j.Status = 400; j.Result = Err(e.Message); }
                catch (Exception e) { j.Status = 500; j.Result = Err(e.GetType().Name + ": " + e.Message); }
                j.Done.Set();
            }
        }

        Taskforce SideOrThrow(int side)
        {
            if (!Singleton<TaskforceManager>.InstanceExists()) throw new ArgumentException("no mission loaded");
            var tf = Singleton<TaskforceManager>.Instance._taskForces.FirstOrDefault(t => t != null && t.Index == side);
            if (tf == null) throw new ArgumentException("no taskforce with Index " + side);
            return tf;
        }

        double _scale = 1;
        void ApplyScale() { if (_scale > 1) GameTime.SetTimeCompression((float)_scale); }

        string TimeCmd(string cmd, double sec, double val)
        {
            if (!Singleton<TaskforceManager>.InstanceExists()) throw new ArgumentException("no mission loaded");
            switch (cmd)
            {
                case "pause": _stepUntil = 0; GameTime.Pause(); break;
                case "resume": _stepUntil = 0; GameTime.UnPause(); ApplyScale(); break;
                case "step":
                    if (sec <= 0 || sec > 600) throw new ArgumentException("seconds must be in (0,600]");
                    _stepUntil = GameTime.missionElapsedTime + sec; GameTime.UnPause(); ApplyScale(); break;
                case "scale":
                    // UnPause() restores the stock speed-list index, so remember the scale and re-apply it after every unpause.
                    if (val < 1 || val > 16) throw new ArgumentException("value must be in [1,16]");
                    _scale = val; if (!GameTime.IsPaused()) ApplyScale(); break;
                default: throw new ArgumentException("cmd must be pause|resume|step|scale");
            }
            return "{\"ok\":true,\"game_time_s\":" + F(GameTime.missionElapsedTime) + ",\"paused\":" + (GameTime.IsPaused() ? "true" : "false") + "}";
        }

        // SIDE-SCOPED: own units (true state of the requester's own forces) + tracks from the requester's own plotting table.
        // Never emitted: other-side UniqueIDs, true positions, Vehicle.BaseObject, RealUnityPosition.
        string BuildState(int side)
        {
            var tf = SideOrThrow(side);
            var sb = new StringBuilder();
            sb.Append("{\"side\":").Append(tf.Index).Append(",\"side_name\":").Append(Q(tf.DisplayName));
            sb.Append(",\"game_time_s\":").Append(F(GameTime.missionElapsedTime));
            sb.Append(",\"paused\":").Append(GameTime.IsPaused() ? "true" : "false");
            sb.Append(",\"stepping\":").Append(_stepUntil > 0 ? "true" : "false");
            sb.Append(",\"own_units\":[");
            bool first = true;
            foreach (var u in tf._taskforceUnits)
            {
                if (u == null || u.IsDestroyed) continue;
                if (!first) sb.Append(','); first = false;
                var wp = u.CommandPosition;
                sb.Append("{\"unit_id\":").Append(u.UniqueID).Append(",\"name\":").Append(Q(u.getName()))
                  .Append(",\"type\":").Append(Q(u._type.ToString()))
                  .Append(",\"lat\":").Append(F(u._geoPosition.Latitude)).Append(",\"lon\":").Append(F(u._geoPosition.Longitude))
                  .Append(",\"heading\":").Append(F(u.Heading.Value)).Append(",\"speed_kn\":").Append(F(u._velocityInKnots))
                  .Append(",\"weapons\":").Append(Q(u._weaponStatus.ToString().ToLowerInvariant()))
                  .Append(",\"emcon\":").Append(u.Emcon.Value ? "true" : "false")   // true = emitters silenced
                  .Append(",\"radar_on\":").Append(u.IsAirSearchRadarsOn.Value || u.IsSurfaceSearchRadarsOn.Value ? "true" : "false")
                  .Append(",\"sonar_on\":").Append(u.IsActiveSonarsOn.Value ? "true" : "false")
                  .Append(",\"controllable\":").Append(u.IsControllable && u.HasControl ? "true" : "false")
                  .Append(",\"ammo\":").Append(AmmoJson(u))
                  .Append(",\"waypoint\":").Append(wp.HasValue ? "{\"lat\":" + F(wp.Value.Latitude) + ",\"lon\":" + F(wp.Value.Longitude) + "}" : "null")
                  .Append('}');
            }
            sb.Append("],\"contacts\":[");
            first = true;
            if (tf.PlottingTable != null)
            {
                foreach (var kv in tf.PlottingTable.LocalVehicles)
                {
                    var v = kv.Value;
                    if (kv.Key == null || v == null || kv.Key._taskforce == tf) continue;   // skip own units/weapons
                    if (!first) sb.Append(','); first = false;
                    string cls = v.Class.HasValue ? v.Class.Value.Value : null;
                    string sd = v.UnitTaskforce.Value != null ? v.UnitTaskforce.Value.DisplayName : null;
                    var est = v.PositionEstimate;
                    sb.Append("{\"track_id\":").Append(v.Id)
                      .Append(",\"class\":").Append(cls == null ? "null" : Q(cls))
                      .Append(",\"side\":").Append(sd == null ? "null" : Q(sd))
                      .Append(",\"identified\":").Append(v.Identified.Value ? "true" : "false")
                      .Append(",\"live\":").Append(v.HasLiveTrack ? "true" : "false")
                      .Append(",\"age_s\":").Append(F(GameTime.missionElapsedTime - v.UpdateEpoch));
                    if (est.HasValue)
                        sb.Append(",\"lat\":").Append(F(est.Value.Item1.Latitude)).Append(",\"lon\":").Append(F(est.Value.Item1.Longitude))
                          .Append(",\"sigma_major\":").Append(F(est.Value.Item2.MajorStdDev)).Append(",\"sigma_minor\":").Append(F(est.Value.Item2.MinorStdDev));
                    sb.Append('}');
                }
            }
            sb.Append("]}");
            return sb.ToString();
        }

        // Own unit's remaining weapons: id (usable as the engage order's "weapon"), count, kind, max range in nm. Own data only.
        static string AmmoJson(ObjectBase u)
        {
            var sb = new StringBuilder("[");
            bool first = true;
            foreach (var kv in u.AmmunitionAmountDictionary)
            {
                if (kv.Value <= 0) continue;
                Ammunition a; u.AmmunitionNameToAmmunitionDictionary.TryGetValue(kv.Key, out a);
                if (a == null || a._ap == null) continue;
                if (!first) sb.Append(','); first = false;
                sb.Append("{\"id\":").Append(Q(kv.Key)).Append(",\"count\":").Append(kv.Value)
                  .Append(",\"kind\":").Append(Q(a._ap._type.ToString())).Append(",\"max_range_nm\":").Append(F(a._ap._maxLaunchRangeInMiles)).Append('}');
            }
            return sb.Append(']').ToString();
        }

        static int AmmoTotal(ObjectBase u) { int n = 0; foreach (var kv in u.AmmunitionAmountDictionary) n += Math.Max(0, kv.Value); return n; }

        string BuildTruth()
        {
            if (!Singleton<TaskforceManager>.InstanceExists()) throw new ArgumentException("no mission loaded");
            // the game's own end-of-mission signal (victory/defeat trigger fired, or the debrief is showing); the runner treats it as authoritative
            bool ended = Singleton<MissionManager>.InstanceExists() && (Singleton<MissionManager>.Instance.MissionEnded || Singleton<MissionManager>.Instance._endMissionTime > 0.0);
            var sb = new StringBuilder("{\"game_time_s\":" + F(GameTime.missionElapsedTime) + ",\"mission_ended\":" + (ended ? "true" : "false") + ",\"sides\":[");
            bool firstSide = true;
            foreach (var tf in Singleton<TaskforceManager>.Instance._taskForces)
            {
                if (tf == null) continue;
                if (!firstSide) sb.Append(','); firstSide = false;
                sb.Append("{\"side\":").Append(tf.Index).Append(",\"name\":").Append(Q(tf.DisplayName))
                  .Append(",\"kill_score\":").Append(F(tf._killScore)).Append(",\"loss_score\":").Append(F(tf._lossScore)).Append(",\"units\":[");
                bool first = true;
                foreach (var u in tf._taskforceUnits)
                {
                    if (u == null) continue;
                    if (!first) sb.Append(','); first = false;
                    sb.Append("{\"unit_id\":").Append(u.UniqueID).Append(",\"name\":").Append(Q(u.getName())).Append(",\"type\":").Append(Q(u._type.ToString()))
                      .Append(",\"alive\":").Append(u.IsDestroyed ? "false" : "true").Append(",\"ammo_total\":").Append(AmmoTotal(u))
                      .Append(",\"lat\":").Append(F(u._geoPosition.Latitude)).Append(",\"lon\":").Append(F(u._geoPosition.Longitude)).Append('}');
                }
                sb.Append("]}");
            }
            return sb.Append("]}").ToString();
        }

        static readonly HashSet<string> OrderKeys = new HashSet<string> { "action", "unit_id", "lat", "lon", "knots", "on", "status", "track_id", "salvo", "weapon" };

        // Strict: body must be {"orders":[{...}]}; unknown keys or wrong types reject the whole batch (nothing applied).
        static List<OrderDto> ParseOrders(string body)
        {
            object root;
            try { root = MiniJson.Parse(body); } catch (FormatException e) { throw new ArgumentException("invalid JSON: " + e.Message); }
            var rd = root as Dictionary<string, object>;
            var arr = rd != null && rd.Count == 1 && rd.ContainsKey("orders") ? rd["orders"] as List<object> : null;
            if (arr == null) throw new ArgumentException("body must be exactly {\"orders\":[...]}");
            var list = new List<OrderDto>();
            foreach (var item in arr)
            {
                var d = item as Dictionary<string, object>;
                if (d == null) throw new ArgumentException("each order must be an object");
                foreach (var k in d.Keys) if (!OrderKeys.Contains(k)) throw new ArgumentException("unknown order key '" + k + "'");
                var o = new OrderDto { action = d.ContainsKey("action") ? d["action"] as string : null, lat = double.NaN, lon = double.NaN };
                if (o.action == null) throw new ArgumentException("order needs a string 'action'");
                if (!d.ContainsKey("unit_id") || !(d["unit_id"] is double)) throw new ArgumentException("order needs a numeric 'unit_id'");
                o.unit_id = (int)(double)d["unit_id"];
                foreach (var f in new[] { "lat", "lon", "knots", "on", "track_id", "salvo" })
                    if (d.ContainsKey(f) && !(d[f] is double)) throw new ArgumentException("'" + f + "' must be a number");
                if (d.ContainsKey("track_id")) o.track_id = (int)(double)d["track_id"];
                if (d.ContainsKey("salvo")) o.salvo = (int)(double)d["salvo"];
                if (d.ContainsKey("weapon")) { o.weapon = d["weapon"] as string; if (o.weapon == null) throw new ArgumentException("'weapon' must be a string"); }
                if (d.ContainsKey("lat")) o.lat = (double)d["lat"];
                if (d.ContainsKey("lon")) o.lon = (double)d["lon"];
                if (d.ContainsKey("knots")) o.knots = (double)d["knots"];
                if (d.ContainsKey("on")) o.on = (int)(double)d["on"];
                if (d.ContainsKey("status")) { o.status = d["status"] as string; if (o.status == null) throw new ArgumentException("'status' must be a string"); }
                list.Add(o);
            }
            return list;
        }

        string ApplyOrders(int side, string body)
        {
            var tf = SideOrThrow(side);
            var orders = ParseOrders(body);
            if (orders.Count > MaxOrdersPerCall) throw new ArgumentException("max " + MaxOrdersPerCall + " orders per call");
            var sb = new StringBuilder("{\"results\":[");
            for (int i = 0; i < orders.Count; i++)
            {
                var o = orders[i];
                string msg; bool ok = ApplyOne(tf, o, out msg);
                Res($"bridge side={side} {(ok ? "OK" : "REJECT")} {o.action} unit={o.unit_id}: {msg}");
                if (i > 0) sb.Append(',');
                sb.Append("{\"unit_id\":").Append(o.unit_id).Append(",\"action\":").Append(Q(o.action)).Append(",\"ok\":").Append(ok ? "true" : "false").Append(",\"message\":").Append(Q(msg)).Append('}');
            }
            return sb.Append("]}").ToString();
        }

        bool ApplyOne(Taskforce tf, OrderDto o, out string msg)
        {
            var u = tf._taskforceUnits.FirstOrDefault(x => x != null && x.UniqueID == o.unit_id);   // ownership: only this side's units
            if (u == null) { msg = "unit not owned by this side"; return false; }
            if (u.IsDestroyed) { msg = "unit destroyed"; return false; }
            if (!u.IsControllable || !u.HasControl) { msg = "unit not controllable"; return false; }
            switch (o.action)
            {
                case "move":
                    if (double.IsNaN(o.lat) || double.IsNaN(o.lon) || Math.Abs(o.lat) > 85 || Math.Abs(o.lon) > 180) { msg = "bad coordinates"; return false; }
                    if (u._userRoot == null) { msg = "unit has no task graph"; return false; }
                    u.RemoveWaypoints();
                    u.setWaypointTask(new GeoPosition(o.lat, o.lon, u._geoPosition._height));
                    msg = "waypoint set"; return true;
                case "speed":
                    if (u is Aircraft || u is Helicopter) { msg = "aircraft speed not implemented"; return false; }
                    if (o.knots < 0 || o.knots > 60) { msg = "speed out of range 0-60"; return false; }
                    u.SetSpeedCommand(new ConstantSpeed((float)o.knots, u));
                    msg = "speed set"; return true;
                case "emcon":   // legacy (file commands only): setEMCON(false) merely clears the flag; CheckEMCON() re-derives it from real sensor state
                    u.setEMCON(o.on == 1);
                    msg = "emcon " + (o.on == 1 ? "on" : "off"); return true;
                case "emitters":
                {
                    string m = (o.status ?? "").ToLowerInvariant();
                    if (m == "silent") { u.setEMCON(true); msg = "emitters silent (search radars, active sonar, jammers off)"; return true; }
                    if (m != "active") { msg = "status must be silent|active"; return false; }
                    u.setEMCON(false);
                    if (!(u is Submarine)) { u.EnableAirSearchRadars(); u.EnableSurfaceSearchRadars(); }
                    msg = "emitters active requested (takes effect when time advances; has no effect on units without a search radar)"; return true;
                }
                case "weapons":
                {
                    // posture only: the unit's own AI decides targets/shots within it (no detection/ROE bypass).
                    ObjectBase.WeaponStatus ws;
                    switch ((o.status ?? "").ToLowerInvariant())
                    {
                        case "hold": ws = ObjectBase.WeaponStatus.Hold; break;
                        case "tight": ws = ObjectBase.WeaponStatus.Tight; break;
                        case "free": ws = ObjectBase.WeaponStatus.Free; break;
                        default: msg = "status must be hold|tight|free"; return false;
                    }
                    u.SetWeaponStatus(ws);
                    msg = "weapons " + ws.ToString().ToLowerInvariant(); return true;
                }
                case "engage":
                {
                    // Same call the player's click-to-attack uses: the unit's own AI picks the weapon, checks range/ammo/arcs and queues the task.
                    // Target must be a track in THIS side's plotting table that the game itself shows as hostile (never a neutral/unknown/own track).
                    if (o.track_id <= 0) { msg = "engage needs a track_id"; return false; }
                    int salvo = o.salvo <= 0 ? 1 : o.salvo;
                    if (salvo > 4) { msg = "salvo must be 1-4"; return false; }
                    ObjectBase tgt = null; Vehicle tv = null;
                    if (tf.PlottingTable != null)
                        foreach (var kv in tf.PlottingTable.LocalVehicles)
                            if (kv.Key != null && kv.Value != null && kv.Value.Id == o.track_id && kv.Key._taskforce != tf) { tgt = kv.Key; tv = kv.Value; break; }
                    if (tgt == null) { msg = "no such track in your picture"; return false; }
                    if (tgt.IsDestroyed) { msg = "track lost or target destroyed"; return false; }
                    if (!tv.Identified.Value || tv.CurrentRelationship() != RelationsState.Hostile) { msg = "track is not identified as hostile - engage refused"; return false; }
                    if (u._ai == null) { msg = "unit has no weapons AI"; return false; }
                    try
                    {
                        if (u._ai.AmmunitionForTarget(tgt).Count == 0) { msg = "this unit has no weapon that can engage that target (no ammo or wrong target type)"; return false; }
                        if (u is Aircraft) { u._ai.AutoAttackByClick(tgt, Ammunition.Type.None, false, salvo, false, false); msg = "attack designated (aircraft AI chooses weapon and timing)"; return true; }
                        int before = u._currentEngageTasks.Count;
                        if (!string.IsNullOrEmpty(o.weapon))
                        {
                            if (!u._ai.AmmunitionForTarget(tgt).Any(a => a._ap != null && a._ap._ammunitionFileName == o.weapon)) { msg = "weapon not usable on that target (no ammo or wrong target type)"; return false; }
                            u.AddEngageTask(new EngageTask(o.weapon, tgt, u, salvo));
                        }
                        else u._ai.AutoAttackByClick(tgt, Ammunition.Type.None, false, salvo, false, false);
                        int queued = u._currentEngageTasks.Count - before;
                        if (queued <= 0) { msg = "no weapon on this unit can engage that target now (out of range, no ammo or wrong target type)"; return false; }
                        msg = "engage queued: " + queued + " task(s), salvo " + salvo; return true;
                    }
                    catch (Exception e) { msg = "engage failed: " + e.GetType().Name; Logger.LogError("engage: " + e); return false; }
                }
                default:
                    msg = "unknown action (move|speed|emitters|weapons|engage)"; return false;
            }
        }
    }
}
