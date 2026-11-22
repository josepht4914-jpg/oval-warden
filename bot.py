"""Oval Warden, a placement-focused mechfight/1 bot."""
import math

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
        self.last_laser = None
        self.laser_cover_until = -1
        self.zone_phase = {}

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
        if None in me["weapons"]:
            for weapon in obs["pickups"]:
                value = {"laser": 53, "lightning": 50, "mortar": 40}.get(weapon["kind"])
                if value:
                    consider(weapon, value, 105)
        for crate in obs["crates"]:
            consider(crate, 24, 54)
        for drop in obs.get("drops", []):
            contested = drop.get("holder") not in (None, me["id"])
            if not contested and drop["lands_in"] < 4 and (me["cubes"] < 10 or me["hp"] < me["max_hp"] * .8):
                consider(drop, 57 - 2 * drop["lands_in"], 90)
        return max(options, key=lambda item: item[0])[1] if options else None

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
        for step in range(8):
            p = point_at(pos, self.heading + step * self.side * math.pi / 4, min(52, zone["next_radius"] * .55))
            if (distance(p, zone["next_center"]) < zone["next_radius"] * .9
                    and self.world.is_walkable(*p, obs["self"]["stats"]["radius"])):
                self.last_explore = p
                return p
        return zone["next_center"]

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
            self.mode, self.goal = "evade laser", self.maneuver(obs, [laser], False, retreat, laser=True)
        elif shelter and not exposed:
            self.mode, self.goal = "cover", tuple(me["pos"][:2])
        elif self.repairing and not exposed and now - self.combat.combat_time > 1:
            # The five-second tracking window can expire just as health starts regenerating.
            self.mode, self.goal = "repair", tuple(me["pos"][:2])
        elif threats and (exposed or retreat):
            self.mode = "retreat" if retreat else "duel"
            resting = retreat and not exposed and me["hp"] < .88 * me["max_hp"]
            self.goal = tuple(me["pos"][:2]) if resting else self.maneuver(obs, exposed or threats, False, retreat)
        else:
            loot = self.loot_options(obs)
            self.mode = "loot" if loot is not None else "explore"
            self.goal = loot if loot is not None else self.explore(obs)
        self.plan_at = now + (.3 if laser else .7 if threats or urgent else .9)

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

    def act(self, obs):
        me, now = obs["self"], obs["t"]
        self.listen(obs)
        enemy, aim, fire, reload_gun = self.combat.decide(obs, self.world)
        pos = me["pos"]
        threats = sorted((m for m in obs["mechs"] if m["kind"] == "mech"),
                         key=lambda m: distance(pos, m["pos"]))[:3]
        urgent = self.urgent_zone(obs)
        laser = self.charged_laser(obs)
        if laser:
            self.last_laser = laser["id"]
            rules = self.config["weapons"]["laser"]
            self.laser_cover_until = now + rules["charge"] + rules.get("hold_s", 2)
        shelter = now < self.laser_cover_until and any(
            m["id"] == self.last_laser and m.get("charging") for m in obs["mechs"])
        finishing = any(d.get("holder") == me["id"] and 0 < d["capture_s"] - d["progress"] <= .9
                        for d in obs.get("drops", ()))
        key = (urgent, tuple(m["id"] for m in threats), me["hp"] < me["max_hp"] * .46,
               finishing, laser and laser["id"], shelter)
        if now >= self.plan_at or key != self.plan_key or self.goal is None:
            self.plan(obs, threats, urgent, not me["in_zone"], laser, shelter)
            self.plan_key = key
        fast_escape = self.mode == "evade laser" and distance(pos, self.goal) > 4
        boost = (fast_escape or ((urgent or self.mode == "retreat") and distance(pos, self.goal) > 8)) and me["energy"] > 26
        move, boost, jump = self.drive(obs, boost)
        if not urgent and self.mode != "capture":
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
            else:
                heading = angle_to(pos, self.goal) if distance(pos, self.goal) > 1 else self.heading
                aim = [math.remainder(heading + .98 * math.sin(.85 * now), math.tau), -.07]
        action = dict(move=move, boost=boost, jump=jump, aim=aim, fire=fire, reload=reload_gun)
        if None in me["weapons"]:
            held = {w["kind"] for w in me["weapons"] if w}
            near = [p for p in obs["pickups"] if p["kind"] != "gun" and p["kind"] not in held
                    and distance(p["pos"], pos) <= self.config["loot"]["pickup_radius"]
                    and abs(p["pos"][2] - pos[2]) <= 4]
            if near:
                choice = max(near, key=lambda p: {"laser": 3, "lightning": 2, "mortar": 1}.get(p["kind"], 0))
                action["take"] = choice["id"]
        if obs.get("offer"):
            priority = ("plating", "nanites", "gun_mag", "armor", "gun_damage", "servos", "laser_damage",
                        "gun_rate", "lightning_range", "laser_charge", "lightning_efficiency", "gun_velocity",
                        "gyros", "mortar_reload", "reactor", "sensors")
            choices = obs["offer"]["choices"]
            action["pick"] = min(range(len(choices)), key=lambda i: priority.index(choices[i]["id"])
                                 if choices[i]["id"] in priority else len(priority))
        return action


if __name__ == "__main__":
    mechfight.run(Bot)
