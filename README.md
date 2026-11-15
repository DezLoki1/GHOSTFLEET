# GHOSTFLEET
Autonomous AI fleet command for Sea Power: Naval Combat in the Missile Age. Independent LLM commanders, asymmetric intelligence, and real-time tactical decision-making.

## Status (2026-11-14, later)

Working prototype on two missions (Red Sea Breakout, Gulf of Sidra). Two independent LLM commanders (Claude via the Claude Code CLI or the API, or scripted
baselines) play one Sea Power v0.9.2 mission inside **one** game process. A BepInEx 5 plugin (`src/Plugin.cs`, `src/Bridge.cs`) exposes a loopback-only HTTP bridge
(`/state`, `/orders`, `/time`, `/truth`); `src/match.py` runs paused decision windows with per-side budgets and records everything. Each side only receives its own
units, weapons, air wing and the tracks from its own plotting table (with the game's own hostile/friendly/neutral relation for each track).

- Orders: `move`, `speed`, `emitters`, `weapons` (posture), `engage` (fire at a hostile track from your own picture, optionally naming the weapon), `launch` (queue aircraft from a carrier deck), `rtb`.
- Commanders may also ask for how long to run before their next decision (`next_check_s`); the mission budget is game time, not window count.
- Scenario files come from the mission itself: `python src/gen_scenario.py "<mission .ini>"`. The winner is the game's own verdict (`/truth` `mission_status`).
- Run a match: `python src/match.py --scenario src/scenarios/action_in_the_gulf_of_sidra_1986.json --side0 claudecli:sonnet@low --side1 claudecli:sonnet@low --confirm-spend --max-usd 4`
- Every match writes `report.txt` (summary, per-window assessments, notes, orders, refusals, raw model replies) into its run folder. Older runs: `python src/report.py runs/<dir>`. Other tools: `replay.py`, `summarize.py`, `audit.py`, `timeline.py`, `unit_trace.py`.
- Tests (offline, mock bridge): `python src/test_match.py`
- Results and findings: [docs/RESULTS_2026-11-14.md](docs/RESULTS_2026-11-14.md) (example reports: [run 4](docs/examples/sidra_run4_report.txt), [Sonnet-vs-Haiku batch](docs/examples/sidra_batch_sonnet_haiku_batch_report.txt)); evidence and open questions: [docs/GATES_A-D_EVIDENCE_2026-11-13.md](docs/GATES_A-D_EVIDENCE_2026-11-13.md)
- Build: `dotnet build -c Release -p:GameDir="<path to a BepInEx-enabled Sea Power copy>"`
- Unattended batches: `python src/batch.py --n 2 --a claudecli:sonnet@low --b claudecli:haiku@low --swap --label L --max-total-usd 7 -- --max-usd 3 --scale 4` restarts the mission (`POST /mission?cmd=restart|play`), runs each match, swaps sides, and writes `batch_report.txt`. The game must be running with a mission loaded once. A Start-mission popup can still appear and is stepped through, not closed.
- Not done: closing popups from the plugin, aggregate tournaments over many models and missions, a spectator view, a second model provider, submarine orders (depth, sonar), formations, kill attribution (the game keeps no killer field), and a third mission to prove the generator on a different kind of mission.

Requires a Sea Power install with BepInEx 5.4.x; no game files are included here.
