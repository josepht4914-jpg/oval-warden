# Oval Warden

A Python bot for the Mech Royale competition. The Dockerfile belongs at the root of the public submission repository. All gameplay code uses the Python standard library. The Python base image is pinned by digest.

## How it plays

The bot tracks opponents by the IDs, positions and velocities supplied by the server. It estimates travel time for the arm gun, anticipates the target's movement during wide torso turns, and uses actual torso orientation to decide when to fire. A charged laser uses a separate aim solution at release. The bot checks the line of fire against the supplied terrain and solid buildings, including when the server continues reporting an opponent behind cover.

Movement uses a ground route through the map and the mech's current velocity to brake near a destination. The bot seeks a second weapon and cubes, faces pursuers while moving independently, and looks for cover when multiple opponents have a shot. Ground navigation is primary. Jump and jet recovery helps with obstructions, and the bot can skip loot it cannot reach.

House hunters can fight from about 150 m with a hitscan laser, so the bot fights near cover. Whenever an opponent can see it, it scores nearby standing points inside the zone by which opponents could hit any part of its body there, and shoots from points with a hidden spot a short dash away. While an enemy laser is ready, that spot must be within 5 m; for 1.65 s after a beam it may be farther. When a laser starts charging (visible for about 0.75 s), the bot runs to the hidden spot and stays there until the shot is released. Exploration runs along buildings, and the torso turns through a full look around every few seconds because house bots share vision.

After a kill the bot walks to where the hunter fell to collect its cubes and its laser, even when that spot is out of view, and it will break off a fight to take a laser lying close by. Cube collecting stops at ten, where the HP and damage bonus is capped. The cover search has a hard time limit so a crowded fight cannot push an answer past the 50 ms budget.

The gun stays; the second slot prefers a laser, then lightning. A mortar is never taken, because lasers do +60% damage to a mech carrying one. A low-health mech that breaks contact stays in cover for repairs while the zone permits. An uncontested supply drop close to completion can provide a faster full repair.

## Test on your computer

From the repository root, run these commands with Python 3.9 or later.

```sh
python3 -B -m unittest discover -s tests -v
python3 kit/check/check.py -- python3 -B -u bot.py
```

The first command checks specific aiming, cover, movement, and loot decisions in controlled scenes. The second runs the organizer's checker on 300 recorded observations from a real match. The replay tests protocol and response timing. Placement comes from an official scored run.

## Verify the submission image

With Docker running, use the included verification script.

```sh
sh verify.sh
```

GitHub Actions runs the Linux/amd64 image check on push and supports manual dispatch. In the repository's **Actions** tab, open **Mechfight container check** to see the result. The organizer's checker drives the same image entrypoint that plays matches, with one CPU, 512 MB memory, user 65534, disabled networking, a read-only filesystem, a read-only arena mount and a 64 MB temporary directory.

Publish this folder's contents at the root of the public repository. Keep the hidden `.github` folder to enable the check. Submit the repository URL on the competition page after the workflow passes.

## Aiming references

The projectile calculation uses the time for a moving game target and a virtual projectile to reach the same point. Target selection uses a small preference for continuing to track a suitable target across updates. The scored game supplies these updates directly every 0.1 seconds of simulation time. [Scott Lembcke on projectile lead](https://www.gamedeveloper.com/programming/shooting-a-moving-target) · [Target tracking and switching in an aim-assist implementation](https://github.com/fgilde/AI-Ming/blob/main/docs/features/aim-assist.md)
