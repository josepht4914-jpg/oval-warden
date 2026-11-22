"""Oval Warden, a placement-focused mechfight/1 bot."""
import math
import random
import time

import mechfight
from combat import Combat
from world import World


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def angle_to(a, b):
    return math.atan2(b[1] - a[1], b[0] - a[0])


def point_at(pos, angle, length):
    return pos[0] + math.cos(angle) * length, pos[1] + math.sin(angle) * length


def with_height(pos, height):
    return pos[0], pos[1], pos[2] + height


import os  # noqa: E402  (experiment knob for the duel simulator; unset in matches)
# Loadout: the gun stays, the second slot prefers a laser, then lightning; a mortar is never taken.
# (Duel simulations: gun + laser beat gun + lightning and laser + lightning against stand-off hunters.)
LOADOUT = "laser:3,lightning:2,mortar:0,gun:99"
GUN_ENGAGE = float(os.environ.get("OW_GUN_ENGAGE", "1e9"))
# Weapon preference. Mortar 0: never picked up (lasers do +60% to its carrier). A held weapon is
# replaced by a pickup worth more; the gun's value decides whether it is ever given up.
WEAPON_VALUE = {k: float(v) for k, v in (item.split(":") for item in os.environ.get(
    "OW_WEAPONS", LOADOUT).split(","))}
# Wall-clock time the cover search may use per decision (the server allows 50 ms in total).
PLAN_BUDGET_S = 0.008
# Tactical constants (environment overrides exist only for simulator experiments).
REPAIR_LOW = float(os.environ.get("OW_REPAIR_LOW", ".46"))     # start hiding to repair below this HP share
REPAIR_HIGH = float(os.environ.get("OW_REPAIR_HIGH", ".85"))   # resume fighting above this HP share
ESC_READY = float(os.environ.get("OW_ESC_READY", "5"))          # max dash to cover while an enemy laser is ready
ESC_COOL = float(os.environ.get("OW_ESC_COOL", "8.5"))          # ... while every enemy laser is recharging
EXTRA_EXPOSE = float(os.environ.get("OW_EXTRA_EXPOSE", "10"))   # peek penalty per extra enemy with a shot
LOS_THREATS = int(os.environ.get("OW_LOS_THREATS", "3"))        # enemies checked in the cover search
GRAB_LASER = float(os.environ.get("OW_GRAB", "100"))  # run for a laser this close even mid-fight
GRAB_OTHER = 30.0
# Weapon rush: while the second slot is empty, a remembered weapon this close outranks duel and retreat
# (two-weapon mechs beat gun-only mechs 44:1 in recorded duels), unless a mech is close with a shot.
RUSH_REACH = float(os.environ.get("OW_RUSH", "170"))
RUSH_LASER_EXTRA = 40.0   # walk this much farther for a laser than for the nearest other weapon
RUSH_CLOSE = 60.0         # a mech this close with line of sight puts the rush back to the mid-fight limits
SWEEP = math.tau - math.radians(110)   # torso rotation that shows every direction once through a 120 degree wedge
FORGET_RANGE = 40.0       # inside the 45 m proximity sensors: an item not reported here with a clear line is gone
DROP_REACH = 250.0
DASH = os.environ.get("OW_DASH", "1") == "1"   # strafe on the boost in duels
BRAWL_REACH = 14.0     # start a lightning fight when a visible hunter is this far beyond arc range
BRAWL_ENERGY = 30.0    # energy needed to start one
AMBUSH = os.environ.get("OW_AMBUSH", "0") == "1"   # wait in cover for hunters (lost to peeking in simulation)
BRAWL = os.environ.get("OW_BRAWL", "1") == "1"
AMBUSH_LIMIT = 22.0    # longest unbroken wait before trading shots at range again
AMBUSH_WAIT = 6.0      # seconds to keep waiting in cover after losing track of a hunter
CUBE_GOAL = 10         # cubes stop adding HP and damage here and only make the mech bigger
USE_GHOSTS = os.environ.get("OW_GHOSTS", "0") == "1"            # treat unexplained sounds as unseen threats
PRECHARGE = os.environ.get("OW_PRECHARGE", "0") == "1"
# Duel shape. Measured on recorded matches: one house expert deals ~150 HP/s under 50 m, ~75 at 75 m
# and ~43 at 105-140 m; half of that is its laser, which cannot hit what it cannot see.
DUEL_NEAR = float(os.environ.get("OW_DUEL_NEAR", "85"))    # preferred duel band against gun / laser / mortar
DUEL_FAR = float(os.environ.get("OW_DUEL_FAR", "115"))
DUEL_MIN = float(os.environ.get("OW_DUEL_MIN", "55"))      # never fight a gun / laser / lightning holder closer
# Hiding from enemy laser charges. A charge from farther than HIDE_RANGE is "distant": it is dodged only
# when cover is within DODGE_REACH, unless we are below HIDE_HP or HIDE_COUNT mechs charge at once.
# 1e9 = hide from every charge as the 4.2 bot did. In the campaign simulator hiding less (60) lost
# about 0.9 kills on schedule B, so it stays off until someone shows otherwise.
HIDE_RANGE = float(os.environ.get("OW_HIDE_RANGE", "1e9"))
HIDE_HP = float(os.environ.get("OW_HIDE_HP", ".35"))
HIDE_COUNT = int(os.environ.get("OW_HIDE_COUNT", "3"))
DODGE_REACH = float(os.environ.get("OW_DODGE_REACH", "7.5"))
ZAP_RUSH = os.environ.get("OW_ZAP_RUSH", "1") == "1"       # holding lightning: close on a mech that has none
PEEK = os.environ.get("OW_PEEK", "1") == "1"               # duel from beside cover when there is some in the band
WEAVE = os.environ.get("OW_WEAVE", "1") == "1"             # swing either side of the firing position
# 1 = when farther than DUEL_FAR + 25 m or closer than DUEL_MIN, leave cover and fix the range in the open
# first. Campaign simulator, schedule B, 48 trials: 2.44 kills with 1, 3.21 with 0 (the 4.2 bot: 3.23).
GATE = os.environ.get("OW_GATE", "0") == "1"
# No firing position beside cover: 1 = strafe in the open, 0 = get behind something first (the 4.2 bot).
OPEN_DUEL = os.environ.get("OW_OPEN", "0") == "1"
# Starting a laser charge on the way to cover (half speed): 0 = allowed, 1 = not below HIDE_HP, 2 = never.
NOCHARGE = int(os.environ.get("OW_NOCHARGE", "1"))
STALK = os.environ.get("OW_STALK", "0") == "1"                  # close on unseen laser carriers through cover          # charge our laser while hiding from a held charge
# Awareness (rear armour takes x1.5; our view is a 120 degree wedge plus 45 m all round).
FACE = os.environ.get("OW_FACE", "1") == "1"             # turn the torso to attackers behind us
FACE_S = 0.8            # one look at an attacker we cannot see: long enough to swing it into the view wedge
FACE_WINDOW = 2.5       # seconds of damage reports that decide who is hurting us
FACE_AGAIN = 5.0        # while shooting a mech, look away from it at most once per this many seconds
FACE_REAL = float(os.environ.get("OW_FACE_REAL", "90"))   # damage in the window that is worth a look (two gun hits)
FACE_BISECT = os.environ.get("OW_FACE_BISECT", "1") == "1"   # running for cover: face between attackers, not the target
FACE_SWITCH = os.environ.get("OW_FACE_SWITCH", "1") == "1"   # fighting: turn to a tracked mech that hurts more
FRONT_ARC = math.radians(80)   # attackers within this of our aim hit front armour (the arc is 90 a side)
FINISH_HP = 0.25        # a visible target below this HP share is finished before we turn to anyone else
FALL_BACK = os.environ.get("OW_FALL_BACK", "0") == "1"   # keep backing away through cover while hidden
CHASE_S = 8.0           # a hunter seen this recently is assumed to be walking in on us
CHASE_RANGE = 130.0     # ... if it was last seen within this distance
RESET_HP = float(os.environ.get("OW_RESET_HP", "0"))   # after a kill below this HP share, heal before the next duel
# Hard reset: also run from a hunter that has found us until HP is back. Off: the campaign simulator
# showed no difference in kills either way (B 3.96 hard, 4.00 soft, se 0.65), and running from a
# hunter that sees us did not raise HP at the next contact.
RESET_HARD = os.environ.get("OW_RESET_HARD", "0") == "1"
HEAD = os.environ.get("OW_HEAD", "1") == "1"             # the cover test also samples the top of our body


class Bot:
    name = "Oval Warden"

    def __init__(self, hello):
        self.config = hello["config"]
        self.world = World(hello)
        self.combat = Combat(hello)
        self.side = 1 if hello["slot"] % 2 else -1
        self.heading = (hello["seed"] % 360) * math.pi / 180
        self.goal = None
        self.mode = "explore"
        self.plan_at = -1
        self.hide_until = -1
        self.repairing = False
        self.last_bearing = None
        self.listen_until = -1
        self.plan_key = None
        self.last_anchor = None
        self.stuck_since = None
        self.jet_until = -1
        self.next_jump = 0
        self.last_explore = None
        self.unreachable_loot = {}
        self.chase_key, self.chase_until = None, 0.0
        self.dodge_until = -1
        self.dodge_vector = None
        self.zone_phase = {}
        self.laser_cool_until = {}
        self.was_charging = {}
        self.deadline = self.hard_deadline = math.inf
        self.hide_spot, self.hide_at = None, -99.0
        self.ghosts = []
        self.zap_range = 0.0      # lightning reach while there is energy to fire it, else 0
        self.has_zap = False
        self.seen_at, self.seen_pos = -99.0, None
        self.ambush_since, self.ambush_seen, self.no_ambush_until = None, -99.0, -99.0
        self.last_pos = {}        # mech id -> (position, had a laser) when last reported
        self.kill_sites = []      # [position, expiry, laser dropped]: where our kills left cubes and weapons
        self.memory = {}          # loot id -> dict(id, what="pickups"|"crates"|"cubes", kind, pos): everything seen
        self.forget_turn = 0
        self.sweep_left, self.sweep_yaw = SWEEP, None   # opening look-around still to turn (radians)
        self.hurt_at = -99.0
        self.cruise = False       # boosting between loot (hysteresis on energy)
        self.hop_origin = None    # where the last explore hop was chosen
        self.rng = random.Random(hello["seed"])
        self.strafe = self.side   # which way we cross the enemy's line of fire in a duel
        self.strafe_until = -1.0  # next random reversal
        self.dash = False         # strafing on the boost right now (hysteresis on energy)
        self.duel_close = False   # inside the minimum duel range: boost back out
        self.release_hold = 0     # decisions spent lining up a laser shot that must be let go early
        self.soft_ids = frozenset()   # distant charging mechs: dodged only when cover is a step away
        self.soft_dodge = False       # the current evade / cover is such a dodge (keep our own charge)
        self.rear_mult = self.config["mech"].get("rear_mult", 1.5)
        self.hurts = []           # (time, attacker id, bearing, damage before the rear multiplier, rear)
        self.hurt_at = -99.0      # last time a mech damaged us
        self.sighted = {}         # mech id -> (time, position, cubes) when last reported
        self.glances = {}         # attacker id -> when we last started turning to it
        self.fall_spot, self.fall_at = None, -99.0
        self.dead_ends = []       # (cover spot we got stuck running to, expiry)
        self.stuck_first = None   # when the current run of stuck periods began
        self.resetting = False    # healing out of sight after a kill
        self.facing = None        # attacker id(s) the torso is turned to guard against (diagnostics)

    def listen(self, obs):
        now, me = obs["t"], obs["self"]
        for m in obs["mechs"]:
            if m.get("kind") == "mech":
                self.sighted[m["id"]] = (now, tuple(m["pos"]), m.get("cubes", 0))
        for e in obs["events"]:
            if e["type"] == "kill":
                self.sighted.pop(e.get("target"), None)
                self.hurts = [h for h in self.hurts if h[1] != e.get("target")]
                if me["hp"] < RESET_HP * me["max_hp"]:
                    # A duel started with more HP is won 91:30: heal out of sight before the next hunter.
                    self.resetting = True
                    self.repairing = self.repairing or RESET_HARD
            elif (e["type"] == "damaged" and e.get("bearing") is not None and e.get("by") is not None
                  and e.get("weapon") in ("gun", "laser", "lightning")):
                # A mortar's bearing points at the blast, not the mech; drones and the zone are not faced.
                rear = bool(e.get("rear"))
                self.hurts.append((now, e["by"], e["bearing"],
                                   e.get("amount", 0) / (self.rear_mult if rear else 1), rear))
                self.hurt_at = now
        if me["hp"] >= RESET_HP * me["max_hp"]:
            self.resetting = False
        self.hurts = [h for h in self.hurts if now - h[0] < FACE_WINDOW]
        self.dead_ends = [d for d in self.dead_ends if d[1] > now]
        reports = [e for e in obs["events"] if e["type"] == "damaged" and e.get("bearing") is not None]
        if reports:
            self.last_bearing = reports[-1]["bearing"]
            self.listen_until = now + 2.0
            return
        sounds = [s for s in obs["sounds"] if s["dist"] != "far" and s["kind"] in
                  ("gun", "laser", "laser_charge", "lightning", "mortar", "boost", "step")]
        if sounds:
            sound = min(sounds, key=lambda s: (s["dist"] != "near", s["age"]))
            if self.listen_until <= now or sound["dist"] == "near":
                self.last_bearing = sound["bearing"]
                self.listen_until = now + 0.9

    def guard_aim(self, obs, aim, shooting, fighting):
        """Yaw to face instead of `aim` so that the mechs hurting us stop hitting rear armour, or None.

        aim is where the torso would point otherwise. shooting: combat is aiming along it at a mech.
        fighting: and we mean to win that exchange (not running for cover), so turning away costs
        our own damage and is done briefly and only for cause."""
        me, now = obs["self"], obs["t"]
        pos = me["pos"]
        self.facing = None
        if not FACE or not self.hurts:
            return None
        focus = self.combat.focus
        target = focus if focus and focus.get("kind") == "mech" else None
        if shooting and target and target.get("hp", math.inf) < FINISH_HP * target.get("max_hp", 1400):
            return None  # nearly dead and in our sights: its death removes an attacker for good
        tracked = {m["id"]: m for m in obs["mechs"] if m.get("kind") == "mech"}
        weight, bearing, last = {}, {}, {}
        for t, by, heard, amount, _ in self.hurts:
            weight[by] = weight.get(by, 0) + amount
            bearing[by], last[by] = heard, t
        for i in bearing:
            if i in tracked:
                bearing[i] = angle_to(pos, tracked[i]["pos"])
        behind = [i for i in weight if abs(math.remainder(bearing[i] - aim[0], math.tau)) > FRONT_ARC]
        if not behind:
            return None
        ours = weight.get(target["id"], 0) if shooting and target else 0
        culprit = max(behind, key=lambda i: (i not in tracked, weight[i]))
        if culprit not in tracked:
            # Not in view: look along the reported bearing. Once seen it is tracked for 5 s, so the
            # cover search and the target choice know about it even after we turn back.
            if now - last[culprit] >= FACE_S:
                return None
            if shooting:
                # Looking away stops our own fire for about a second and shows our back to the
                # target: only for real damage, and no less than the target itself is doing.
                if weight[culprit] < max(FACE_REAL, ours):
                    return None
                if now - self.glances.get(culprit, -99) > FACE_AGAIN:
                    self.glances[culprit] = now
                if now - self.glances[culprit] >= FACE_S and weight[culprit] < 2 * ours + FACE_REAL:
                    return None  # looked once and did not find it
            self.facing = culprit
            return bearing[culprit]
        if not fighting and (FACE_BISECT or not shooting):
            # Running or hiding: one facing that puts every attacker in front, if there is one.
            yaws = sorted(bearing.values())
            gap, k = max(((yaws[(j + 1) % len(yaws)] - yaws[j]) % math.tau if len(yaws) > 1 else math.tau, j)
                         for j in range(len(yaws)))
            spread = math.tau - gap
            if spread < 2 * FRONT_ARC:
                self.facing = tuple(sorted(weight))
                return math.remainder(yaws[(k + 1) % len(yaws)] + spread / 2, math.tau)
            self.facing = max(weight, key=weight.get)
            return bearing[self.facing]
        # Shooting at a mech while a tracked one hits our back: turn only if it clearly hurts more,
        # and hand it to combat as the target (its stickiness then keeps the torso there).
        if not fighting or not FACE_SWITCH or weight[culprit] <= 1.3 * ours + 40:
            return None
        if now - self.glances.get(culprit, -99) > FACE_AGAIN:
            self.glances[culprit] = now
        if now - self.glances[culprit] >= FACE_S:
            return None
        self.combat.focus = tracked[culprit]
        self.facing = culprit
        return bearing[culprit]

    def hunters(self, obs, threats):
        """Mechs to stay away from: tracked ones, plus those seen in the last CHASE_S seconds at
        their last known position (a hunter that loses sight of us walks in), nearest first."""
        now, pos = obs["t"], obs["self"]["pos"]
        known = {m["id"] for m in threats}
        out = list(threats)
        for i, (t, where, cubes) in self.sighted.items():
            if i not in known and now - t < CHASE_S:
                out.append(dict(id=i, kind="mech", pos=list(where), cubes=cubes, weapons=()))
        out = [m for m in out if distance(pos, m["pos"]) < CHASE_RANGE]
        out.sort(key=lambda m: distance(pos, m["pos"]))
        return out[:LOS_THREATS]

    FALL_RINGS = ((23, 12, math.pi / 12), (41, 16, 0), (11, 12, 0), (68, 16, math.pi / 16))

    def fall_back(self, obs, chasers):
        """While out of sight: a hidden point farther from the hunters, or None to stay put.

        75% of lethal damage is dealt inside 75 m and regeneration does not need us to stand
        still, so waiting where the hunter last saw us only lets it choose the range."""
        if not FALL_BACK or not chasers:
            return None
        pos, now = obs["self"]["pos"], obs["t"]
        if (self.fall_spot is not None and now - self.fall_at < 2.0 and distance(pos, self.fall_spot) > 3
                and not self.dead_end(self.fall_spot) and self.still_hidden(obs, chasers, self.fall_spot)):
            return self.fall_spot
        here = min(distance(pos, m["pos"]) for m in chasers)
        next_center, next_radius = obs["zone"]["next_center"], obs["zone"]["next_radius"]
        # The full search costs 7 ms (median, three hunters) to 40 ms: give it half the usual budget,
        # most useful rings first, because the walk to a far point can still need a route search.
        budget = self.deadline, self.hard_deadline
        self.deadline, self.hard_deadline = budget[0] - .004, budget[1] - .005
        try:
            points = self.cover_points(obs, chasers, self.FALL_RINGS)
        finally:
            self.deadline, self.hard_deadline = budget
        ranked = []
        for p, _, any_part, ring in points:
            if any_part or self.dead_end(p):
                continue
            gain = min(distance(p, m["pos"]) for m in chasers) - here
            score = gain - .12 * ring - .6 * max(0, distance(p, next_center) - next_radius + 8)
            if score > 3:
                ranked.append((score, p))
        ranked.sort(reverse=True)
        for _, p in ranked[:4]:
            # The way there should not cross a line of fire either (checked at the middle).
            middle = ((pos[0] + p[0]) / 2, (pos[1] + p[1]) / 2)
            if time.perf_counter() > self.hard_deadline or self.still_hidden(obs, chasers, middle):
                self.fall_spot, self.fall_at = tuple(p), now
                return self.fall_spot
        self.fall_spot = None
        return None

    def dead_end(self, spot):
        return any(distance(spot, p) < 5 for p, _ in self.dead_ends)

    def rest(self, obs, threats):
        """Out of every tracked mech's sight and regenerating: set mode and goal.

        Returns False when nobody is near and we are only topping up after a kill: then healing
        happens on the move (loot, explore), since regeneration does not need us to stand still."""
        me = obs["self"]
        pos = me["pos"]
        hurt = me["hp"] < REPAIR_LOW * me["max_hp"]
        held = {w["kind"] for w in me["weapons"] if w}
        zone_center, zone_radius = self.live_zone(obs)
        for site, _, has_laser in self.kill_sites:
            # What our kill dropped comes first: each cube is +4% HP on the spot.
            d = distance(pos, site)
            if (5 < d <= (45 if hurt else 90) and abs(site[2] - pos[2]) <= 4
                    and (me["cubes"] < CUBE_GOAL or (has_laser and "laser" not in held))
                    and tuple(site[:2]) not in self.unreachable_loot
                    and distance(site, zone_center) <= zone_radius - 2):
                self.mode, self.goal = "loot", tuple(site[:2])
                return True
        chasers = self.hunters(obs, threats)
        away = self.fall_back(obs, chasers)
        if away is None and not (hurt or threats or chasers):
            return False
        self.mode, self.goal = "repair", away if away is not None else tuple(pos[:2])
        return True

    def live_zone(self, obs):
        """The circle that hurts right now: (center, radius).

        protocol.md does not say whether zone.center/radius are re-reported while the circle
        shrinks or stay at the phase's starting circle. Handle both: if the reported circle has
        not changed since this shrink began, interpolate it ourselves."""
        zone, now = obs["zone"], obs["t"]
        center, radius = tuple(zone["center"]), zone["radius"]
        start, end = zone["shrink_start"], zone["shrink_end"]
        key = (start, end)
        if self.zone_phase.get("key") != key:
            self.zone_phase = {"key": key, "first": (center, radius), "moved": False}
        elif (center, radius) != self.zone_phase["first"]:
            self.zone_phase["moved"] = True
        if self.zone_phase["moved"] or not start < now < end:
            return center, radius
        f = (now - start) / max(1e-6, end - start)
        nc, nr = zone["next_center"], zone["next_radius"]
        return ((center[0] + (nc[0] - center[0]) * f, center[1] + (nc[1] - center[1]) * f),
                radius + (nr - radius) * f)

    def urgent_zone(self, obs):
        me, zone = obs["self"], obs["zone"]
        pos = me["pos"]
        outside_next = distance(pos, zone["next_center"]) - max(0, zone["next_radius"] - 8)
        if outside_next <= 0:
            return False
        center, radius = self.live_zone(obs)
        margin = radius - distance(pos, center)
        travel_time = outside_next / max(1, me["stats"]["walk_speed"]) * 1.6
        return (not me["in_zone"] or
                (obs["t"] >= zone["shrink_start"] and margin < 12) or
                zone["shrink_end"] - obs["t"] < travel_time + 8)

    @staticmethod
    def zone_goal(obs):
        pos, zone = obs["self"]["pos"], obs["zone"]
        center, radius = zone["next_center"], zone["next_radius"]
        # Enter well inside the safe boundary. Large circles leave room to use perimeter cover.
        return point_at(center, angle_to(center, pos), min(distance(pos, center), 0.67 * radius))

    def remember(self, obs):
        """Keep every pickup, crate and cube ever seen (only what is in view is reported).

        An entry is dropped when we took it, when a take says it is gone, or when its spot is inside
        the proximity sensors with a clear line and the server no longer lists it."""
        me, now = obs["self"], obs["t"]
        pos = me["pos"]
        listed = set()
        for what in ("pickups", "crates", "cubes"):
            for obj in obs.get(what) or ():
                listed.add(obj["id"])
                entry = self.memory.get(obj["id"])
                if entry is None:
                    self.memory[obj["id"]] = dict(id=obj["id"], what=what, kind=obj.get("kind"), pos=obj["pos"])
                    if what == "pickups":
                        self.plan_at = now   # a weapon came into view: decide again at once
                else:
                    entry["pos"], entry["kind"] = obj["pos"], obj.get("kind")
        for event in obs["events"]:
            if event["type"] == "took" or (event["type"] == "take_failed" and event.get("reason") == "gone"):
                self.memory.pop(event.get("id"), None)
            elif event["type"] == "damaged":
                self.hurt_at = now
        eye = with_height(pos, me["stats"]["eye_height"])
        nearby = [e for e in self.memory.values() if e["id"] not in listed
                  and abs(e["pos"][0] - pos[0]) <= FORGET_RANGE and abs(e["pos"][1] - pos[1]) <= FORGET_RANGE
                  and distance(pos, e["pos"]) <= FORGET_RANGE]
        # A handful of sight lines per decision; the rest wait for the next one.
        self.forget_turn += 1
        for k in range(min(8, len(nearby))):
            entry = nearby[(self.forget_turn + k) % len(nearby)]
            p = entry["pos"]
            if ((distance(pos, p) < 3 and abs(p[2] - pos[2]) < 4)
                    or self.world.clear(eye, (p[0], p[1], p[2] + 1.0))):
                self.memory.pop(entry["id"], None)

    def wanted_weapon(self, me, kind):
        _, held_value = self.weapon_slot(me)
        return WEAPON_VALUE.get(kind, 0) > held_value and all(not w or w["kind"] != kind for w in me["weapons"])

    def on_roof(self, p, pos):
        """A spot more than 4 m above the ground that is not on our own level."""
        return p[2] > self.world.ground(p[0], p[1]) + 4 and abs(p[2] - pos[2]) > 4

    @staticmethod
    def our_ring(obs):
        """The landed supply drop whose ring we stand in with nobody else holding it, or None."""
        me = obs["self"]
        for drop in obs.get("drops") or ():
            if (drop["lands_in"] <= 0 and drop.get("holder") in (None, me["id"])
                    and distance(me["pos"], drop["pos"]) <= drop["radius"] - 1):
                return drop
        return None

    def ring_point(self, pos, drop):
        """Keep walking a small circle inside the ring: a standing mech is the easiest target there is."""
        angle = angle_to(drop["pos"], pos) + self.side * .9
        return point_at(drop["pos"], angle, min(4.0, .5 * drop["radius"]))

    def loot_options(self, obs):
        me, zone = obs["self"], obs["zone"]
        pos, radius = me["pos"], me["stats"]["radius"]
        ring = self.our_ring(obs)
        if ring is not None:
            # A capture that is running (full repair, full energy, 3 cubes) comes before any other loot.
            return self.ring_point(pos, ring)
        options = []
        zone_center, zone_radius = self.live_zone(obs)
        self.unreachable_loot = {p: expiry for p, expiry in self.unreachable_loot.items() if expiry > obs["t"]}
        def consider(obj, value, reach):
            p = obj["pos"]
            if tuple(p[:2]) in self.unreachable_loot:
                return
            dist = distance(pos, p)
            if dist > reach or distance(p, zone_center) > zone_radius - 2:
                return
            ground = self.world.ground(p[0], p[1])
            if p[2] > ground + 4 and abs(p[2] - pos[2]) > 4:
                return
            if dist > 4 and p[2] <= ground + 4 and not self.world.is_walkable(p[0], p[1], radius):
                return
            score = value - 0.34 * dist
            options.append((score, p))
        _, held_value = self.weapon_slot(me)
        held = {w["kind"] for w in me["weapons"] if w}
        unarmed = len(held) < 2
        for item in self.memory.values():
            what = item["what"]
            if what == "cubes":
                if me["cubes"] < CUBE_GOAL:
                    consider(item, 47, 90)
            elif what == "pickups":
                kind = item["kind"]
                if WEAPON_VALUE.get(kind, 0) > held_value and kind not in held:
                    best = kind == "laser"
                    consider(item, (110 if best else 50) + WEAPON_VALUE[kind], 190 if best else 105)
            elif unarmed:
                # No second weapon known: a crate drops one half the time.
                consider(item, 46, 150)
            elif me["cubes"] < CUBE_GOAL:
                consider(item, 24, 70)
        for site, _, has_laser in self.kill_sites:
            # A kill leaves its cubes plus two, and every weapon but the gun, often out of sight.
            wanted = (has_laser and "laser" not in held) or me["cubes"] < CUBE_GOAL
            if wanted and distance(pos, site) > 5:
                consider(dict(pos=site), 95 if has_laser and "laser" not in held else 60, 260)
        walk = max(1.0, me["stats"]["walk_speed"]) * .8
        for drop in obs.get("drops") or ():
            if drop.get("holder") not in (None, me["id"]):
                continue
            if not (me["cubes"] < 10 or me["hp"] < me["max_hp"] * .8):
                continue
            spot = drop["pos"]
            if sum(1 for m in obs["mechs"] if m["kind"] == "mech" and distance(m["pos"], spot) < 80) >= 2:
                continue
            # Leave so as to arrive as it lands (at most ~3 s early); late is still worth it.
            travel = max(0.0, distance(pos, spot) - drop["radius"]) / walk
            if drop["lands_in"] <= travel + 3:
                needed = me["hp"] < me["max_hp"] * .7 or me["cubes"] < 10
                consider(drop, (250 if needed else 57) - 2 * drop["lands_in"], DROP_REACH)
        return max(options, key=lambda item: item[0])[1] if options else None

    def far_loot(self, obs):
        """Nearest remembered loot we still want inside the next circle, however far (explore heads there)."""
        me, zone = obs["self"], obs["zone"]
        pos = me["pos"]
        unarmed = any(w is None for w in me["weapons"])
        best, best_dist = None, math.inf
        for item in self.memory.values():
            what, p = item["what"], item["pos"]
            if what == "pickups":
                if not self.wanted_weapon(me, item["kind"]):
                    continue
            elif what == "crates":
                if not unarmed and me["cubes"] >= CUBE_GOAL:
                    continue
            elif me["cubes"] >= CUBE_GOAL:
                continue
            dist = distance(pos, p) + (0 if what == "pickups" else 60)
            if dist >= best_dist or tuple(p[:2]) in self.unreachable_loot or self.on_roof(p, pos):
                continue
            if distance(p, zone["next_center"]) > zone["next_radius"] * .9:
                continue
            best, best_dist = (p[0], p[1]), dist
        return best

    @staticmethod
    def weapon_slot(me):
        """(slot a better pickup would go into, value of what is there now).

        The target loadout is laser + lightning: the laser outranges the house snipers and lightning
        shorts their lasers up close, so the arm gun is given up for whichever comes second. A mortar
        is never worth holding against house bots: lasers do +60% (cook-off) to a mech carrying one."""
        weapons = me["weapons"]
        for slot, weapon in enumerate(weapons):
            if weapon is None:
                return slot, 0
        others = [(WEAPON_VALUE.get(w["kind"], 0), slot) for slot, w in enumerate(weapons)]
        if not others:
            return None, math.inf
        value, slot = min(others)
        return slot, value

    def charged_laser(self, obs):
        me = obs["self"]
        center = with_height(me["pos"], .5 * me["stats"]["height"])
        for enemy in obs["mechs"]:
            if not enemy.get("charging") or enemy.get("kind") != "mech":
                continue
            if distance(me["pos"], enemy["pos"]) <= .95 * self.zap_range:
                continue  # in arc range: our lightning shorts its laser before it can fire
            scale = 1 + self.config["cubes"]["size_per_cube"] * enemy.get("cubes", 0)
            muzzle = with_height(enemy["pos"], self.config["mech"]["muzzle_height"] * scale)
            d = distance(center, muzzle)
            pitch = math.atan2(center[2] - muzzle[2], d)
            yaw_error = abs(math.remainder(angle_to(muzzle, center) - enemy["aim"][0], math.tau))
            tolerance = .15 + math.atan2(me["stats"]["radius"], max(1, d))
            if (d <= self.config["weapons"]["laser"]["range"] + 3 and yaw_error < tolerance
                    and abs(pitch - enemy["aim"][1]) < tolerance and self.world.clear(muzzle, center)):
                return enemy
        return None

    def maneuver(self, obs, threats, urgent, retreat, laser=False):
        me, zone = obs["self"], obs["zone"]
        pos = me["pos"]
        foe = threats[0]["pos"]
        away = angle_to(foe, pos)
        enemy_eyes = [with_height(m["pos"], 8 * (1 + self.config["cubes"]["size_per_cube"] * m.get("cubes", 0)))
                      for m in threats[:2]]
        radius = me["stats"]["radius"]
        next_center = zone["next_center"]
        zone_center, zone_radius = self.live_zone(obs)
        proposed = [(pos[0], pos[1])]
        if urgent:
            heading = angle_to(pos, self.zone_goal(obs))
        elif laser:
            heading = away + self.side * math.pi / 2
            across = math.cos(heading) * me["vel"][0] + math.sin(heading) * me["vel"][1]
            if across < -2:
                heading += math.pi
        elif retreat:
            heading = away
        else:
            heading = away + self.side * math.pi / 2
        offsets = (0, .55, -.55, 1.15, -1.15, 1.85, -1.85, math.pi)
        for length in (7, 17, 39):
            proposed.extend(point_at(pos, heading + turn * self.side, length) for turn in offsets)
        best, best_score = proposed[0], -math.inf
        for i, p in enumerate(proposed):
            ground = self.world.ground(*p)
            if i and not self.world.is_walkable(p[0], p[1], radius):
                continue
            z = pos[2] if i == 0 else ground
            eye = (p[0], p[1], z + me["stats"]["eye_height"])
            # Out of time: rank the remaining points on distance and zone alone.
            late = time.perf_counter() > self.hard_deadline
            exposed = 0 if late else sum(self.world.clear(e, eye) for e in enemy_eyes)
            dist = distance(p, foe)
            goal_distance = distance(p, next_center)
            outside = max(0, distance(p, zone_center) - max(0, zone_radius - 5))
            score = -outside * 5 - max(0, goal_distance - zone["next_radius"] + 5) * .15
            if urgent:
                score -= goal_distance * 1.7
            if laser:
                # A charged beam has no flight time. Reach the nearest shielded point,
                # or keep crossing the attacker's view if no cover is accessible.
                score -= 70 * exposed
                if i:
                    score += 14 * math.cos(angle_to(pos, p) - heading)
                if exposed == 0:
                    score -= .3 * distance(pos, p)
            elif retreat:
                score += min(dist, 125) * .35 - 38 * exposed
            else:
                near, far, _ = self.duel_band(obs, threats[0])
                ideal = min(far, max(near, distance(pos, foe)))
                score -= .48 * abs(dist - ideal)
                score += 7 if exposed == 1 else -6 * exposed
                score += 6 * math.cos(angle_to(pos, p) - heading) if i else 0
                if not i and exposed:
                    score -= 12
            # Nearby immediately accessible cover wins over a point across a solid wall.
            if i and not self.world.walk_clear(pos[:2], p, radius):
                score -= 9
            score -= .11 * distance(pos, p)
            if score > best_score:
                best, best_score = p, score
        return best

    def explore(self, obs):
        pos, zone = obs["self"]["pos"], obs["zone"]
        if distance(pos, zone["next_center"]) > max(12, zone["next_radius"] * .83):
            return self.zone_goal(obs)
        # Explore distinct ground locations while looking for floor loot or the first pursuer.
        if self.last_explore and distance(pos, self.last_explore) > 6:
            if distance(self.last_explore, zone["next_center"]) < max(8, zone["next_radius"] * .9):
                return self.last_explore
        if zone["next_radius"] < 12:
            return zone["next_center"]
        radius = obs["self"]["stats"]["radius"]
        hop = min(52, zone["next_radius"] * .55)

        def allowed(p):
            return (distance(p, zone["next_center"]) < zone["next_radius"] * .9
                    and self.world.is_walkable(p[0], p[1], radius))
        # Hop toward loot remembered out of reach, else toward the middle of the next circle,
        # along buildings: a sniper who finds us there leaves a wall within a short dash.
        target = self.far_loot(obs)
        loot = target is not None
        if target is None and distance(pos, zone["next_center"]) > 70:
            target = tuple(zone["next_center"])
        # Asked for a new hop without having moved: the last one led nowhere. Try another way round.
        failed = self.hop_origin is not None and distance(pos, self.hop_origin) < 6
        self.hop_origin = (pos[0], pos[1])
        if failed and loot:
            self.unreachable_loot[target] = obs["t"] + 20
        if target is not None and not failed:
            if distance(pos, target) <= hop + 8 and allowed(target):
                self.last_explore = tuple(target)
                return self.last_explore
            toward = angle_to(pos, target)
            best, best_score = None, -math.inf
            for turn in (0, .45, -.45, .9, -.9, 1.35, -1.35):
                p = point_at(pos, toward + turn, hop)
                if not allowed(p):
                    continue
                score = hop * math.cos(turn) + (9 if self.world.wall_distance(*p) <= radius + 6 else 0)
                if score > best_score:
                    best, best_score = p, score
            if best is not None:
                self.last_explore = best
                return best
        self.heading += self.side * 1.31
        options = []
        for step in range(8):
            p = point_at(pos, self.heading + step * self.side * math.pi / 4, hop)
            if allowed(p):
                options.append(p)
        if not options:
            return zone["next_center"]
        walled = [p for p in options if self.world.wall_distance(*p) <= radius + 6]
        self.last_explore = (walled or options)[0]
        return self.last_explore

    def finishing_drop(self, obs, threats):
        """Stay in an uncontested ring with hunters about when the repair will come before they can
        stop it: always through the last moments, earlier while HP covers the wait."""
        me = obs["self"]
        for drop in obs.get("drops") or ():
            if drop["lands_in"] > 0 or drop.get("holder") != me["id"]:
                continue
            left = drop["capture_s"] - drop["progress"]
            ring = drop["radius"]
            if not 0 < left or distance(me["pos"], drop["pos"]) > ring:
                continue
            if left <= .9:
                # A runner immediately outside can contest the clock before it completes.
                reach = ring + left * me["stats"]["boost_speed"] * 1.2
            elif me["hp"] > 150 * left * max(1, len(threats)):
                # About 150 HP/s is what one expert deals at its closest; the capture restores everything.
                reach = ring + 6 + left * me["stats"]["walk_speed"] * 1.1
            else:
                continue
            if all(distance(m["pos"], drop["pos"]) > reach for m in threats):
                return self.ring_point(me["pos"], drop)
        return None

    def body_scale(self, mech):
        return 1 + self.config["cubes"]["size_per_cube"] * mech.get("cubes", 0)

    NEAR_RINGS = ((0, 1, 0), (3.5, 12, 0), (7, 12, math.pi / 12), (11, 12, 0),
                  (16, 12, math.pi / 12), (23, 12, 0))
    FAR_RINGS = ((31, 16, 0), (41, 16, math.pi / 16), (53, 16, 0), (68, 16, math.pi / 16))

    def cover_points(self, obs, threats, rings=NEAR_RINGS):
        """Reachable standing points around us: (point, ids that can see our center, ids that can
        see any part of our body, distance from us).

        House hunters fight from ~150 m with a hitscan laser, so standing anywhere a wall is not
        within a short dash is what loses duels. Near rings must be reachable in a straight line."""
        me = obs["self"]
        pos, stats = me["pos"], me["stats"]
        radius = stats["radius"]
        guns = [(m["id"], with_height(m["pos"], self.config["mech"]["muzzle_height"] * self.body_scale(m)))
                for m in threats[:LOS_THREATS]]
        zone_center, zone_radius = self.live_zone(obs)
        points = []
        for ring, count, offset in rings:
            # Wall-clock guard: near rings come first, and a late answer costs a second of coasting.
            if ring and time.perf_counter() > self.deadline:
                break
            for k in range(count):
                if ring and time.perf_counter() > self.hard_deadline:
                    return points
                if ring:
                    p = point_at(pos, offset + k * math.tau / count, ring)
                    if not self.world.is_walkable(p[0], p[1], radius):
                        continue
                    if distance(p, zone_center) > zone_radius - 6:
                        continue
                    if ring < 25 and not self.world.walk_clear(pos, p, radius):
                        continue
                    z = self.world.ground(*p)
                else:
                    p, z = (pos[0], pos[1]), pos[2]
                body = (p[0], p[1], z + 0.5 * stats["height"])
                head = (p[0], p[1], z + 0.93 * stats["height"])
                center, any_part = set(), set()
                for i, gun in guns:
                    if self.world.clear(gun, body):
                        center.add(i)
                        any_part.add(i)
                        continue
                    # Low cover hides the middle of the body and leaves the head in the beam.
                    if HEAD and self.world.clear(gun, head):
                        any_part.add(i)
                        continue
                    # A center behind a corner can still leave a shoulder in the beam.
                    a = angle_to(gun, body) + math.pi / 2
                    for side in (-radius, radius):
                        edge = (body[0] + side * math.cos(a), body[1] + side * math.sin(a), body[2])
                        if self.world.clear(gun, edge):
                            any_part.add(i)
                            break
                points.append((p, frozenset(center), frozenset(any_part), ring))
        return points

    def still_hidden(self, obs, threats, spot):
        """True if every part of a mech standing at spot is out of the listed threats' line of fire."""
        stats = obs["self"]["stats"]
        z = self.world.ground(spot[0], spot[1])
        body = (spot[0], spot[1], z + 0.5 * stats["height"])
        for m in threats[:LOS_THREATS]:
            gun = with_height(m["pos"], self.config["mech"]["muzzle_height"] * self.body_scale(m))
            a = angle_to(gun, body) + math.pi / 2
            for side in (0, -stats["radius"], stats["radius"]):
                if self.world.clear(gun, (body[0] + side * math.cos(a), body[1] + side * math.sin(a), body[2])):
                    return False
            if HEAD and self.world.clear(gun, (spot[0], spot[1], z + 0.93 * stats["height"])):
                return False
        return True

    def hide_point(self, obs, threats, points=None):
        """Closest point no listed threat can hit, searching farther out if nothing is close.

        Keeps the spot already being run to while it still hides us: switching targets mid-dash
        throws away the momentum the 0.75 s laser warning depends on."""
        pos, now = obs["self"]["pos"], obs["t"]
        if (self.hide_spot is not None and now - self.hide_at < 1.5 and distance(pos, self.hide_spot) < 40
                and not self.dead_end(self.hide_spot) and self.still_hidden(obs, threats, self.hide_spot)):
            self.hide_at = now
            return self.hide_spot
        if points is None:
            points = self.cover_points(obs, threats)
        foe = threats[0]["pos"]
        for group in (points, None):
            if group is None:
                if time.perf_counter() > self.deadline:
                    break
                group = self.cover_points(obs, threats, self.FAR_RINGS)
            hidden = [c for c in group if not c[2] and not (self.dead_ends and self.dead_end(c[0]))]
            if hidden:
                self.hide_spot = tuple(min(hidden, key=lambda c: c[3] - 0.08 * distance(c[0], foe))[0][:2])
                self.hide_at = now
                return self.hide_spot
        return None

    def peek_point(self, obs, threats, band, escape_limit=7):
        """A firing position on the primary threat with cover a short dash away.

        A charging laser is visible for about 0.75 s before it fires; a walking mech covers
        about 6 m in that time. Returns (point or None, all candidate points)."""
        points = self.cover_points(obs, threats)
        hidden = [c[0] for c in points if not c[2]]
        primary = threats[0]
        radius = obs["self"]["stats"]["radius"]
        near, far, floor = band
        best, best_score = None, -math.inf
        for p, seen, _, ring in points:
            if primary["id"] not in seen:
                continue
            escape = min((distance(p, h) for h in hidden
                          if distance(p, h) <= escape_limit and self.world.walk_clear(p, h, radius)),
                         default=math.inf)
            if escape > escape_limit:
                continue
            d = distance(p, primary["pos"])
            # Outside the preferred band the range error outweighs the walk (0.5 per metre of ring).
            score = (-0.5 * ring - 3.0 * escape - 0.8 * (max(0, near - d) + max(0, d - far))
                     - 3.0 * max(0, floor - d) - EXTRA_EXPOSE * (len(seen) - 1))
            if score > best_score:
                best, best_score = p, score
        return best, points

    def stalk_point(self, obs, threats):
        """A hidden point at least 3 m closer to the nearest threat, or None."""
        pos, foe = obs["self"]["pos"], threats[0]["pos"]
        now_dist = distance(pos, foe)
        if now_dist < self.duel_range(obs, threats[0]):
            return None
        hidden = [c for c in self.cover_points(obs, threats) if not c[2]
                  and distance(c[0], foe) < now_dist - 3]
        return min(hidden, key=lambda c: distance(c[0], foe))[0] if hidden else None

    def grab_point(self, obs):
        """A weapon worth leaving a fight for.

        Second slot empty (the weapon rush): any remembered laser or lightning within RUSH_REACH, the
        laser unless it is more than RUSH_LASER_EXTRA farther than the other. With a mech close and
        looking at us, or with a second weapon already held, only the short mid-fight distances."""
        me = obs["self"]
        pos, radius = me["pos"], me["stats"]["radius"]
        if self.our_ring(obs) is not None:
            return None   # never walk out of a running capture for a weapon
        _, held_value = self.weapon_slot(me)
        held = {w["kind"] for w in me["weapons"] if w}
        zone_center, zone_radius = self.live_zone(obs)
        rush = any(w is None for w in me["weapons"])
        if rush:
            eyes = with_height(pos, me["stats"]["eye_height"])
            for m in obs["mechs"]:
                if (m["kind"] == "mech" and distance(pos, m["pos"]) <= RUSH_CLOSE
                        and self.world.clear(eyes, with_height(m["pos"], 4.5 * self.body_scale(m)))):
                    rush = False
                    break
        laser_reach = max(GRAB_LASER, RUSH_REACH) if rush else GRAB_LASER
        other_reach = RUSH_REACH if rush else GRAB_OTHER
        nearest = {}   # is it a laser -> (distance, point)
        for item in self.memory.values():
            if item["what"] != "pickups":
                continue
            kind, at = item["kind"], item["pos"]
            if kind in held or WEAPON_VALUE.get(kind, 0) <= held_value or tuple(at[:2]) in self.unreachable_loot:
                continue
            best = kind == "laser"
            dist = distance(pos, at)
            if dist > (laser_reach if best else other_reach) or dist >= nearest.get(best, (math.inf,))[0]:
                continue
            if distance(at, zone_center) > zone_radius - 2 or self.on_roof(at, pos):
                continue
            if dist > 4 and not self.world.is_walkable(at[0], at[1], radius):
                continue
            nearest[best] = (dist, tuple(at[:2]))
        if "laser" not in held:
            for site, _, has_laser in self.kill_sites:
                dist = distance(pos, site)
                if (has_laser and 5 < dist <= laser_reach and dist < nearest.get(True, (math.inf,))[0]
                        and abs(site[2] - pos[2]) <= 4
                        and tuple(site[:2]) not in self.unreachable_loot
                        and distance(site, zone_center) <= zone_radius - 2):
                    nearest[True] = (dist, tuple(site[:2]))
        laser, other = nearest.get(True), nearest.get(False)
        if laser and (other is None or laser[0] <= other[0] + RUSH_LASER_EXTRA):
            return laser[1]
        return other[1] if other else None

    def brawl_point(self, obs, threats, target):
        """Where to stand in a lightning fight: in arc range of the target, seen by as few others as possible."""
        ideal = .6 * self.zap_range
        best, best_score = None, -math.inf
        for p, seen, any_part, ring in self.cover_points(obs, threats):
            if target["id"] not in seen:
                continue
            d = distance(p, target["pos"])
            score = -abs(d - ideal) - 14 * len(any_part - {target["id"]}) - .15 * ring
            if d > .9 * self.zap_range:
                score -= 25
            if score > best_score:
                best, best_score = p, score
        return best if best is not None else tuple(target["pos"][:2])

    def duel_band(self, obs, threat):
        """(nearest, farthest, never-closer-than) distance to fight this mech at.

        A house expert's damage on us falls from ~150 HP/s under 50 m to ~43 HP/s at 105-140 m, while
        an accurate laser still hits at that range. A lightning holder only fires inside 42 m and walks
        straight in: the same band keeps us out of its reach while we shoot it."""
        theirs = threat.get("weapons", ())
        if ZAP_RUSH and self.zap_range > 0 and "lightning" not in theirs:
            return .5 * self.zap_range, .85 * self.zap_range, 0.0
        return DUEL_NEAR, DUEL_FAR, DUEL_MIN

    def duel_range(self, obs, threat):
        near, far, _ = self.duel_band(obs, threat)
        return min(far, max(near, distance(obs["self"]["pos"], threat["pos"])))

    def quick_cover(self, obs, foes):
        """A point hidden from the listed mechs that we reach before a charge that just began is let go
        (the house laser fires 0.8 s after it starts charging), or None."""
        me = obs["self"]
        charging = any(w and w["kind"] == "laser" and w.get("charge", 0) > 0 for w in me["weapons"])
        reach = DODGE_REACH * (.55 if charging else 1)   # our own charge halves our speed
        # Its own small time slice: the plan that follows still needs the rest of the budget.
        deadline, hard = self.deadline, self.hard_deadline
        self.deadline = self.hard_deadline = min(deadline, time.perf_counter() + .003)
        try:
            points = self.cover_points(obs, foes, self.NEAR_RINGS[:3])
        finally:
            self.deadline, self.hard_deadline = deadline, hard
        hidden = [c for c in points if not c[2] and c[3] <= reach]
        return tuple(min(hidden, key=lambda c: c[3])[0][:2]) if hidden else None

    def weave(self, obs, targets, threats, anchor):
        """Never stand at a firing position: swing a few metres either side of it, across the primary
        threat's line of fire, reversing at random, without stepping into another threat's view."""
        me, now = obs["self"], obs["t"]
        pos, stats = me["pos"], me["stats"]
        swing = 6.5 if self.dash else 4
        if distance(pos, anchor) > swing + 2:
            return anchor
        if now >= self.strafe_until:
            if self.rng.random() < .85:
                self.strafe = -self.strafe
            self.strafe_until = now + self.rng.uniform(.5, 1.1)
        primary = targets[0]
        chest = with_height(primary["pos"], 4.5 * self.body_scale(primary))
        others = [with_height(m["pos"], self.config["mech"]["muzzle_height"] * self.body_scale(m))
                  for m in threats if m["id"] != primary["id"]][:2]
        tangent = angle_to(primary["pos"], anchor) + math.pi / 2
        for side in (self.strafe, -self.strafe):
            p = point_at(anchor, tangent, swing * side)
            if (not self.world.is_walkable(p[0], p[1], stats["radius"])
                    or not self.world.walk_clear(pos, p, stats["radius"])):
                continue
            if time.perf_counter() <= self.hard_deadline:
                z = self.world.ground(*p)
                if not self.world.clear((p[0], p[1], z + stats["muzzle_height"]), chest):
                    continue
                body = (p[0], p[1], z + .5 * stats["height"])
                if any(self.world.clear(gun, body) for gun in others):
                    continue
            self.strafe = side
            return p
        return anchor

    def duel_point(self, obs, targets, threats):
        """Where to walk in a duel: across the primary threat's line of fire, reversing at random
        (a linear-lead gun misses a target that reverses), steering into the preferred range band and
        keeping the target in our sights. Never a place to stand: the goal is always a stride away."""
        me, now = obs["self"], obs["t"]
        pos, stats = me["pos"], me["stats"]
        radius = stats["radius"]
        primary = targets[0]
        foe = primary["pos"]
        dist = distance(pos, foe)
        near, far, floor = self.duel_band(obs, primary)
        if now >= self.strafe_until:
            if self.rng.random() < .85:
                self.strafe = -self.strafe
            self.strafe_until = now + self.rng.uniform(.5, 1.1)
        self.duel_close = dist < floor
        # Radial wish: negative backs away, positive closes in, zero inside the band.
        if dist < floor:
            radial = -2.2
        elif dist < near:
            radial = -.35 - .9 * (near - dist) / max(1.0, near - floor)
        elif dist > far:
            radial = .35 + min(1.0, (dist - far) / 30)
        else:
            radial = 0.0
        toward = angle_to(pos, foe)
        cos_t, sin_t = math.cos(toward), math.sin(toward)
        zone_center, zone_radius = self.live_zone(obs)
        chest = with_height(foe, 4.5 * self.body_scale(primary))
        others = [(m, with_height(m["pos"], self.config["mech"]["muzzle_height"] * self.body_scale(m)))
                  for m in threats if m["id"] != primary["id"]][:2]
        best, best_score, best_side = None, -math.inf, self.strafe
        for side in (self.strafe, -self.strafe):
            heading = math.atan2(side * cos_t + radial * sin_t, -side * sin_t + radial * cos_t)
            for turn in (0, .5, -.5, 1.0, -1.0, 1.5, -1.5):
                p = point_at(pos, heading + turn, 10)
                if not self.world.is_walkable(p[0], p[1], radius) or not self.world.walk_clear(pos, p, radius):
                    continue
                d = distance(p, foe)
                score = 10 * math.cos(turn) - (5 if side != self.strafe else 0)
                score -= 5 * max(0, distance(p, zone_center) - (zone_radius - 8))
                if d < floor:
                    score -= 12 + 2.5 * (floor - d)
                elif d < near:
                    score -= .6 * (near - d)
                elif d > far:
                    score -= .4 * (d - far)
                if time.perf_counter() <= self.hard_deadline:
                    z = self.world.ground(*p)
                    if not self.world.clear((p[0], p[1], z + stats["muzzle_height"]), chest):
                        score -= 9   # a place we cannot shoot it from
                    body = (p[0], p[1], z + .5 * stats["height"])
                    for m, gun in others:
                        if self.world.clear(gun, body):
                            score -= EXTRA_EXPOSE
                        score -= 1.5 * max(0, DUEL_MIN - distance(p, m["pos"]))
                if score > best_score:
                    best, best_score, best_side = p, score, side
        if best is None:
            return self.maneuver(obs, targets, False, False)
        self.strafe = best_side
        return best

    def plan(self, obs, threats, urgent, emergency, laser, shelter):
        me = obs["self"]
        now = obs["t"]
        eyes = with_height(me["pos"], me["stats"]["eye_height"])
        exposed = [m for m in threats if self.world.clear(eyes, with_height(m["pos"], 4.5 * (
            1 + self.config["cubes"]["size_per_cube"] * m.get("cubes", 0))))]
        if len(exposed) >= 2 or (exposed and me["hp"] < REPAIR_LOW * me["max_hp"]):
            self.hide_until = now + me["stats"]["regen_delay"] + 2
        if exposed and me["hp"] < REPAIR_LOW * me["max_hp"]:
            self.repairing = True
        if me["hp"] >= REPAIR_HIGH * me["max_hp"]:
            self.repairing = False
        retreat = self.repairing or now < self.hide_until
        capture = self.finishing_drop(obs, threats)
        # Distant charges only (HIDE_RANGE): not worth breaking off a fight for, but stepping behind a
        # wall that is right here costs nothing and the beam cannot hit what it cannot see.
        quick = None
        self.soft_dodge = False
        if not urgent and (laser or shelter) and (laser is None or laser["id"] in self.soft_ids) and all(
                m["id"] in self.soft_ids for m in shelter):
            first = ([laser] if laser else []) + [m for m in shelter if not laser or m["id"] != laser["id"]]
            quick = self.quick_cover(obs, first + [m for m in threats if m not in first])
            if quick is None:
                laser, shelter = None, []
            else:
                self.soft_dodge = True
        # Lightning fight: a visible hunter close enough to reach before it does much damage.
        near = threats[0] if threats else None
        near_dist = distance(me["pos"], near["pos"]) if near else math.inf
        brawl = (BRAWL and self.zap_range > 0 and near is not None and near in exposed
                 and "lightning" not in near.get("weapons", ())
                 and near_dist <= self.zap_range + BRAWL_REACH
                 and (me["energy"] >= BRAWL_ENERGY or self.mode == "brawl"))
        grab = None if urgent or laser else self.grab_point(obs)
        if capture and not urgent and not (laser and me["hp"] < .3 * me["max_hp"]):
            self.mode, self.goal = "capture", capture
        elif urgent:
            self.mode = "zone"
            self.goal = self.zone_goal(obs)
            if threats and distance(me["pos"], self.goal) > 20 and retreat and not emergency:
                self.goal = self.maneuver(obs, exposed or threats, True, True)
        elif laser:
            spot = quick if quick is not None else self.hide_point(
                obs, [laser] + [m for m in threats if m["id"] != laser["id"]])
            self.mode = "evade laser"
            self.goal = spot if spot is not None else self.maneuver(obs, [laser], False, retreat, laser=True)
        elif grab is not None:
            self.mode, self.goal = "grab", grab
        elif brawl:
            # Never back out of arc range: up close the hunter's laser is shorted and nothing misses.
            self.mode, self.goal = "brawl", self.brawl_point(obs, threats, near)
        elif AMBUSH and self.has_zap and threats and not self.repairing and now >= self.no_ambush_until:
            # Hunters stand off at laser range while they see us and walk in when they do not.
            # Break the line of sight and let them come into arc range.
            self.mode = "ambush"
            if exposed:
                spot = self.hide_point(obs, exposed + [m for m in threats if m not in exposed])
                if spot is not None:
                    self.goal = spot
                elif self.zap_range > 0 and near_dist <= self.zap_range + 45 and me["energy"] >= 60:
                    # No cover to wait behind: close the gap before its laser does the work.
                    self.mode, self.goal = "brawl", tuple(near["pos"][:2])
                else:
                    self.goal = self.maneuver(obs, exposed, False, True)
            else:
                self.goal = tuple(me["pos"][:2])
        elif (AMBUSH and self.has_zap and not threats and now - self.seen_at < AMBUSH_WAIT and me["in_zone"]
              and now >= self.no_ambush_until):
            self.mode, self.goal = "ambush", tuple(me["pos"][:2])
        elif shelter:
            # A charged laser can be held for 2 s waiting for us to step out. Stay hidden from
            # every charging mech until it fires or lets go.
            spot = quick if quick is not None else self.hide_point(
                obs, shelter + [m for m in threats if m not in shelter])
            self.mode = "cover"
            # No hidden point in reach: keep crossing the charging mech's view instead of standing in it.
            self.goal = spot if spot is not None else self.maneuver(obs, shelter, False, retreat, laser=True)
        elif ((self.repairing or self.resetting) and not exposed and now - self.combat.combat_time > 1
              and self.rest(obs, threats)):
            # The five-second tracking window can expire just as health starts regenerating.
            pass  # rest() chose: our kill's loot, a hidden point farther from the hunters, or stay
        elif threats and (exposed or retreat):
            targets = exposed or threats
            if retreat:
                self.mode = "retreat"
                if not exposed and me["hp"] < .88 * me["max_hp"]:
                    # Hidden: keep the wall between us and back away at a walk (no boost noise).
                    away = self.fall_back(obs, self.hunters(obs, threats))
                    self.mode = "repair" if away is not None else "retreat"
                    self.goal = away if away is not None else tuple(me["pos"][:2])
                else:
                    spot = self.hide_point(obs, targets)
                    self.goal = spot if spot is not None else self.maneuver(obs, targets, False, True)
            else:
                self.mode = "duel"
                # Range is what beats the house guns and lasers, and a mech that stands still is what
                # accurate lasers punish: fight inside the preferred band and never stop moving.
                near, far, floor = band = self.duel_band(obs, targets[0])
                spot = hide = None
                if PEEK and (not GATE or floor <= distance(me["pos"], targets[0]["pos"]) <= far + 25):
                    # Cover within a step lets us dodge its beam: fight from beside it when the range
                    # is about right, swinging either side of the firing position.
                    ready = any("laser" in m.get("weapons", ()) and self.laser_cool_until.get(m["id"], -1) < now + .4
                                for m in targets)
                    spot, points = self.peek_point(obs, targets, band, ESC_READY if ready else ESC_COOL)
                    if spot is None and not OPEN_DUEL:
                        hide = self.hide_point(obs, targets, points)
                if spot is not None:
                    self.goal = self.weave(obs, targets, threats, spot) if WEAVE else spot
                elif hide is not None:
                    self.goal = hide
                else:
                    self.goal = self.duel_point(obs, targets, threats)
        elif STALK and not urgent and any("laser" in m.get("weapons", ()) and distance(me["pos"], m["pos"]) < 280
                                          for m in threats):
            # A laser carrier we cannot currently see: close in through cover instead of
            # wandering into its line of fire. Hold position if no covered step gets closer.
            self.mode, self.goal = "stalk", self.stalk_point(obs, threats) or tuple(me["pos"][:2])
        else:
            loot = self.loot_options(obs)
            self.mode = "loot" if loot is not None else "explore"
            self.goal = loot if loot is not None else self.explore(obs)
        self.plan_at = now + (.3 if laser or threats else .7 if urgent else .9)
        if self.mode == "duel":
            self.plan_at = min(self.plan_at, max(now + .1, self.strafe_until))
        else:
            self.duel_close = False

    def drive(self, obs, boost):
        me = obs["self"]
        pos, velocity, stats = me["pos"], me["vel"], me["stats"]
        dist = distance(pos, self.goal)
        if dist < 1.3:
            return [0, 0], False, False
        xy = self.world.move_toward(pos, self.goal, stats["radius"])
        # Decelerate into a pickup or cover position using actual velocity and available traction.
        accel = max(1, stats["ground_accel"] if me["grounded"] else stats["air_accel"])
        speed = math.hypot(*velocity[:2])
        brake_distance = speed * speed / (2 * accel)
        scale = min(1, math.sqrt(2 * accel * max(0, dist - .8)) / max(1, stats["walk_speed"]))
        if dist < brake_distance + 3:
            boost = False
        now = obs["t"]
        if self.last_anchor is None or distance(pos, self.last_anchor) > 3 or dist < 5:
            self.last_anchor, self.stuck_since = tuple(pos), now
            self.stuck_first = None
        jump = False
        if dist > 5 and self.stuck_since is not None and now - self.stuck_since > 1.6:
            if self.mode in ("loot", "grab"):
                self.unreachable_loot[tuple(self.goal[:2])] = now + 12
                self.plan_at = now
                self.stuck_since = now
                return [0, 0], False, False
            if self.mode == "explore":
                self.last_explore = None
                self.plan_at = now
            if self.stuck_first is None:
                self.stuck_first = now
            if ((self.mode in ("retreat", "repair", "evade laser", "cover") or now - self.hurt_at < 4)
                    and now - self.stuck_first < 4.5):
                # Under fire or running for cover: a jet hop hangs us in the air in plain view.
                # Give this spot up and let the planner pick another one (three tries, then hop).
                self.dead_ends.append((tuple(self.goal[:2]), now + 6))
                self.hide_spot = self.fall_spot = None
                self.plan_at = now
                self.stuck_since = now
            elif now >= self.next_jump and me["grounded"] and me["energy"] > 42:
                jump, self.jet_until, self.next_jump = True, now + 2.5, now + 5.0
                self.plan_at = now
                self.stuck_since = now
        if now < self.jet_until and math.hypot(*xy) < .1:
            angle = angle_to(pos, self.goal)
            xy = math.cos(angle), math.sin(angle)
        if not me["grounded"]:
            boost = now < self.jet_until and me["energy"] > 16
        return [xy[0] * scale, xy[1] * scale], boost, jump

    def avoid_projectile(self, obs, move):
        """Choose a reachable sidestep when a visible game projectile is on a collision course."""
        me, now = obs["self"], obs["t"]
        if not me["grounded"]:
            return move
        if now < self.dodge_until:
            return self.dodge_vector
        pos, vel, stats = me["pos"], me["vel"], me["stats"]
        earliest, incoming = .75, None
        for shot in obs["projectiles"]:
            if shot["kind"] not in ("gun", "npc"):
                continue
            p, v = shot["pos"], shot["vel"]
            dx, dy = p[0] - pos[0], p[1] - pos[1]
            vx, vy = v[0] - vel[0], v[1] - vel[1]
            t = -(dx * vx + dy * vy) / max(1, vx * vx + vy * vy)
            if not .16 < t < earliest:
                continue
            z = p[2] + v[2] * t - pos[2]
            if 0 <= z <= stats["height"] and math.hypot(dx + vx * t, dy + vy * t) < stats["radius"] + .4:
                earliest, incoming = t, v
        if incoming is None:
            return move
        norm = max(1, math.hypot(*incoming[:2]))
        left = (-incoming[1] / norm, incoming[0] / norm)
        # Keeping existing lateral momentum avoids spending most of the warning time braking.
        toward = left[0] * (vel[0] + 4 * move[0]) + left[1] * (vel[1] + 4 * move[1])
        first = 1 if toward > 0 else -1 if toward < 0 else self.side
        for sign in (first, -first):
            side = [sign * left[0], sign * left[1]]
            dest = (pos[0] + 7 * side[0], pos[1] + 7 * side[1])
            if self.world.walk_clear(pos, dest, stats["radius"]):
                self.dodge_vector = side
                self.dodge_until = now + min(.45, earliest + .05)
                return side
        return move

    def avoid_shell(self, obs, move, boost):
        """Leave the blast circle of a mortar shell about to land on us: (move, boost).

        Shells are lobbed over buildings (36% without line of sight) and stay visible for the
        1.5-3 s they fly, so walking 10 m is enough."""
        me = obs["self"]
        rules = self.config["weapons"].get("mortar")
        if not rules or not me["grounded"] or not obs["projectiles"]:
            return move, boost
        pos, vel, stats = me["pos"], me["vel"], me["stats"]
        gravity = max(1, rules.get("gravity", 22))
        reach = rules.get("splash_radius", 7) + stats["radius"] + 1.5
        first = None
        for shot in obs["projectiles"]:
            if shot["kind"] != "mortar":
                continue
            p, v = shot["pos"], shot["vel"]
            floor, t, land = pos[2], None, None
            for _ in range(2):
                root = v[2] * v[2] + 2 * gravity * (p[2] - floor)
                if root < 0:
                    t = None
                    break
                t = (v[2] + math.sqrt(root)) / gravity
                land = (p[0] + v[0] * t, p[1] + v[1] * t)
                floor = self.world.ground(*land)
            if t is not None and 0 < t < 3.5 and distance(pos, land) < reach + 12 and (first is None or t < first[0]):
                first = (t, land)
        if first is None:
            return move, boost
        t, land = first
        speed = stats["walk_speed"]
        ahead = (pos[0] + (.5 * vel[0] + .5 * speed * move[0]) * t,
                 pos[1] + (.5 * vel[1] + .5 * speed * move[1]) * t)
        if distance(ahead, land) > reach + 2 and math.hypot(*move) > .5:
            return move, boost  # already walking out of it, or past it
        if distance(pos, land) > reach + 2:
            # The way we were going leads into it: stop short of the circle, not before.
            return (move, boost) if distance(pos, land) > reach + 8 else ([0, 0], False)
        if distance(pos, land) > .5:
            away = angle_to(land, pos)
        else:
            away = math.atan2(vel[1], vel[0]) if math.hypot(*vel[:2]) > 1 else self.heading
        for turn in (0, .7, -.7, 1.4, -1.4):
            yaw = away + turn * self.side
            if self.world.walk_clear(pos, point_at(pos, yaw, 9), stats["radius"]):
                return [math.cos(yaw), math.sin(yaw)], me["energy"] > 26
        return move, boost

    GHOST_KINDS = {"laser_charge": 2.6, "gun": 2.0, "step": 2.0, "boost": 2.0, "laser": 1.0}
    GHOST_RANGE = {"near": 40, "mid": 100, "far": 190}

    def ghost_threats(self, obs):
        """Mechs we hear but cannot see, placed along the heard bearing.

        House bots share vision, so an unseen one charging a laser is charging it at us. A sound
        whose bearing matches no visible mech starts a short-lived estimate of its position."""
        now, pos = obs["t"], obs["self"]["pos"]
        visible = [angle_to(pos, m["pos"]) for m in obs["mechs"] if m.get("kind") == "mech"]
        for sound in obs.get("sounds", ()):
            kind = sound.get("kind")
            if kind not in self.GHOST_KINDS or (kind != "laser_charge" and sound.get("dist") == "far"):
                continue
            if any(abs(math.remainder(sound["bearing"] - b, math.tau)) < math.radians(16) for b in visible):
                continue
            heard = now - sound.get("age", 0)
            spot = point_at(pos, sound["bearing"], self.GHOST_RANGE.get(sound.get("dist"), 100))
            ghost = dict(kind=kind, spot=spot, heard=heard, until=heard + self.GHOST_KINDS[kind])
            # One estimate per direction: a newer sound from the same sector replaces the older one,
            # except that a beam (laser) ends the charge it came from.
            keep = []
            for old in self.ghosts:
                same = abs(math.remainder(angle_to(pos, old["spot"]) - sound["bearing"], math.tau)) < math.radians(20)
                if same and (kind != "laser_charge" or old["kind"] != "laser_charge"):
                    if kind in ("gun", "step", "boost") and old["kind"] == "laser_charge" and old["until"] > now:
                        ghost = None  # do not let footsteps erase a charge still in progress
                        keep.append(old)
                    continue
                keep.append(old)
            self.ghosts = keep + ([ghost] if ghost else [])
        self.ghosts = [g for g in self.ghosts if g["until"] > now]
        out = []
        for i, g in enumerate(self.ghosts):
            x, y = g["spot"]
            out.append(dict(id=-1 - i, kind="mech", ghost=True, pos=[x, y, self.world.ground(x, y)], vel=[0, 0, 0],
                            aim=[angle_to(g["spot"], pos), 0], cubes=0, weapons=["gun", "laser"],
                            charging=g["kind"] == "laser_charge"))
        return out

    def track_lasers(self, obs):
        """Estimate when each enemy laser can fire again from beams, hits and charge changes."""
        now = obs["t"]
        cooldown = self.config["weapons"]["laser"]["cooldown"]
        for m in obs["mechs"]:
            if m.get("kind") != "mech":
                continue
            if self.was_charging.get(m["id"]) and not m.get("charging"):
                self.laser_cool_until[m["id"]] = now + cooldown
            self.was_charging[m["id"]] = bool(m.get("charging"))
        for beam in obs.get("beams", ()):
            if beam.get("kind") == "laser" and beam.get("by") is not None:
                self.laser_cool_until[beam["by"]] = now + cooldown - beam.get("age", 0)
        for event in obs["events"]:
            if event["type"] == "damaged" and event.get("weapon") == "laser" and event.get("by") is not None:
                self.laser_cool_until[event["by"]] = now + cooldown

    def act(self, obs):
        self.deadline = time.perf_counter() + PLAN_BUDGET_S
        self.hard_deadline = self.deadline + .003
        me, now = obs["self"], obs["t"]
        self.listen(obs)
        self.remember(obs)
        enemy, aim, fire, reload_gun = self.combat.decide(obs, self.world)
        idle_aim = aim is None and enemy is None   # nothing to shoot or watch: free to look around
        pos = me["pos"]
        threats = sorted((m for m in obs["mechs"] if m["kind"] == "mech"),
                         key=lambda m: distance(pos, m["pos"]))[:3]
        urgent = self.urgent_zone(obs)
        self.track_lasers(obs)
        self.has_zap, self.zap_range = False, 0.0
        for weapon in me["weapons"]:
            if weapon and weapon["kind"] == "lightning":
                stats = weapon.get("stats") or self.config["weapons"]["lightning"]
                self.has_zap = True
                if me["energy"] >= 4 * stats["energy"]:
                    self.zap_range = stats["range"]
        for m in obs["mechs"]:
            if m["kind"] == "mech":
                self.last_pos[m["id"]] = (tuple(m["pos"]), "laser" in m.get("weapons", ()))
        for event in obs["events"]:
            if event["type"] == "kill" and event.get("target") in self.last_pos:
                site, has_laser = self.last_pos.pop(event["target"])
                self.kill_sites.append([site, now + 75, has_laser])
        self.kill_sites = [k for k in self.kill_sites if k[1] > now
                           and not (distance(pos, k[0]) < 6 and abs(pos[2] - k[0][2]) < 5 and k[1] - 75 < now - 1.5)
                           and tuple(k[0][:2]) not in self.unreachable_loot]
        if threats:
            self.seen_at, self.seen_pos = now, tuple(threats[0]["pos"][:2])
        elif any(e["type"] == "kill" for e in obs["events"]):
            self.seen_at = -99.0
        # Waiting only pays if the hunter comes. If a wait drags on, go back to trading shots for a while.
        if self.mode != "ambush" and now - self.ambush_seen > 3:
            self.ambush_since = None
        if self.mode == "ambush":
            self.ambush_seen = now
            if self.ambush_since is None:
                self.ambush_since = now
            elif now - self.ambush_since > AMBUSH_LIMIT:
                self.no_ambush_until, self.ambush_since = now + 15, None
        laser = self.charged_laser(obs)
        rules = self.config["weapons"]["laser"]
        # Every mech charging a laser in range, aimed at us or not yet, including ones only heard.
        ghosts = self.ghost_threats(obs) if USE_GHOSTS else []
        shelter = [m for m in threats + ghosts if m.get("charging")
                   and .95 * self.zap_range < distance(pos, m["pos"]) <= rules["range"] + 3]
        # Charging mechs farther than HIDE_RANGE are "distant" (see the constants): plan() dodges them
        # only when cover is a step away, unless we are nearly dead or several charge at once.
        charging = sum(1 for m in obs["mechs"] if m["kind"] == "mech" and m.get("charging"))
        self.soft_ids = frozenset()
        if me["hp"] >= HIDE_HP * me["max_hp"] and charging < HIDE_COUNT:
            self.soft_ids = frozenset(m["id"] for m in ([laser] if laser else []) + shelter
                                      if distance(pos, m["pos"]) > HIDE_RANGE)
        finishing = any(d.get("holder") == me["id"] and 0 < d["capture_s"] - d["progress"] <= .9
                        for d in obs.get("drops", ()))
        ready = tuple(sorted(m["id"] for m in threats if "laser" in m.get("weapons", ())
                             and self.laser_cool_until.get(m["id"], -1) < now + .4))
        key = (urgent, tuple(m["id"] for m in threats), me["hp"] < me["max_hp"] * REPAIR_LOW,
               finishing, laser and laser["id"], tuple(m["id"] for m in shelter), ready)
        if now >= self.plan_at or key != self.plan_key or self.goal is None:
            self.plan(obs, threats, urgent, not me["in_zone"], laser, shelter)
            self.plan_key = key
        # Loot watchdog: never keep chasing (or standing beside) an item we evidently cannot collect,
        # for example a cube on a ledge above us. Give each target a time allowance, then skip it.
        if self.mode in ("loot", "grab") and self.goal is not None and not any(
                distance(self.goal, d["pos"]) < d["radius"] + 2 for d in obs.get("drops", ())):
            chase = (round(self.goal[0]), round(self.goal[1]))
            if chase != self.chase_key:
                self.chase_key, self.chase_until = chase, now + 6 + distance(pos, self.goal) / 7
            elif now > self.chase_until:
                self.unreachable_loot[tuple(self.goal[:2])] = now + 30
                self.chase_key, self.plan_at = None, now
        else:
            self.chase_key = None
        fast_escape = self.mode == "evade laser" and distance(pos, self.goal) > 4
        boost = (fast_escape or ((urgent or self.mode in ("retreat", "cover")) and distance(pos, self.goal) > 8)) and me["energy"] > 26
        self.dash = DASH and (me["energy"] >= 85 or (self.dash and me["energy"] > 45))
        if self.mode in ("ambush", "grab") and distance(pos, self.goal) > 8:
            # Lightning runs on the same energy as the boost: keep most of it for the fight.
            boost = me["energy"] > (70 if self.has_zap else 30)
        elif self.mode == "brawl":
            boost = bool(me["energy"] > 55 and threats and
                         distance(pos, threats[0]["pos"]) > self.zap_range + 2 and distance(pos, self.goal) > 8)
        elif self.mode == "duel" and self.duel_close:
            boost = me["energy"] > 35   # something got inside the minimum range: open it again quickly
        elif self.mode == "duel" and threats and not self.has_zap:
            # Strafe on the boost in bursts: a faster swing is harder for a straight-line lead to follow.
            # The floor keeps enough energy for a dash to cover.
            boost = self.dash
        # Travelling to loot with nobody tracked: boost in bursts (walk back up to 90, spend down to 55).
        self.cruise = me["energy"] >= 90 or (self.cruise and me["energy"] > 55)
        if self.mode in ("loot", "explore", "grab") and not threats and not self.has_zap:
            ring = self.our_ring(obs) if self.mode == "loot" else None
            if ring is not None:
                self.goal, boost = self.ring_point(pos, ring), False
            else:
                boost = self.cruise and distance(pos, self.goal) > 12
        elif self.mode == "capture":
            ring = self.our_ring(obs)
            if ring is not None:
                self.goal = self.ring_point(pos, ring)
        move, boost, jump = self.drive(obs, boost)
        # Sidestepping a 45-damage bullet must not carry us away from cover that stops a 315-damage beam.
        if not urgent and self.mode not in ("capture", "evade laser", "cover", "retreat", "brawl", "ambush", "grab"):
            move = self.avoid_projectile(obs, move)
        if self.mode not in ("capture", "evade laser") and me["in_zone"]:
            move, boost = self.avoid_shell(obs, move, boost)   # (awareness) mortar shells, in any other mode

        # If hidden and recovering, attacking a crate or drone would reset the repair timer.
        healing = self.mode in ("retreat", "repair", "ambush") and me["hp"] < me["max_hp"] * .88
        focus = self.combat.focus
        if healing and (not focus or focus.get("kind") != "mech"):
            fire, aim = [False, False], None
        if self.mode == "cover" and PRECHARGE:
            # Waiting behind a wall for a held enemy charge: charge ours (speed does not matter while
            # standing still) so the return shot fires the moment we step out into its cooldown.
            for slot, weapon in enumerate(me["weapons"]):
                if weapon and weapon["kind"] == "laser" and weapon["cooldown"] <= .1:
                    fire[slot] = True
        dumping = False
        if ((self.mode == "evade laser" and not self.soft_dodge) or (urgent and not me["in_zone"])
                or (self.mode == "ambush" and distance(pos, self.goal) > 2)):
            for slot, weapon in enumerate(me["weapons"]):
                if weapon and weapon["kind"] == "laser":
                    # Charging cuts ground speed in half while the other weapon remains usable.
                    fire[slot] = False
                    shot = self.combat.laser_shot
                    if weapon.get("charge", 0) > 0 and shot is not None:
                        # Letting go fires along the torso's actual aim: point it at the target in the
                        # same action, and hold at most two more decisions for the torso to get there.
                        aim, dumping = list(shot[0]), True
                        if not shot[1] and self.release_hold < 2:
                            fire[slot] = True
                            self.release_hold += 1
        elif (self.mode in ("cover", "retreat") and distance(pos, self.goal) > 2
              and (NOCHARGE == 2 or (NOCHARGE == 1 and me["hp"] < HIDE_HP * me["max_hp"]))):
            for slot, weapon in enumerate(me["weapons"]):
                if weapon and weapon["kind"] == "laser" and not weapon.get("charge", 0):
                    fire[slot] = False   # do not start a charge (half speed) on the way to cover
        if not dumping:
            self.release_hold = 0
        shooting = aim is not None and bool(focus) and focus.get("kind") == "mech"
        if aim is None:
            if enemy:
                aim = [angle_to(pos, enemy["pos"]), 0]
            elif self.listen_until > now:
                aim = [self.last_bearing, 0]
            elif self.seen_pos is not None and now - self.seen_at < 4 and distance(pos, self.seen_pos) > 3:
                # Face where the hunter was last seen: a hit from behind does 1.5 times the damage.
                aim = [angle_to(pos, self.seen_pos), 0]
            else:
                # Nobody known: keep the torso turning through full circles (one every 1.6 s). Everything
                # in view within 200 m is then seen at least every 1.6 s and tracked for 5 s after, so a
                # third party is noticed before its second shot, and loot all round is remembered.
                aim = [math.remainder(me["aim"][0] + self.side * 1.4, math.tau), -.07]
        # Rear armour: an attacker outside our front arc does x1.5 and is usually outside our view too.
        face = self.guard_aim(obs, aim, shooting,
                              shooting and self.mode not in ("retreat", "repair", "cover", "evade laser"))
        if face is not None:
            aim = [face, 0]
        action = dict(move=move, boost=boost, jump=jump, aim=aim, fire=fire, reload=reload_gun)
        slot, held_value = self.weapon_slot(me)
        held = {w["kind"] for w in me["weapons"] if w}
        # Only what the server lists right now (anything within reach is inside the proximity sensors):
        # a take of a remembered id that is gone fails, and its "drop" could still empty our slot.
        near = [p for p in obs.get("pickups") or ()
                if WEAPON_VALUE.get(p["kind"], 0) > held_value and p["kind"] not in held
                and distance(p["pos"], pos) <= self.config["loot"]["pickup_radius"]
                and abs(p["pos"][2] - pos[2]) <= 4]
        busy = slot is None or bool(me["weapons"][slot] and me["weapons"][slot].get("charge", 0) > 0)
        if near and not busy:
            action["take"] = max(near, key=lambda p: WEAPON_VALUE[p["kind"]])["id"]
            if me["weapons"][slot] is not None:
                action["drop"] = slot
        # Opening look-around: one full turn of the torso while already walking, so the weapons lying
        # around the spawn are in memory within the first second or so.
        if self.sweep_yaw is not None and self.sweep_left > 0:
            self.sweep_left -= abs(math.remainder(me["aim"][0] - self.sweep_yaw, math.tau))
        self.sweep_yaw = me["aim"][0]
        if self.sweep_left > 0 and idle_aim and now - self.hurt_at > 2:
            action["aim"] = [math.remainder(me["aim"][0] + self.side * 1.4, math.tau), -.07]
        if obs.get("offer"):
            priority = ("armor", "plating", "laser_damage", "laser_charge", "nanites", "gun_velocity",
                        "gun_damage", "gun_rate", "lightning_efficiency", "lightning_range", "servos", "gun_mag",
                        "lightning_chain", "gyros", "reactor", "capacitor", "jets", "sensors")
            choices = obs["offer"]["choices"]
            action["pick"] = min(range(len(choices)), key=lambda i: priority.index(choices[i]["id"])
                                 if choices[i]["id"] in priority else len(priority))
        return action


if __name__ == "__main__":
    mechfight.run(Bot)
