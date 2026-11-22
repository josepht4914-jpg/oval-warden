"""Oval Warden, a placement-focused mechfight/1 bot."""
import math
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
GUN_ENGAGE = float(os.environ.get("OW_GUN_ENGAGE", "120"))
# Second-slot preference. Mortar 0: never picked up (lasers do +60% to its carrier).
WEAPON_VALUE = {"laser": 3, "lightning": 2, "mortar": 0}
# Wall-clock time the cover search may use per decision (the server allows 50 ms in total).
PLAN_BUDGET_S = 0.012


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
        self.dodge_until = -1
        self.dodge_vector = None
        self.zone_phase = {}
        self.laser_cool_until = {}
        self.was_charging = {}
        self.deadline = math.inf
        self.hide_spot, self.hide_at = None, -99.0

    def listen(self, obs):
        now = obs["t"]
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

    def loot_options(self, obs):
        me, zone = obs["self"], obs["zone"]
        pos, radius = me["pos"], me["stats"]["radius"]
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
        for cube in obs["cubes"]:
            consider(cube, 47 if me["cubes"] < 10 else 33, 90)
        _, held_value = self.weapon_slot(me)
        held = {w["kind"] for w in me["weapons"] if w}
        for weapon in obs["pickups"]:
            if WEAPON_VALUE.get(weapon["kind"], 0) > held_value and weapon["kind"] not in held:
                consider(weapon, {"laser": 53, "lightning": 50}[weapon["kind"]], 105)
        for crate in obs["crates"]:
            consider(crate, 24, 54)
        for drop in obs.get("drops", []):
            contested = drop.get("holder") not in (None, me["id"])
            if not contested and drop["lands_in"] < 4 and (me["cubes"] < 10 or me["hp"] < me["max_hp"] * .8):
                consider(drop, 57 - 2 * drop["lands_in"], 90)
        return max(options, key=lambda item: item[0])[1] if options else None

    @staticmethod
    def weapon_slot(me):
        """(slot a better pickup would go into, value of what is there now).

        The gun stays. The other slot prefers a laser, then lightning. A mortar is never worth
        holding against house bots: lasers do +60% (cook-off) to a mech carrying one."""
        weapons = me["weapons"]
        for slot, weapon in enumerate(weapons):
            if weapon is None:
                return slot, 0
        others = [(WEAPON_VALUE.get(w["kind"], 0), slot) for slot, w in enumerate(weapons) if w["kind"] != "gun"]
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
            exposed = sum(self.world.clear(e, eye) for e in enemy_eyes)
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
                if any(w and w["kind"] == "lightning" for w in me["weapons"]):
                    ideal = 36
                elif any(kind in ("laser", "mortar") for kind in threats[0].get("weapons", ())):
                    ideal = min(145, max(66, distance(pos, foe)))
                else:
                    ideal = 66
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
        self.heading += self.side * 1.31
        radius = obs["self"]["stats"]["radius"]
        options = []
        for step in range(8):
            p = point_at(pos, self.heading + step * self.side * math.pi / 4, min(52, zone["next_radius"] * .55))
            if (distance(p, zone["next_center"]) < zone["next_radius"] * .9
                    and self.world.is_walkable(*p, radius)):
                options.append(p)
        if not options:
            return zone["next_center"]
        # Travel along buildings: a sniper who finds us there leaves a wall within a short dash.
        walled = [p for p in options if self.world.wall_distance(*p) <= radius + 6]
        self.last_explore = (walled or options)[0]
        return self.last_explore

    def finishing_drop(self, obs, threats):
        """Stay in an uncontested ring through the last moments of a full repair."""
        me = obs["self"]
        for drop in obs.get("drops", ()):
            if drop["lands_in"] > 0 or drop.get("holder") != me["id"]:
                continue
            left = drop["capture_s"] - drop["progress"]
            ring = drop["radius"]
            if not 0 < left <= .9 or distance(me["pos"], drop["pos"]) > ring:
                continue
            # A runner immediately outside can contest the clock before it completes.
            reach = ring + left * me["stats"]["boost_speed"] * 1.2
            if all(distance(m["pos"], drop["pos"]) > reach for m in threats):
                return drop["pos"]
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
                for m in threats[:2]]
        points = []
        for ring, count, offset in rings:
            # Wall-clock guard: near rings come first, and a late answer costs a second of coasting.
            if ring and time.perf_counter() > self.deadline:
                break
            for k in range(count):
                if ring:
                    p = point_at(pos, offset + k * math.tau / count, ring)
                    if not self.world.is_walkable(p[0], p[1], radius):
                        continue
                    if ring < 25 and not self.world.walk_clear(pos, p, radius):
                        continue
                    z = self.world.ground(*p)
                else:
                    p, z = (pos[0], pos[1]), pos[2]
                body = (p[0], p[1], z + 0.5 * stats["height"])
                center, any_part = set(), set()
                for i, gun in guns:
                    if self.world.clear(gun, body):
                        center.add(i)
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
        for m in threats[:2]:
            gun = with_height(m["pos"], self.config["mech"]["muzzle_height"] * self.body_scale(m))
            a = angle_to(gun, body) + math.pi / 2
            for side in (0, -stats["radius"], stats["radius"]):
                if self.world.clear(gun, (body[0] + side * math.cos(a), body[1] + side * math.sin(a), body[2])):
                    return False
        return True

    def hide_point(self, obs, threats, points=None):
        """Closest point no listed threat can hit, searching farther out if nothing is close.

        Keeps the spot already being run to while it still hides us: switching targets mid-dash
        throws away the momentum the 0.75 s laser warning depends on."""
        pos, now = obs["self"]["pos"], obs["t"]
        if (self.hide_spot is not None and now - self.hide_at < 1.5 and distance(pos, self.hide_spot) < 40
                and self.still_hidden(obs, threats, self.hide_spot)):
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
            hidden = [c for c in group if not c[2]]
            if hidden:
                self.hide_spot = tuple(min(hidden, key=lambda c: c[3] - 0.08 * distance(c[0], foe))[0][:2])
                self.hide_at = now
                return self.hide_spot
        return None

    def peek_point(self, obs, threats, ideal, escape_limit=7):
        """A firing position on the primary threat with cover a short dash away.

        A charging laser is visible for about 0.75 s before it fires; a walking mech covers
        about 6 m in that time. Returns (point or None, all candidate points)."""
        points = self.cover_points(obs, threats)
        hidden = [c[0] for c in points if not c[2]]
        primary = threats[0]
        radius = obs["self"]["stats"]["radius"]
        best, best_score = None, -math.inf
        for p, seen, _, ring in points:
            if primary["id"] not in seen:
                continue
            escape = min((distance(p, h) for h in hidden
                          if distance(p, h) <= escape_limit and self.world.walk_clear(p, h, radius)),
                         default=math.inf)
            if escape > escape_limit:
                continue
            score = (-0.5 * ring - 3.0 * escape - 0.06 * abs(distance(p, primary["pos"]) - ideal)
                     - 10 * (len(seen) - 1))
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

    def duel_range(self, obs, threat):
        me = obs["self"]
        held = {w["kind"] for w in me["weapons"] if w}
        if "lightning" in held:
            return 36
        if "laser" in held:
            return min(150, max(60, distance(me["pos"], threat["pos"])))
        return 80

    def plan(self, obs, threats, urgent, emergency, laser, shelter):
        me = obs["self"]
        now = obs["t"]
        eyes = with_height(me["pos"], me["stats"]["eye_height"])
        exposed = [m for m in threats if self.world.clear(eyes, with_height(m["pos"], 4.5 * (
            1 + self.config["cubes"]["size_per_cube"] * m.get("cubes", 0))))]
        if len(exposed) >= 2 or (exposed and me["hp"] < .46 * me["max_hp"]):
            self.hide_until = now + me["stats"]["regen_delay"] + 2
        if exposed and me["hp"] < .46 * me["max_hp"]:
            self.repairing = True
        if me["hp"] >= .85 * me["max_hp"]:
            self.repairing = False
        retreat = self.repairing or now < self.hide_until
        capture = self.finishing_drop(obs, threats)
        if capture and not urgent and not (laser and me["hp"] < .3 * me["max_hp"]):
            self.mode, self.goal = "capture", capture
        elif urgent:
            self.mode = "zone"
            self.goal = self.zone_goal(obs)
            if threats and distance(me["pos"], self.goal) > 20 and retreat and not emergency:
                self.goal = self.maneuver(obs, exposed or threats, True, True)
        elif laser:
            spot = self.hide_point(obs, [laser] + [m for m in threats if m["id"] != laser["id"]])
            self.mode = "evade laser"
            self.goal = spot if spot is not None else self.maneuver(obs, [laser], False, retreat, laser=True)
        elif shelter:
            # A charged laser can be held for 2 s waiting for us to step out. Stay hidden from
            # every charging mech until it fires or lets go.
            spot = self.hide_point(obs, shelter + [m for m in threats if m not in shelter])
            self.mode, self.goal = "cover", spot if spot is not None else tuple(me["pos"][:2])
        elif self.repairing and not exposed and now - self.combat.combat_time > 1:
            # The five-second tracking window can expire just as health starts regenerating.
            self.mode, self.goal = "repair", tuple(me["pos"][:2])
        elif threats and (exposed or retreat):
            targets = exposed or threats
            if retreat:
                self.mode = "retreat"
                if not exposed and me["hp"] < .88 * me["max_hp"]:
                    self.goal = tuple(me["pos"][:2])
                else:
                    spot = self.hide_point(obs, targets)
                    self.goal = spot if spot is not None else self.maneuver(obs, targets, False, True)
            else:
                self.mode = "duel"
                # Peek from right beside cover while a sniper's laser is ready; while it recharges
                # (1.65 s after a beam) a wider choice of firing positions is safe.
                ready = any("laser" in m.get("weapons", ()) and self.laser_cool_until.get(m["id"], -1) < now + .4
                            for m in targets)
                held = {w["kind"] for w in me["weapons"] if w}
                far_sniper = (held == {"gun"} and "laser" in targets[0].get("weapons", ())
                              and distance(me["pos"], targets[0]["pos"]) > GUN_ENGAGE)
                if far_sniper:
                    # Gun rounds take 0.5 s+ to cross 120 m against a strafing target; wait in
                    # cover for it to come closer instead of trading with a hitscan laser.
                    spot, points = None, None
                else:
                    spot, points = self.peek_point(obs, targets, self.duel_range(obs, targets[0]),
                                                   5 if ready else 8.5)
                if spot is None:
                    # No firing position with nearby cover: get behind something and let it come.
                    spot = self.hide_point(obs, targets, points)
                self.goal = spot if spot is not None else self.maneuver(obs, targets, False, False)
        elif not urgent and any("laser" in m.get("weapons", ()) and distance(me["pos"], m["pos"]) < 280
                                for m in threats):
            # A laser carrier we cannot currently see: close in through cover instead of
            # wandering into its line of fire. Hold position if no covered step gets closer.
            self.mode, self.goal = "stalk", self.stalk_point(obs, threats) or tuple(me["pos"][:2])
        else:
            loot = self.loot_options(obs)
            self.mode = "loot" if loot is not None else "explore"
            self.goal = loot if loot is not None else self.explore(obs)
        self.plan_at = now + (.3 if laser or threats else .7 if urgent else .9)

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
        jump = False
        if dist > 5 and self.stuck_since is not None and now - self.stuck_since > 1.6:
            if self.mode == "loot":
                self.unreachable_loot[tuple(self.goal[:2])] = now + 12
                self.plan_at = now
                self.stuck_since = now
                return [0, 0], False, False
            if self.mode == "explore":
                self.last_explore = None
                self.plan_at = now
            if now >= self.next_jump and me["grounded"] and me["energy"] > 42:
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
        me, now = obs["self"], obs["t"]
        self.listen(obs)
        enemy, aim, fire, reload_gun = self.combat.decide(obs, self.world)
        pos = me["pos"]
        threats = sorted((m for m in obs["mechs"] if m["kind"] == "mech"),
                         key=lambda m: distance(pos, m["pos"]))[:3]
        urgent = self.urgent_zone(obs)
        self.track_lasers(obs)
        laser = self.charged_laser(obs)
        rules = self.config["weapons"]["laser"]
        # Every visible mech charging a laser in range, aimed at us or not yet.
        shelter = [m for m in threats if m.get("charging")
                   and distance(pos, m["pos"]) <= rules["range"] + 3]
        finishing = any(d.get("holder") == me["id"] and 0 < d["capture_s"] - d["progress"] <= .9
                        for d in obs.get("drops", ()))
        ready = tuple(sorted(m["id"] for m in threats if "laser" in m.get("weapons", ())
                             and self.laser_cool_until.get(m["id"], -1) < now + .4))
        key = (urgent, tuple(m["id"] for m in threats), me["hp"] < me["max_hp"] * .46,
               finishing, laser and laser["id"], tuple(m["id"] for m in shelter), ready)
        if now >= self.plan_at or key != self.plan_key or self.goal is None:
            self.plan(obs, threats, urgent, not me["in_zone"], laser, shelter)
            self.plan_key = key
        fast_escape = self.mode == "evade laser" and distance(pos, self.goal) > 4
        boost = (fast_escape or ((urgent or self.mode == "retreat") and distance(pos, self.goal) > 8)) and me["energy"] > 26
        move, boost, jump = self.drive(obs, boost)
        # Sidestepping a 45-damage bullet must not carry us away from cover that stops a 315-damage beam.
        if not urgent and self.mode not in ("capture", "evade laser", "cover", "retreat"):
            move = self.avoid_projectile(obs, move)

        # If hidden and recovering, attacking a crate or drone would reset the repair timer.
        healing = self.mode in ("retreat", "repair") and me["hp"] < me["max_hp"] * .88
        focus = self.combat.focus
        if healing and (not focus or focus.get("kind") != "mech"):
            fire, aim = [False, False], None
        if self.mode == "evade laser" or (urgent and not me["in_zone"]):
            for slot, weapon in enumerate(me["weapons"]):
                if weapon and weapon["kind"] == "laser":
                    # Charging cuts ground speed in half while the other weapon remains usable.
                    fire[slot] = False
        if aim is None:
            if enemy:
                aim = [angle_to(pos, enemy["pos"]), 0]
            elif self.listen_until > now:
                aim = [self.last_bearing, 0]
            elif now % 4.5 < 1.7:
                # House bots share vision and know where we are; spin the torso for a full look
                # around so a sniper is spotted before its first beam, not after.
                aim = [math.remainder(me["aim"][0] + self.side * 1.4, math.tau), -.07]
            else:
                heading = angle_to(pos, self.goal) if distance(pos, self.goal) > 1 else self.heading
                aim = [math.remainder(heading + .98 * math.sin(.85 * now), math.tau), -.07]
        action = dict(move=move, boost=boost, jump=jump, aim=aim, fire=fire, reload=reload_gun)
        slot, held_value = self.weapon_slot(me)
        held = {w["kind"] for w in me["weapons"] if w}
        near = [p for p in obs["pickups"] if WEAPON_VALUE.get(p["kind"], 0) > held_value and p["kind"] not in held
                and distance(p["pos"], pos) <= self.config["loot"]["pickup_radius"]
                and abs(p["pos"][2] - pos[2]) <= 4]
        busy = slot is None or bool(me["weapons"][slot] and me["weapons"][slot].get("charge", 0) > 0)
        if near and not busy:
            action["take"] = max(near, key=lambda p: WEAPON_VALUE[p["kind"]])["id"]
            if me["weapons"][slot] is not None:
                action["drop"] = slot
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
