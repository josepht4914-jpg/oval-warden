"""Small deterministic game scenarios that the kit's fixed replay cannot exercise."""
import copy
import gzip
import json
import math
import unittest
from pathlib import Path
from unittest.mock import patch

import bot


ROOT = Path(__file__).resolve().parents[1]


class FlatArena:
    """Flat floor, optionally with a thin solid wall between x=35 and x=36."""
    def __init__(self, hello):
        self.wall = False

    def clear(self, a, b):
        return not self.wall or max(a[0], b[0]) < 35 or min(a[0], b[0]) > 36

    def ground(self, x, y):
        return 0

    def is_walkable(self, x, y, radius=2.5):
        return True

    def walk_clear(self, a, b, radius=2.5):
        return True

    def wall_distance(self, x, y):
        return 63.0

    def move_toward(self, pos, goal, radius):
        dx, dy = goal[0] - pos[0], goal[1] - pos[1]
        d = math.hypot(dx, dy)
        return (dx/d, dy/d) if d else (0, 0)


class Decisions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with gzip.open(ROOT / "kit/check/stream.jsonl.gz", "rt") as data:
            cls.hello = json.loads(next(data))
            cls.frame = json.loads(next(data))

    def setUp(self):
        with patch.object(bot, "World", FlatArena):
            self.pilot = bot.Bot(self.hello)
        self.obs = copy.deepcopy(self.frame)
        self.me = self.obs["self"]
        self.me.update(pos=[0, 0, 0], aim=[0, 0], vel=[0, 0, 0])
        self.obs["zone"].update(center=[0, 0], next_center=[0, 0], radius=250, next_radius=180)
        self.rival = dict(id=1, kind="mech", pos=[60, 0, 0], vel=[0, 11.7, 0], aim=[0, 0],
                          hp=1400, cubes=0, weapons=["gun"], charging=False)

    def test_weapon_choices_build_laser_and_lightning(self):
        # House lasers do +60% to a mech carrying a mortar, so a mortar is never taken.
        rules = self.hello["config"]["weapons"]
        gun = dict(kind="gun", cooldown=0, ammo=12, reload=0, stats=rules["gun"])

        def held(kind, charge=0):
            return dict(kind=kind, cooldown=0, charge=charge, stats=rules[kind])

        self.obs.update(mechs=[], crates=[], cubes=[])
        # Target loadout is laser + lightning: the gun (slot 0) is given up for the second of them.
        cases = ((None, "mortar", None), (None, "laser", (8001, None)), (held("mortar"), "lightning", (8001, 1)),
                 (held("lightning"), "laser", (8001, 0)), (held("laser"), "lightning", (8001, 0)),
                 (held("laser", charge=.3), "laser", None), (held("laser"), "mortar", None))
        for second, pickup, expected in cases:
            self.me["weapons"] = [gun, second]
            self.obs["pickups"] = [dict(id=8001, kind=pickup, pos=[1, 0, 0])]
            action = self.pilot.act(self.obs)
            got = (action["take"], action.get("drop")) if "take" in action else None
            self.assertEqual(got, expected, f"holding {second and second['kind']}, {pickup} at our feet")

    def test_stays_out_of_sight_while_a_laser_is_held_charged(self):
        # A wall at x=35..36 hides us at x>36 from a sniper at x=-100. While it holds a charge,
        # the bot must not step back out to peek, even though it could shoot from there.
        with patch.object(bot, "World", FlatArena):
            pilot = bot.Bot(self.hello)
        pilot.world.wall = True
        self.me["pos"] = [38, 0, 0]
        self.rival.update(pos=[-100, 0, 0], vel=[0, 0, 0], aim=[0, 0], weapons=["gun", "laser"], charging=True)
        self.obs["mechs"] = [self.rival]
        for step in range(8):
            self.obs.update(t=step / 10, tick=2 * step)
            action = pilot.act(self.obs)
            self.assertNotEqual(pilot.mode, "duel")
            self.assertLessEqual(action["move"][0], 0.05 if self.me["pos"][0] > 36.5 else 1)

    def test_turn_and_lead_a_crossing_mech(self):
        self.me["aim"] = [-math.pi/2, 0]
        self.obs["mechs"] = [self.rival]
        first_shot = None
        for step in range(14):
            self.obs["t"], self.obs["tick"] = .1 * step, 2 * step
            self.rival["pos"][1] = 11.7 * self.obs["t"]
            action = self.pilot.act(self.obs)
            if action["fire"][0]:
                self.assertGreater(action["aim"][0], bot.angle_to(self.me["pos"], self.rival["pos"]))
                first_shot = self.obs["t"]
                break
            for axis, rate in enumerate((4, 3)):
                error = math.remainder(action["aim"][axis] - self.me["aim"][axis], math.tau)
                self.me["aim"][axis] += max(-.1 * rate, min(.1 * rate, error))
        self.assertIsNotNone(first_shot)
        self.assertGreaterEqual(first_shot, .3)
        self.assertLess(first_shot, .8)

    def test_hidden_mech_blocks_fire_and_allows_repair(self):
        self.obs["mechs"] = [self.rival]
        self.me["hp"] = 400
        self.pilot.act(self.obs)
        self.pilot.world.wall = True
        self.obs["t"], self.obs["tick"] = 1., 20
        action = self.pilot.act(self.obs)
        self.assertEqual(action["fire"], [False, False])
        self.assertEqual(action["move"], [0, 0])
        self.obs.update(t=7., tick=140, mechs=[], crates=[dict(id=1001, pos=[5, 0, 0], hp=50)])
        action = self.pilot.act(self.obs)
        self.assertEqual(self.pilot.mode, "repair")
        self.assertEqual(action["fire"], [False, False])
        self.me["hp"] = self.me["max_hp"]
        self.obs.update(t=8., tick=160)
        self.pilot.act(self.obs)
        self.assertEqual(self.pilot.mode, "loot")

    def test_cover_and_zone_priorities(self):
        self.obs["mechs"] = [self.rival, dict(self.rival, id=2, pos=[65, 10, 0])]
        self.pilot.act(self.obs)
        self.assertEqual(self.pilot.mode, "retreat")
        self.me["pos"] = [260, 0, 0]
        self.me["in_zone"] = False
        self.obs["pickups"] = [dict(id=8010, kind="laser", pos=[275, 0, 0])]
        self.obs["t"], self.obs["tick"] = .1, 2
        action = self.pilot.act(self.obs)
        self.assertEqual(self.pilot.mode, "zone")
        self.assertLess(action["move"][0], -.8)
        self.assertTrue(action["boost"])

    def test_pickups_obey_horizontal_and_vertical_reach(self):
        self.obs["pickups"] = [dict(id=8010, kind="laser", pos=[0, 0, 7]),
                               dict(id=8011, kind="lightning", pos=[2, 0, 0])]
        self.assertEqual(self.pilot.act(self.obs)["take"], 8011)

    def test_movement_brakes_and_dodge_keeps_lateral_momentum(self):
        self.pilot.goal = (2, 0)
        self.me["vel"] = [19.5, 0, 0]
        self.assertFalse(self.pilot.drive(self.obs, True)[1])
        self.me["vel"] = [0, 3, 0]
        self.obs["projectiles"] = [dict(id=700, kind="gun", pos=[100, 1, 5], vel=[-252, 0, 0])]
        move = self.pilot.avoid_projectile(self.obs, [1, 0])
        self.assertGreater(move[1], .9)
        self.obs["t"] = .1
        self.assertEqual(self.pilot.avoid_projectile(self.obs, [-1, 0]), move)

    def test_imminent_supply_drop_repair(self):
        self.me["hp"] = 400
        self.obs["mechs"] = [self.rival]
        self.obs["drops"] = [dict(id=20000, pos=[0, 0, 0], radius=8, lands_in=0,
                                  capture_s=4, progress=3.6, holder=self.me["id"])]
        action = self.pilot.act(self.obs)
        self.assertEqual(self.pilot.mode, "capture")
        self.assertEqual(action["move"], [0, 0])
        self.rival["pos"] = [10, 0, 0]
        self.assertIsNone(self.pilot.finishing_drop(self.obs, [self.rival]))

    def test_charged_laser_crosses_the_beam_and_keeps_full_speed(self):
        self.rival.update(pos=[100, 0, 0], aim=[math.pi, 0], weapons=["gun", "laser"], charging=True)
        self.obs["mechs"] = [self.rival]
        self.me["weapons"][1] = dict(kind="laser", stats=self.hello["config"]["weapons"]["laser"],
                                     cooldown=0, charge=0)
        action = self.pilot.act(self.obs)
        self.assertEqual(self.pilot.mode, "evade laser")
        self.assertGreater(abs(action["move"][1]), .85)
        self.assertFalse(action["fire"][1])
        self.pilot.world.wall = True
        self.assertIsNone(self.pilot.charged_laser(self.obs))

    def test_cross_into_building_cover_on_the_campus_map(self):
        hello = copy.deepcopy(self.hello)
        hello["map"]["dir"] = str(ROOT / "kit/arena")
        pilot = bot.Bot(hello)
        ground = pilot.world.ground
        start, foe = (-212, -39), (-107, -39)
        self.me["pos"] = [*start, ground(*start)]
        self.rival.update(pos=[*foe, ground(*foe)], vel=[0, 0, 0], aim=[math.pi, 0],
                          weapons=["gun", "laser"], charging=True)
        self.obs["mechs"] = [self.rival]
        self.obs["zone"].update(center=list(start), next_center=list(start), radius=560, next_radius=380)
        muzzle = bot.with_height(self.rival["pos"], 7)
        self.assertTrue(pilot.world.clear(muzzle, bot.with_height(self.me["pos"], 4.5)))
        for step in range(10):
            self.obs.update(t=step/10, tick=2*step)
            action = pilot.act(self.obs)
            stats = self.me["stats"]
            speed = stats["boost_speed" if action["boost"] else "walk_speed"]
            for _ in range(2):
                vx, vy = self.me["vel"][:2]
                ax, ay = (speed * u - v for u, v in zip(action["move"], (vx, vy)))
                ramp = min(1, .05 * stats["ground_accel"] / max(1e-9, math.hypot(ax, ay)))
                vx, vy = vx + ax * ramp, vy + ay * ramp
                x, y = self.me["pos"][:2]
                dest = (x + vx * .05, y + vy * .05)
                if pilot.world.walk_clear(self.me["pos"], dest):
                    self.me.update(pos=[*dest, ground(*dest)], vel=[vx, vy, 0])
                else:
                    self.me["vel"] = [0, 0, 0]
            if not pilot.world.clear(muzzle, bot.with_height(self.me["pos"], 4.5)):
                return
        self.fail("The mech stayed exposed to the charged laser for the entire second")


if __name__ == "__main__":
    unittest.main()
