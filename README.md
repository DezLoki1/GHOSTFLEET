# GHOSTFLEET
Autonomous AI fleet command for Sea Power: Naval Combat in the Missile Age. Independent LLM commanders, asymmetric intelligence, and real-time tactical decision-making.

## Status (2026-11-14)

Working prototype. Two independent LLM commanders (Claude via the Claude Code CLI or the API, or scripted baselines) play one
Sea Power v0.9.2 scenario inside **one** game process. A BepInEx 5 plugin (`src/Plugin.cs`, `src/Bridge.cs`) exposes a loopback-only HTTP
bridge (`/state`, `/orders`, `/time`, `/truth`); `src/match.py` runs paused decision windows against it with per-side budgets and records
every window. Each side only receives its own units, its own weapons and the tracks from its own plotting table.

- Orders: `move`, `speed`, `emitters`, `weapons` (posture), and `engage` (fire at an identified-hostile track from your own picture).
- Run a match: `python src/match.py --side0 claudecli:sonnet@low --side1 claudecli:sonnet@low --windows 12 --step 300 --confirm-spend --max-usd 1.5`
- Read it back: `python src/replay.py runs/<match_dir> [--raw]`, `python src/summarize.py runs/<match_dir>`, `python src/audit.py runs/<match_dir>`
- Tests (offline, mock bridge): `python src/test_match.py`
- Results and findings: [docs/RESULTS_2026-11-14.md](docs/RESULTS_2026-11-14.md); evidence and open questions: [docs/GATES_A-D_EVIDENCE_2026-11-13.md](docs/GATES_A-D_EVIDENCE_2026-11-13.md)
- Build: `dotnet build -c Release -p:GameDir="<path to a BepInEx-enabled Sea Power copy>"`
- Not done: scenario reload from the bridge (a human loads the mission), tournaments/side swaps, spectator dashboard, a second model provider.

Requires a Sea Power install with BepInEx 5.4.x; no game files are included here.
