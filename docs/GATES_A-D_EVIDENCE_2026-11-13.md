# Sea Power AI Commanders: static evidence for Gates A-D

**Build inspected:** 0.9.2 build 261113 (`changelog.txt`), Unity 6000.0.85, **Mono** (`MonoBleedingEdge`), so Harmony/BepInEx 5 is viable.
**Method:** `ilspycmd` decompile of `Sea Power_Data/Managed/Seapower-Scripts.dll` into scratch (read-only; game folder untouched). Nothing was run. BepInEx is **not installed**, so every "runtime" item below is still unverified.
Decompile output: scratch directory (regenerate with `ilspycmd -p -o <dir> <Seapower-Scripts.dll>`, ~25 s; game files are not included in this repo).

## Evidence table

| Gate / question | Finding in 0.9.2 (file:line in decompile) | Status |
|---|---|---|
| **1. Enumerate both taskforces** | `TaskforceManager._taskForces` (`Seapower/TaskforceManager.cs:14`); also `SideRelations.Sides` (`SideRelations.cs:21`, v0.9 multi-side). `Taskforce.TfType` = None/Player/Ally/Enemy/Neutral (`Taskforce.cs:16`). Each has `_taskforceVessels/Submarines/Aircraft/Helicopters/LandUnits`, `TaskforceObjects`, `Index`, `Relations`. | Source-confirmed. How v0.9 maps "extra" sides onto TfType is unknown. |
| **2a. Per-side picture exists for non-player sides** | Ctor `Taskforce(TfType, name)` creates `PlottingTable = new PlottingTable(this)` for **every** side (`Taskforce.cs:464`). `updateTaskforceContacts()` / `updateIncremental()` run it (`:879,:885`). The game's own enemy `AIPlayer` consumes `AIPlayerTaskforce.PlottingTable.LocalVehicles` as its detected-enemy set (`AIPlayer.cs:54-56`). | **Source-confirmed; this was the recon's #1 unknown.** Runtime content still to be viewed. |
| **2b. What is the honest (reported) contact data** | `PlottingTable.LocalVehicles : Dictionary<ObjectBase, Vehicle>` (`PlottingTable.cs:39`). Per `Vehicle`: `Id` (track number, allocated per table, `:227`), `PositionEstimate` = `(GeoPosition, PositionEigpairs, Entity)?` (detected-or-extrapolated, `Vehicle.cs:171`), `HasLiveTrack`, `Class` (`SourcedProperty<string>`), `UnitTaskforce` (null until classified, `IsClassified`), `Identified`, `IsDormant`, `Altitude`, `Side`, `UpdateEpoch`, `DetectingSensors`, `ErrorEllipse`. | Source-confirmed. `PositionEigpairs` gives uncertainty; use it, not a made-up `uncertainty_nm`. |
| **2c. Leak points to never serialize** | `Vehicle.BaseObject`/`Object`, `Vehicle.RealUnityPosition` (`:195`, true transform), and the **dictionary key** `ObjectBase` of `LocalVehicles` (its `UniqueID`, `_taskforce`, damage, ammo). Also `AIPlayer.AllOppositeSideUnits` is omniscient. | Design rule. Key by `Vehicle.Id`, emit only reported fields. |
| **2d. Sensor-sharing (new in v0.9)** | `SharedSensorPicture.Exchange(IList<Taskforce>, SideRelations)` merges pictures between related sides. | **Risk:** relations between BLUE and RED must be Hostile/no-share in the test mission or pictures merge. Read this class before Gate D. |
| **3a. Orders: signatures still exist** | `ObjectBase.setWaypointTask(GeoPosition, string="wp", WaypointHeightState)` returns `GoToWaypointTask` (`ObjectBase.cs:5519`); `AddEngageTask(EngageTask)` (`:4404`); `RemoveWaypoints()` (`:5092`); `setEMCON(bool, bool)` (`:6268`); `SetWeaponStatus(status, formationCheck)` (`:3286`); `SetSpeedCommand(in SpeedRequest)` (`:1939`); `Submarine.setDepth(float)` (`Submarine.cs:1336`); `FlightDeck.createLaunchTask(...)` (`FlightDeck.cs:2298`); sensor enable/disable set (`ObjectBase.cs:11407-11732`). | Source-confirmed. |
| **3b. Mod's call sites match 0.9.2** | `new EngageTask(string, ObjectBase\|Vector3, ObjectBase, int, ...)` ctors exist (`EngageTask.cs:71,100`); `ConstantSpeed`/`ConstantMach` are `readonly struct(float, ObjectBase)` (matches `StateSerializer.cs:116,120`); `ObjectBase.Awake()` public and `OnDestroy()` private (still Harmony-patchable). | No drift found in the sampled set. A full **compile** of the mod against 0.9.2 was not done (needs BepInEx.dll). |
| **3c. Can one process order the RED unit** | F10 "(c)" is just the global `Globals.AllowControlOfEnemyUnits` (`ReactiveProperty<bool>`, `Globals.cs:672`; toggled `DM.cs:892`, `DM_TestTools.cs:458`). `ObjectBase.AcceptsOrdersFromPlayer` = true for every non-Player taskforce when it is set (`ObjectBase.cs:1233-1243`). A plugin can set it directly; no player-side swap. | Source-confirmed. Whether `setWaypointTask` itself checks it (vs only the UI) is unverified. |
| **3d. Game AI arbitration** | Per-unit `AI` (`AI.cs`) gates on `HasControl`/`IsControllable` (e.g. `:604`, `:3395`) and has `IsProcessed(target)` (`:5240`). Taskforce AI for non-player sides runs `CheckASWAssignment/CheckVIDAssignment` every 10 s when `Globals._allowEnemyUnitsAttackPlayer` (`Taskforce.cs:1122-1129`). Global enemy commander `AIPlayer` is a NodeCanvas behaviour tree; `IsForcedToDisable` is a static defaulting to true in `InitStaticFields` (`AIPlayer.cs:86`). | **Fairness asymmetry:** RED (Enemy type) gets taskforce AI helpers and BLUE (Player) does not. Runtime test needed to see whether AI overwrites an external waypoint. |
| **4a. Pause / time** | `GameTime.Pause(bool)` sets `TimeCompression=0` and `InputHandler.IsInPauseMode` (`GameTime.cs:280`); `UnPause()`, `SetTimeCompression(float)` (`:198`), `IsPaused()` = `Time.timeScale < 0.05` (`:318`); `missionElapsedTime` (double). `Update()` keeps running at timeScale 0, so main-thread bridge polling survives pause. | Source-confirmed; step-wise advance (unpause N s) is design work. |
| **4b. Reload a scenario headlessly** | Not found by name. Only `CampaignMain.LoadScenario/StartMission`, `MissionEditorViewModel.LoadMission(filename, reloadEditorMission)`. Mission entry is likely via `SceneCreator`/`GameMain` and a menu view-model. | **Open.** Needs a follow-up trace from the main-menu "start mission" path. |

## What changed vs the recon doc
- Installed game is **0.9.2**, not 0.9.0 (build 261113 includes campaign fixes only per changelog head; no API notes).
- Unknown #1 (non-player `PlottingTable`) is resolved at source level: tables exist for all sides and the game's own AI reads one.
- Multi-side v0.9 data model is `SideRelations`; `TfType` still only has Player/Ally/Enemy/Neutral, so "two symmetric LLM sides" probably means BLUE=Player and RED=Enemy (or two Allies) with the asymmetries above.

## Not done / not verified
- No code run, no plugin loaded, BepInEx/Anchor Chain not installed (that changes the game folder; I held off).
- Mod source not compiled against 0.9.2 (BepInEx.dll not obtainable from NuGet; would need the GitHub 5.4.x release).
- Runtime behaviors: AI overwriting orders, `setWaypointTask` without the enemy-control flag, what `LocalVehicles` actually contains at t=0 for RED, UniqueID stability after reload.

## Suggested next step
Gate A (manual, 5 min) and Gate B (read-only plugin that logs `TaskforceManager._taskForces` + each `PlottingTable.LocalVehicles` count, keyed by `Vehicle.Id`) on a **copy** of the game folder with BepInEx 5.4.x installed, so retail stays pristine.

---
## Runtime results (Gate B plugin, first mission run, 2026-11-13 ~22:58)

Mission: Red Sea scenario (lat ~12.9N, lon ~43.1E), 3 taskforces: Taskforce1 = Player (Yorktown CG-48 + E-2C), Taskforce2 = Enemy/AI (13 units), Neutral (3 merchants). Plugin: `GHOSTFLEET/Plugin.cs` -> `BepInEx/ghostfleet/snapshots.log`.

| Check | Result |
|---|---|
| Plugin loads on 0.9.2 | Yes, BepInEx 5.4.23.6, compiled against 0.9.2 DLLs, 0 errors |
| Both sides' tables exist and are populated | Yes. Enemy (Taskforce2) table had 4-9 contacts, Player 6-12. All `table=True`. |
| Fog of war holds | Yes. Player saw ~6-7 of 16 true other-side units; land sites, MiGs, two merchants and one warship stayed hidden for the whole run. Enemy saw 4 of 5. |
| Table is empty before the first sim tick | Yes. t=0 (paused): 0 contacts on every side; contacts appear by t=8 s. |
| Estimates differ from truth | Yes. One ESM-only track (Cross Slot radar site) was reported ~0.3 deg off true position, sigmaMaj ~28.8 km, `live=False`. Radar tracks had sigma 0.0 and matched true position. |
| Weapons show up as tracks | Yes. 4 SS-N-2 missiles became Player tracks (class wp_ss-n-2) at t=78-88 s. |
| Track ids | Per-table allocated (7001...), separate from UniqueID. |
| Game starts paused | Yes (`paused=True` at t=0). |
| Neutral side | Its table held 8-13 contacts, more than either combatant; treat the Neutral table as unreliable/shared. |

Caveats: one scenario, one run, position matching used a 0.02 deg tolerance (approximate). `sigma` units not confirmed (looks like metres). Orders (Gate C) and the F10/AllowControlOfEnemyUnits path are still untested at runtime. Launching `Sea Power.exe` from `C:\Games` needed `steam_appid.txt` (1286220) or it restarted via Steam.

---
## Runtime results: Gate C (command smoke test), same Red Sea mission

| Check | Result |
|---|---|
| `setWaypointTask` on a Blue (Player) ship, Yorktown uid 8 | Works. Heading 145 -> 136 deg in 12 s, then continued turning; lat/lon track reversed course over ~120 s (slow cruiser turn, ~0.4-0.75 deg/s). |
| Same call on a Red (Enemy/AI) ship, Prj.159-A uid 30 | Works with `Globals.AllowControlOfEnemyUnits = False`. No F10 toggle required for plugin-issued orders. |
| `RemoveWaypoints()` + `setWaypointTask()` | Replaces the mission's queued tasks (Yorktown 2 -> 1, Petya 3 -> 1); `CommandPosition` becomes the new target. |
| AI override | None seen over 90 game-seconds of watch samples for either ship; the order persisted and `wpdist_mi` began falling for Petya at ~t=122 s. Longer-term behavior (self-defence retasking, attacks) untested. |
| Ownership validation (plugin) | Rejected `move 1 8` (Blue unit ordered as Red) and `move 0 99999` (missing unit). |
| CourseCommand hypothesis | Refuted: `courseCmd=none` on both ships. (Vessel steering does prefer `CourseCommand` over `CommandPosition`, Vessel.cs:915-926, so clear it if a unit ever has one.) |
| Units | `GetDistToCurrentActiveWaypointInMiles()` returns nautical miles. |
| Early mis-diagnosis | I first read "distance growing" as "ignored"; the ship was simply still turning ~170 deg. Judge steering by heading, not distance. |

Not verified: reaching the waypoint, aircraft/submarine orders, EMCON/speed/attack commands, behavior after the AI sees a threat, scene reload. Plugin fix (stale `commands.done.N.txt` retry loop) is in source but not rebuilt/deployed yet.

---
## Runtime results: Gate E (loopback bridge + external scripted commanders)

Setup: plugin `GHOSTFLEET/Bridge.cs` (HttpListener on 127.0.0.1:8765, all game access on the Unity main thread via a job queue) + `GHOSTFLEET/orchestrator.py` (stdlib Python; scripted `patrol` for Blue, `intercept` for Red; no LLM). Run log: `GHOSTFLEET/runs/run_20261113_233355.jsonl`.

| Check | Result |
|---|---|
| Side-scoped `/state` | Whitelisted keys only; own units + that side's own table tracks; passed `check_state` every window. |
| Orders over HTTP applied on main thread | Yes: 1 Blue + 6 Red move orders, HTTP 200, `applied_check` 1/1 and 6/6 (next state's waypoint equals the order). |
| Cross-side order (Red orders Blue unit 8) | REJECTED by ownership check. |
| Unknown JSON key | HTTP 400, whole batch rejected. |
| Pause / step / resume | 6 windows x 30 game-s, t = 8 -> 38 -> 68 -> ... -> 158 s, deterministic windows; pausing leaves HTTP responsive. |
| Time scale | Fixed: UnPause() resets speed, so the plugin re-applies the stored scale after every unpause. |

Bugs found on the way (all fixed): Unity `JsonUtility` returned null for the order batch (replaced by strict `MiniJson`, 15 offline tests pass); orchestrator mislabelled HTTP 400 as "accepted" (now fails fast); stale `commands.done.N.txt` retry loop in the file poller.

Not done: scenario reload from the bridge (Gate E asks for one reload; menu path is Main menu > Scenarios > Intro Scenarios > Red Sea Breakout > Play > briefing Play; no in-code entry point found yet), LLM commanders (Gate F), attack/aircraft/sub orders, longer AI-override test, fairness (Blue=Player vs Red=Enemy AI helpers).
