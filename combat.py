"""Aim and weapon decisions using the game observations and the static arena."""
import math


def yaw_error(a, b):
    return abs(math.remainder(a - b, math.tau))


def angles(origin, point):
    dx, dy, dz = (point[i] - origin[i] for i in range(3))
    return math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))


class Combat:
    def __init__(self, hello):
        self.rules = hello["config"]
        self.sim_step = 1 / self.rules["match"]["tick_hz"]
        self.decision_step = self.sim_step * self.rules["match"]["decide_every"]
        self.focus = None
        self.attacker_times = {}
        self.combat_time = -100.0
        self.last_enemy_time = -100.0

    def _stats(self, weapon):
        return weapon.get("stats") or self.rules["weapons"][weapon["kind"]]

    def _body(self, obj):
        kind = obj.get("kind", "crate")
        if kind == "mech":
            scale = 1 + self.rules["cubes"]["size_per_cube"] * obj.get("cubes", 0)
            base = self.rules["mech"]
            return base["radius"] * scale, base["height"] * scale
        if kind == "npc":
            npc = self.rules["npc"]
            return npc["radius"], npc["height"]
        crate = self.rules["crates"]
        return crate.get("radius", 2.5), crate.get("height", 3.0)

    def _select(self, obs, world, muzzle, reach):
        me, now = obs["self"], obs["t"]
        candidates = []
        enemies = []
        old_id = self.focus["id"] if self.focus else None
        for obj in (*obs["mechs"], *obs.get("crates", ())):
            pos = obj["pos"]
            d = math.dist(pos, me["pos"])
            kind = obj.get("kind", "crate")
            hp = obj.get("hp", 1400)
            threatening = False
            if kind == "mech":
                enemies.append((d, obj))
                priority = d + 0.045 * hp - 105
                recently_hit = now - self.attacker_times.get(obj["id"], -100) < 2.5
                if recently_hit:
                    priority -= 55
                if d <= 75:
                    dx, dy = me["pos"][0] - pos[0], me["pos"][1] - pos[1]
                    vx, vy = obj.get("vel", (0, 0, 0))[:2]
                    closing = dx * vx + dy * vy > 3 * max(1, math.hypot(dx, dy))
                    facing = obj.get("aim")
                    watching = facing and yaw_error(facing[0], math.atan2(dy, dx)) < math.pi / 3
                    threatening = recently_hit or d < 30 or closing or watching
            else:
                # Farming only pays when the drop can reasonably be collected.
                if d > (68 if kind == "crate" else 55):
                    continue
                if me["hp"] < me["max_hp"] * 0.82 and now - self.combat_time > 2:
                    continue
                priority = d + (7 if kind == "crate" else 20) + 0.025 * hp
            if d > reach + 3:
                continue
            priority += 8 * yaw_error(me["aim"][0], math.atan2(pos[1]-muzzle[1], pos[0]-muzzle[0]))
            if obj["id"] == old_id:
                priority -= 12
            candidates.append((0 if threatening else 1, priority, obj))
        closest = min(enemies, key=lambda e: e[0])[1] if enemies else None
        candidates.sort(key=lambda e: (e[0], e[1]))
        for _, _, obj in candidates[:5]:
            radius, height = self._body(obj)
            x, y, z = obj["pos"]
            point = (x, y, z + 0.5 * height)
            if not world.clear(muzzle, point):
                # An enemy behind cover can still be reported for five seconds.
                point = (x, y, z + 0.83 * height)
                if obj.get("kind") != "mech" or not world.clear(muzzle, point):
                    continue
            rival = obj if obj.get("kind") == "mech" else closest
            return obj, point, radius, height, rival
        return None, None, 0, 0, closest

    def _mortar(self, obs, obj, mortar, world):
        me = obs["self"]
        stats = self._stats(mortar)
        tube = (*me["pos"][:2], me["pos"][2] + me["stats"]["height"] + 0.5)
        x, y, z = obj["pos"]
        vx, vy, vz = obj.get("vel", (0, 0, 0))
        distance = math.hypot(x - tube[0], y - tube[1])
        if not 16 < distance < 125 or abs(vz) > 5:
            return None
        # Within the low-angle envelope, the game's travel-time estimate converges quickly.
        flight = max(0.05, (distance - 3) / stats["speed"])
        for _ in range(4):
            tx, ty = x + vx * flight, y + vy * flight
            tz = z + 1.0 + vz * min(flight, 0.2)
            raised = tz + 0.5 * stats["gravity"] * flight**2
            aim = angles(tube, (tx, ty, raised))
            travel = math.dist(tube, (tx, ty, raised))
            flight = max(0.05, (travel - 3) / stats["speed"])
        if not me["stats"]["min_pitch"] <= aim[1] <= me["stats"]["max_pitch"]:
            return None
        # Sample the virtual shell arc so a nearby eave does not absorb the shot.
        previous = tube
        for fraction in (0.25, 0.5, 0.75, 1):
            sample = (tube[0] + (tx - tube[0]) * fraction,
                      tube[1] + (ty - tube[1]) * fraction,
                      tube[2] + (tz - tube[2]) * fraction
                      + 0.5 * stats["gravity"] * flight**2 * fraction * (1 - fraction))
            if not world.clear(previous, sample):
                return None
            previous = sample
        return aim

    def decide(self, obs, world):
        me, now = obs["self"], obs["t"]
        for event in obs.get("events", ()):
            if event["type"] in ("damaged", "hit"):
                self.combat_time = now
            if event["type"] == "damaged" and event.get("by") is not None:
                self.attacker_times[event["by"]] = now
        muzzle = (*me["pos"][:2], me["pos"][2] + me["stats"]["muzzle_height"])
        weapons = {w["kind"]: (slot, w) for slot, w in enumerate(me["weapons"]) if w}
        gun = weapons.get("gun", (None, None))[1]
        reach = max((self._stats(w).get("range", 125) for _, w in weapons.values()), default=0)
        target, point, radius, height, enemy = self._select(obs, world, muzzle, reach)
        self.focus = target
        if target and target.get("kind") == "mech":
            self.last_enemy_time = now
        triggers = [False, False]
        reload = bool(gun and not gun.get("reload") and gun.get("ammo", 1) == 0)
        if not target:
            # Letting go of a charging laser fires it. If the target just ducked out of the
            # line of fire, keep holding (the game auto-fires after hold_s at full charge)
            # rather than dumping the shot into a wall.
            laser = weapons.get("laser")
            if laser and laser[1].get("charge", 0) > 0 and enemy is not None:
                triggers[laser[0]] = True
            if gun and now - self.last_enemy_time > 0.6:
                reload |= gun.get("ammo", 12) < self._stats(gun)["mag"] * 0.8
            return enemy, None, triggers, reload

        # Estimate where each weapon wants the shared torso to point at its next shot.
        speed = target.get("vel", (0, 0, 0))
        dist = max(1, math.dist(muzzle, point))
        dt = self.sim_step
        shooter = tuple(p + v * dt for p, v in zip(muzzle, me["vel"]))
        next_point = tuple(p + v * dt for p, v in zip(point, speed))
        direct = angles(shooter, next_point)
        solutions = {}
        aim = direct
        track_point = next_point
        if gun and dist <= self._stats(gun)["range"] + 3:
            stats = self._stats(gun)
            flight = max(0, dist - 3) / stats["speed"]
            for _ in range(2):
                lead = tuple(p + v * flight for p, v in zip(next_point, speed))
                flight = max(0, math.dist(shooter, lead) - 3) / stats["speed"]
            if math.dist(shooter, lead) <= stats["range"] + 3 and world.clear(muzzle, lead):
                solutions["gun"] = angles(shooter, lead)
                aim = solutions["gun"]
                track_point = lead

        mortar = weapons.get("mortar", (None, None))[1]
        if mortar and mortar["cooldown"] <= self.decision_step:
            mortar_aim = self._mortar(obs, target, mortar, world)
            if mortar_aim is not None:
                solutions["mortar"] = aim = mortar_aim
                track_point = None

        laser = weapons.get("laser", (None, None))[1]
        release = False
        if laser and dist <= self._stats(laser)["range"] + 3:
            stats = self._stats(laser)
            charge = laser.get("charge", 0)
            ratio = charge / stats["charge"]
            damage = stats["damage"] * (stats["min_frac"] + (1 - stats["min_frac"]) * min(ratio, 1))
            damage *= 1 + self.rules["cubes"]["damage_per_cube"] * min(me["cubes"], self.rules["cubes"]["stat_cap"])
            release = charge >= stats["charge"] - 0.001 or (
                charge >= stats["min_charge"] and damage >= 1.15 * target.get("hp", 1400))
            if laser["cooldown"] <= self.decision_step or charge > 0:
                solutions["laser"] = direct
                if release or charge + self.decision_step >= stats["charge"]:
                    aim = direct
                    track_point = next_point

        current_yaw, current_pitch = me["aim"]
        if track_point is not None:
            stats = me["stats"]
            yaw_time = yaw_error(current_yaw, aim[0]) / max(0.01, stats["turn_rate"])
            pitch_time = abs(current_pitch - aim[1]) / max(0.01, stats["pitch_rate"])
            # While the torso crosses a large angle, a strafing target can move
            # past a corner. Recompute each observation, using only bullet travel
            # lead once the torso is within one decision of the firing direction.
            extra_time = min(0.25, max(yaw_time, pitch_time) - self.decision_step)
            if extra_time > 0:
                future_shooter = tuple(p + v * extra_time for p, v in zip(shooter, me["vel"]))
                future_point = tuple(p + v * extra_time for p, v in zip(track_point, speed))
                future_yaw, future_pitch = angles(future_shooter, future_point)
                delta_yaw = math.remainder(future_yaw - aim[0], math.tau)
                aim = (math.remainder(aim[0] + max(-0.3, min(0.3, delta_yaw)), math.tau),
                       max(stats["min_pitch"], min(stats["max_pitch"], future_pitch)))
        # The torso can already be catching up on the next angular sample. Limit
        # that allowance to half a body width so a large retarget cannot waste a round.
        yaw_tolerance = math.atan2(radius * 0.95, max(1, math.dist(shooter[:2], point[:2])))
        pitch_tolerance = math.atan2(height * 0.37, dist)
        for kind, (slot, w) in weapons.items():
            if kind == "lightning":
                stats = self._stats(w)
                cone = math.radians(stats["cone_deg"])
                triggers[slot] = (dist <= stats["range"] and me["energy"] >= stats["energy"]
                                  and yaw_error(current_yaw, direct[0]) <= cone
                                  and yaw_error(aim[0], direct[0]) <= cone)
                continue
            wanted = solutions.get(kind)
            if wanted is None:
                continue
            extra = math.atan2(2.5, dist) if kind == "mortar" else 0
            aligned = (yaw_error(current_yaw, wanted[0]) <= yaw_tolerance * 1.4 + extra
                       and abs(current_pitch - wanted[1]) <= pitch_tolerance * 1.2 + extra
                       and yaw_error(aim[0], wanted[0]) <= yaw_tolerance + extra
                       and abs(aim[1] - wanted[1]) <= pitch_tolerance + extra)
            if kind == "laser":
                triggers[slot] = not (release and aligned)
            elif kind == "gun":
                triggers[slot] = aligned and not w.get("reload") and w.get("ammo", 0) > 0
            elif kind == "mortar":
                triggers[slot] = aligned
        return enemy, list(aim), triggers, reload
