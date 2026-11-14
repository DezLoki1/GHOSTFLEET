using System;
using System.IO;
using System.Linq;
using System.Text;
using BepInEx;
using SeaPower;
using UnityEngine;

namespace GhostFleet
{
    // Gate B: READ-ONLY. Logs every taskforce and its own PlottingTable. No orders, no state changes.
    // Snapshot every 10 s real time while a mission is loaded, or on F9.
    [BepInPlugin("ghostfleet.gateb", "GhostFleet Gate B", "0.1.0")]
    public partial class Plugin : BaseUnityPlugin
    {
        const float IntervalSec = 10f;
        float _next, _nextBeat;
        bool _keyBroken;
        string _file;

        void Awake()
        {
            var dir = Path.Combine(Paths.BepInExRootPath, "ghostfleet");
            Directory.CreateDirectory(dir);
            _file = Path.Combine(dir, "snapshots.log");
            Logger.LogInfo("GhostFleet loaded. Snapshots: " + _file);
            try { StartBridge(); } catch (Exception e) { Logger.LogError("Bridge failed to start: " + e); }
        }

        void OnDestroy() { StopBridge(); }

        // ---- Gate C: file-polled command dispatcher (main thread). Not for LLM use yet; smoke test only. ----
        // BepInEx/ghostfleet/commands.txt, one command per line, consumed (renamed) when read. Results -> results.log.
        //   move <side> <uid> <lat> <lon> | speed <side> <uid> <knots> | emcon <side> <uid> <0|1>
        //   control <0|1> | pause | resume | dump
        // <side> = Taskforce.Index; the unit must belong to that side's _taskforceUnits (ownership enforced here).
        float _nextPoll;
        int _cmdSeq;
        readonly System.Collections.Generic.Dictionary<ObjectBase, float> _watch = new System.Collections.Generic.Dictionary<ObjectBase, float>();
        float _nextWatch;

        void Res(string s)
        {
            string line = $"[t={GameTime.missionElapsedTime:F1}] {s}";
            File.AppendAllText(Path.Combine(Path.GetDirectoryName(_file), "results.log"), line + System.Environment.NewLine);
            Logger.LogInfo(line);
        }

        void PollCommands()
        {
            if (Time.realtimeSinceStartup < _nextPoll) return;
            _nextPoll = Time.realtimeSinceStartup + 1f;
            if (_watch.Count > 0 && Time.realtimeSinceStartup >= _nextWatch)
            {
                _nextWatch = Time.realtimeSinceStartup + 5f;
                foreach (var kv in _watch.ToList())
                {
                    var o = kv.Key;
                    if (o == null || o.IsDestroyed || Time.realtimeSinceStartup > kv.Value) { _watch.Remove(kv.Key); continue; }
                    Res($"watch uid={o.UniqueID} {o.getName()} geo={o._geoPosition.Latitude:F4},{o._geoPosition.Longitude:F4} kn={o._velocityInKnots:F1} wpdist_mi={o.GetDistToCurrentActiveWaypointInMiles():F2}");
                }
            }
            var path = Path.Combine(Path.GetDirectoryName(_file), "commands.txt");
            if (!File.Exists(path)) return;
            string[] lines;
            var done = Path.Combine(Path.GetDirectoryName(_file), $"commands.done.{++_cmdSeq}.txt");
            try { lines = File.ReadAllLines(path); if (File.Exists(done)) File.Delete(done); File.Move(path, done); }
            catch (IOException) { _cmdSeq--; return; }   // still being written; retry next poll
            foreach (var raw in lines)
            {
                var l = raw.Trim();
                if (l.Length == 0 || l[0] == '#') continue;
                try { Exec(l); } catch (Exception e) { Res($"ERROR '{l}': {e.Message}"); }
            }
        }

        static double D(string s) => double.Parse(s, System.Globalization.CultureInfo.InvariantCulture);

        void Exec(string line)
        {
            var a = line.Split(new[] { ' ', '\t' }, StringSplitOptions.RemoveEmptyEntries);
            switch (a[0].ToLowerInvariant())
            {
                case "pause": GameTime.Pause(); Res("OK pause"); return;
                case "resume": GameTime.UnPause(); Res("OK resume"); return;
                case "dump": _next = 0; Res("OK dump queued"); return;
                case "control": Globals.AllowControlOfEnemyUnits.Value = a[1] == "1"; Res($"OK AllowControlOfEnemyUnits={Globals.AllowControlOfEnemyUnits.Value}"); return;
            }
            // unit-targeted commands: <verb> <side> <uid> ...
            int side = int.Parse(a[1]), uid = int.Parse(a[2]);
            var tfs = Singleton<TaskforceManager>.Instance._taskForces;
            var tf = tfs.FirstOrDefault(t => t != null && t.Index == side);
            if (tf == null) { Res($"REJECT '{line}': no taskforce with Index {side}"); return; }
            var u = tf._taskforceUnits.FirstOrDefault(x => x != null && x.UniqueID == uid);
            if (u == null) { Res($"REJECT '{line}': unit {uid} not owned by side {side}"); return; }
            if (u.IsDestroyed) { Res($"REJECT '{line}': unit destroyed"); return; }
            if (!u.IsControllable || !u.HasControl) { Res($"REJECT '{line}': unit not controllable (ctrl={u.IsControllable}/{u.HasControl})"); return; }
            string before = $"geo={u._geoPosition.Latitude:F4},{u._geoPosition.Longitude:F4} kn={u._velocityInKnots:F1}";
            switch (a[0].ToLowerInvariant())
            {
                case "move":
                {
                    double lat = D(a[3]), lon = D(a[4]);
                    if (double.IsNaN(lat) || double.IsNaN(lon) || Math.Abs(lat) > 85 || Math.Abs(lon) > 180) { Res($"REJECT '{line}': bad coordinates"); return; }
                    u.RemoveWaypoints();
                    var t = u.setWaypointTask(new GeoPosition(lat, lon, u._geoPosition._height));
                    Res($"OK move {u.getName()} (uid {uid}, side {side}) -> {lat:F4},{lon:F4}; task={(t != null)}; wpdist_mi={u.GetDistToCurrentActiveWaypointInMiles():F2}; before {before}; AllowControl={Globals.AllowControlOfEnemyUnits.Value}");
                    break;
                }
                case "speed":
                {
                    double kn = D(a[3]);
                    if (u is Aircraft || u is Helicopter) { Res($"REJECT '{line}': aircraft speed not implemented"); return; }
                    if (kn < 0 || kn > 60) { Res($"REJECT '{line}': speed out of range"); return; }
                    u.SetSpeedCommand(new ConstantSpeed((float)kn, u));
                    Res($"OK speed {u.getName()} -> {kn:F0} kn; before {before}");
                    break;
                }
                case "probe":
                {
                    var cc = u.CourseCommand.Value;
                    int tasks = u._userRoot != null ? u._userRoot.TaskViewModels.Count : -1;
                    Res($"PROBE {u.getName()} uid={uid}: courseCmd={(cc == null ? "none" : cc.GetType().Name)} cmdPos={(u.CommandPosition.HasValue ? u.CommandPosition.Value.Latitude.ToString("F3") + "," + u.CommandPosition.Value.Longitude.ToString("F3") : "none")} heading={u.Heading.Value:F0} kn={u._velocityInKnots:F1} userTasks={tasks} formation={(u.Formation != null)} aligning={u.Aligning} state={u.StateMachine?.CurrentState?.GetType().Name} hasAI={(u._ai != null)}");
                    return;
                }
                case "clearcourse":
                    u.CourseCommand.Value = null;
                    Res($"OK clearcourse {u.getName()}");
                    break;
                case "emcon":
                    u.setEMCON(a[3] == "1");
                    Res($"OK emcon {u.getName()} -> {a[3]}");
                    break;
                default: Res($"REJECT '{line}': unknown verb"); return;
            }
            _watch[u] = Time.realtimeSinceStartup + 90f;
        }

        void Update()
        {
            try { if (Singleton<TaskforceManager>.InstanceExists()) PollCommands(); }
            catch (Exception e) { Logger.LogError("PollCommands failed: " + e); }
            try { PumpBridge(); }
            catch (Exception e) { Logger.LogError("PumpBridge failed: " + e); }
            bool force = false;
            if (!_keyBroken)
            {
                try { force = Input.GetKeyDown(KeyCode.F9); }
                catch (Exception) { _keyBroken = true; Logger.LogWarning("Legacy Input unavailable; F9 disabled, timer only."); }
            }
            if (!force && Time.realtimeSinceStartup < _next) return;
            _next = Time.realtimeSinceStartup + IntervalSec;
            try { Snapshot(); }
            catch (Exception e) { Logger.LogError("Snapshot failed: " + e); }
        }

        void Snapshot()
        {
            bool have = Singleton<TaskforceManager>.InstanceExists();
            var tfs = have ? Singleton<TaskforceManager>.Instance._taskForces : null;
            if (tfs == null || tfs.Count == 0)
            {
                if (Time.realtimeSinceStartup >= _nextBeat) { _nextBeat = Time.realtimeSinceStartup + 30f; Logger.LogInfo($"heartbeat: TaskforceManager={have} taskforces={tfs?.Count ?? 0}"); }
                return;
            }

            var sb = new StringBuilder();
            sb.AppendLine($"=== t={GameTime.missionElapsedTime:F1}s paused={GameTime.IsPaused()} taskforces={tfs.Count} player={Globals._playerTaskforce?.DisplayName} ===");
            foreach (var tf in tfs)
            {
                if (tf == null) continue;
                var pt = tf.PlottingTable;
                int own = 0, contacts = 0;
                var lines = new StringBuilder();
                if (pt != null)
                {
                    foreach (var kv in pt.LocalVehicles)
                    {
                        var v = kv.Value;
                        if (kv.Key == null || v == null) continue;
                        if (kv.Key._taskforce == tf) { own++; continue; }   // own units reported to self
                        contacts++;
                        // SIDE VIEW ONLY: track id, reported class/side, estimated position. No true transform, no UniqueID.
                        string cls = v.Class.HasValue ? v.Class.Value.Value : "?";
                        string side = v.UnitTaskforce.Value != null ? v.UnitTaskforce.Value.DisplayName : "?";
                        string pos = "none";
                        var est = v.PositionEstimate;
                        if (est.HasValue)
                            pos = $"{est.Value.Item1.Latitude:F4},{est.Value.Item1.Longitude:F4} sigmaMaj={est.Value.Item2.MajorStdDev:F1} sigmaMin={est.Value.Item2.MinorStdDev:F1}";
                        lines.AppendLine($"    trk={v.Id} class={cls} side={side} ident={v.Identified.Value} dormant={v.IsDormant.Value} live={v.HasLiveTrack} age={GameTime.missionElapsedTime - v.UpdateEpoch:F0}s pos={pos}");
                    }
                }
                // TRUTH (omniscient, diagnostic only, never for a commander): other-side units that exist vs. how many this side tracks.
                int trueOthers = tfs.Where(o => o != null && o != tf).Sum(o => o._taskforceUnits.Count(u => u != null && !u.IsDestroyed));
                sb.AppendLine($"[{tf.Index}] {tf.DisplayName} type={tf.Side} ai={tf.IsAI} units={tf._taskforceUnits.Count} (V{tf._taskforceVessels.Count} S{tf._taskforceSubmarines.Count} A{tf._taskforceAircraft.Count} H{tf._taskforceHelicopters.Count} L{tf._taskforceLandUnits.Count}) table={(pt != null)} own_in_table={own} contacts={contacts} TRUTH_other_side_units={trueOthers}");
                foreach (var o in tf._taskforceUnits.Where(u => u != null && !u.IsDestroyed).Take(40))
                    sb.AppendLine($"    own uid={o.UniqueID} {o.getName()} type={o._type} geo={o._geoPosition.Latitude:F4},{o._geoPosition.Longitude:F4} ctrl={o.IsControllable}/{o.HasControl} acceptsPlayerOrders={o.AcceptsOrdersFromPlayer} kn={o._velocityInKnots:F1} wpdist_mi={o.GetDistToCurrentActiveWaypointInMiles():F2}");
                sb.Append(lines);
            }
            sb.AppendLine($"AllowControlOfEnemyUnits={Globals.AllowControlOfEnemyUnits.Value}");
            File.AppendAllText(_file, sb.ToString());
            Logger.LogInfo($"Snapshot written t={GameTime.missionElapsedTime:F0}s, {tfs.Count} taskforces");
        }
    }
}
