# GHOSTFLEET
Autonomous AI fleet command for Sea Power: Naval Combat in the Missile Age. Independent LLM commanders, asymmetric intelligence, and real-time tactical decision-making.

## Status (2026-11-13)

Early prototype. Two independent commanders can be driven inside **one** Sea Power v0.9.2 process:
a BepInEx 5 plugin (`src/Plugin.cs`, `src/Bridge.cs`) exposes a loopback-only HTTP bridge
(`/state`, `/orders`, `/time`) and `src/orchestrator.py` runs scripted (non-LLM) commanders against it.
Each side only receives its own units plus the tracks from its own plotting table.

- Evidence and open questions: [docs/GATES_A-D_EVIDENCE_2026-11-13.md](docs/GATES_A-D_EVIDENCE_2026-11-13.md)
- Build: `dotnet build -c Release -p:GameDir="<path to a BepInEx-enabled Sea Power copy>"`
- Not done: scenario reload from the bridge, LLM commanders, attack/aircraft/sub orders.

Requires a Sea Power install with BepInEx 5.4.x; no game files are included here.
