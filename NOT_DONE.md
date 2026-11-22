# Status and what is not done yet

Last updated: Sun 22 Nov, 2:40 AM CST. Cutoff: 10:00 AM CST.

## In this folder now (version "team merge")

Changed files since the 4.2 submission: `bot.py`, `combat.py`, `world.py`, `tests/test_decisions.py`.
Checked here: 32 unit tests pass, the organizer's `check.py` passes, worst decision time 13 ms (limit 50 ms).
NOT yet scored on the real leaderboard and NOT yet pushed to GitHub.

Simulator result, 48 practice-style runs per squad schedule (imitation hunters built from 20+ recorded real matches):

| | 4.2 bot | this version |
|---|---|---|
| Kills per run, schedule A | 1.94 | 2.46 |
| Kills per run, schedule B | 3.23 | 3.56 |
| Runs that ended still gun-only | 18 of 48 | 5-6 of 48 |

The kill gain is about one standard error, so it is not proven. The clear, repeatable effect is that the bot arms itself much sooner.

### What is in it
- Loot memory: remembers every weapon, crate and cube it has seen.
- Weapon rush: looks around at spawn and runs (boosting) to the nearest laser or lightning within 170 m.
- Supply drops: leaves in time, circles inside the ring, does not abandon a capture.
- A visible mech always outranks crates and drones as a target; a mech below 35% HP is finished before switching.
- Duels: prefers 85-115 m, swings side to side at its firing spot with random reversals, and now does so on the boost in bursts (keeps 45 energy in reserve).
- Faces attackers it cannot see when they do real damage (rear hits do 1.5x).
- Cover test includes the head; no jet hop while under fire; sidesteps incoming mortar shells.
- Gives up on a cube or weapon it cannot collect after a time allowance (no more standing beside it).
- Turns the torso through full circles whenever no enemy is known.
- No 40-55 ms stall every second cube.

### Built but switched OFF (no gain, or a loss, in the simulator)
Each is one constant near the top of `bot.py`:
- `HIDE_RANGE` (stop hiding from laser charges beyond 60 m): lost kills.
- `GATE`, `OPEN_DUEL` (leave cover to fix the range / strafe in the open): lost kills.
- `FALL_BACK`, `RESET_HP`, `REPAIR_LOW = .55` (keep backing away while hidden, heal after each kill): no gain, possibly a small loss.
- `AMBUSH` (wait in cover with lightning): lost in the first simulator.

## Requested, not done
- Rooftops / high ground by jumping and jetting. Not built. In 20 recorded matches mechs on roofs took the same damage as on the ground (26 vs 27 HP/s), expert bots were hit more often while airborne (gun 56% vs 45%), and the simulator cannot test jumping. A quarter of weapons and crates are on roofs, so rooftop looting is the one version of this that might pay.
- Blind fire through trees at sounds. The bot already shoots a seen target through trees (its line-of-fire check ignores trees, and the game reports a mech's position for 5 s after it goes out of view). It does not shoot at a sound alone.

## Known limits
- The simulator has no zone, crates, drones, supply drops or jumping, and the easy / medium / hard practice bots were not in the recordings, so their strength is an estimate. Supply-drop, crate and zone-centre changes are covered by unit tests only.
- The weapon rush can run up to 170 m under fire, and can walk to a weapon another bot already took.
- Timing was measured on this PC; the match container is slower.

## Ideas not tried
- Rooftop looting for the first weapon.
- A second look at fighting range once real scores are in (the range changes are present but mostly off).
- A survival-first variant for the final free-for-all (the final is 16 teams' bots, not the practice squad).
- More recorded matches to model the house bots' squad behaviour, which the public rooms do not show.
