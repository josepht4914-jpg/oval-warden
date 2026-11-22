"""Static arena geometry, ground clearance and bounded route planning."""

from array import array
from collections import defaultdict
from heapq import heappop, heappush
import json
import math
from pathlib import Path
import sys


def _direction(start, end):
    dx, dy = end[0] - start[0], end[1] - start[1]
    distance = math.hypot(dx, dy)
    return (dx / distance, dy / distance) if distance > 1e-6 else (0.0, 0.0)


class _Prism:
    def __init__(self, data):
        self.base, self.top = data["base"], data["top"]
        ring = data["ring"]
        self.bounds = (min(ring[::2]), min(ring[1::2]), max(ring[::2]), max(ring[1::2]))
        self.edges = []
        self.bands = defaultdict(list)
        for flat in [ring, *data.get("holes", [])]:
            points = list(zip(flat[::2], flat[1::2]))
            for a, b in zip(points, points[1:] + points[:1]):
                if a == b:
                    continue
                edge = (*a, b[0] - a[0], b[1] - a[1])
                self.edges.append(edge)
                for band in range(math.floor(min(a[1], b[1]) / 12), math.floor(max(a[1], b[1]) / 12) + 1):
                    self.bands[band].append(edge)

    def crosses(self, x, y, z, dx, dy, dz):
        # Clip to the prism's 3-D bounding box, then test its actual walls and holes.
        lo, hi = 0.0, 1.0
        x0, y0, x1, y1 = self.bounds
        for value, delta, low, high in ((x, dx, x0, x1), (y, dy, y0, y1), (z, dz, self.base, self.top)):
            if abs(delta) < 1e-10:
                if value < low or value > high:
                    return False
            else:
                t0, t1 = (low - value) / delta, (high - value) / delta
                if t1 < t0:
                    t0, t1 = t1, t0
                lo, hi = max(lo, t0), min(hi, t1)
                if lo > hi:
                    return False

        px, py = x + lo * dx, y + lo * dy
        inside = False
        for ex, ey, sx, sy in self.bands.get(math.floor(py / 12), ()):
            if (ey > py) != (ey + sy > py) and px < ex + (py - ey) * sx / sy:
                inside = not inside
        if inside:
            return True
        for ex, ey, sx, sy in self.edges:
            cross = dx * sy - dy * sx
            if abs(cross) < 1e-9:
                continue
            wx, wy = ex - x, ey - y
            t = (wx * sy - wy * sx) / cross
            if lo - 1e-8 <= t <= hi + 1e-8 and 0 <= (wx * dy - wy * dx) / cross <= 1:
                return True
        return False


class World:
    """Shot visibility uses solid geometry. Ground routes use a 2 m clearance raster."""

    def __init__(self, hello):
        folder = Path(hello["map"]["dir"]) / "v2"
        with (folder / "world.json").open() as stream:
            world = json.load(stream)
        terrain = world["terrain"]
        self.x0, self.y0 = terrain["minX"], terrain["minY"]
        self.nx, self.ny = terrain["nx"], terrain["ny"]
        self.step, self.scale = terrain["step"], terrain.get("scale", 0.01)
        self.heights = array("h", (folder / terrain["file"]).read_bytes())
        if sys.byteorder != "little":
            self.heights.byteswap()
        self.terrain_ceiling = {}
        min_tile_x, min_tile_y = math.floor(self.x0 / 20), math.floor(self.y0 / 20)
        max_tile_x = math.floor((self.x0 + (self.nx - 1) * self.step) / 20)
        max_tile_y = math.floor((self.y0 + (self.ny - 1) * self.step) / 20)
        for ty in range(min_tile_y, max_tile_y + 1):
            j0 = max(0, math.floor((ty * 20 - self.y0) / self.step))
            j1 = min(self.ny - 1, math.ceil(((ty + 1) * 20 - self.y0) / self.step))
            for tx in range(min_tile_x, max_tile_x + 1):
                i0 = max(0, math.floor((tx * 20 - self.x0) / self.step))
                i1 = min(self.nx - 1, math.ceil(((tx + 1) * 20 - self.x0) / self.step))
                self.terrain_ceiling[tx, ty] = self.scale * max(
                    max(self.heights[j*self.nx+i0:j*self.nx+i1+1]) for j in range(j0, j1+1)
                )
        self.prisms = [_Prism(p) for p in world["prisms"]]
        self.tiles = defaultdict(list)
        for prism in self.prisms:
            x0, y0, x1, y1 = prism.bounds
            for iy in range(math.floor(y0 / 20), math.floor(y1 / 20) + 1):
                for ix in range(math.floor(x0 / 20), math.floor(x1 / 20) + 1):
                    self.tiles[ix, iy].append(prism)
        self._build_ground_grid(hello["config"]["mech"])
        self._profiles = {}
        # Every clearance class the mech can grow into (radius +5% per cube, a new class about every
        # second cube), filled now: a flood fill inside a decision costs most of the 50 ms budget.
        first = self._minimum_clearance(hello["config"]["mech"].get("radius", 2.5))
        for limit in range(min(first, 14), max(first, 26) + 1):
            self._components(limit)
        self._route = []
        self._goal = None
        self._route_radius = 0

    def ground(self, x, y):
        x = max(0.0, min(self.nx - 1.000001, (x - self.x0) / self.step))
        y = max(0.0, min(self.ny - 1.000001, (y - self.y0) / self.step))
        ix, iy = int(x), int(y)
        fx, fy = x - ix, y - iy
        k = iy * self.nx + ix
        h = self.heights
        low = h[k] + fx * (h[k + 1] - h[k])
        high = h[k + self.nx] + fx * (h[k + self.nx + 1] - h[k + self.nx])
        return (low + fy * (high - low)) * self.scale

    def clear(self, a, b):
        """A straight line of fire. Terrain is sampled every metre; foliage passes shots."""
        x, y, z = a
        dx, dy, dz = b[0] - x, b[1] - y, b[2] - z
        distance = math.hypot(dx, dy)
        # Grid traversal visits every spatial bin hit by the segment, including narrow slivers.
        ix, iy = math.floor(x / 20), math.floor(y / 20)
        end_x, end_y = math.floor(b[0] / 20), math.floor(b[1] / 20)
        sx, sy = (1 if dx >= 0 else -1), (1 if dy >= 0 else -1)
        tx = ((ix + (sx > 0)) * 20 - x) / dx if dx else math.inf
        ty = ((iy + (sy > 0)) * 20 - y) / dy if dy else math.inf
        step_x, step_y = (20 / abs(dx) if dx else math.inf), (20 / abs(dy) if dy else math.inf)
        seen = set()
        entered = 0.0
        while True:
            for prism in self.tiles.get((ix, iy), ()):
                if prism not in seen:
                    seen.add(prism)
                    if prism.crosses(x, y, z, dx, dy, dz):
                        return False
            leave = min(1.0, tx if ix != end_x else math.inf, ty if iy != end_y else math.inf)
            low_z = z + (entered if dz >= 0 else leave) * dz
            if low_z < self.terrain_ceiling.get((ix, iy), math.inf):
                steps = max(1, math.ceil((leave - entered) * distance))
                for i in range(steps + 1):
                    t = entered + (leave - entered) * i / steps
                    if z + t * dz < self.ground(x + t * dx, y + t * dy) - 0.02:
                        return False
            if ix == end_x and iy == end_y:
                break
            entered = leave
            if ix != end_x and (tx < ty or iy == end_y):
                ix, tx = ix + sx, tx + step_x
            else:
                iy, ty = iy + sy, ty + step_y
        return True

    def _build_ground_grid(self, mech):
        spacing = 2
        self.fw = width = 1 + int((self.nx - 1) * self.step / spacing)
        self.fh = height = 1 + int((self.ny - 1) * self.step / spacing)
        heights = array("f", [0.0]) * (width * height)
        clearance = bytearray([252]) * len(heights)
        scale = self.scale
        max_gradient = 2 * math.tan(math.radians(mech.get("max_slope_deg", 50))) * self.step / scale
        slope_limit = max_gradient * max_gradient
        for j in range(height):
            y = self.y0 + j * spacing
            iy = round((y - self.y0) / self.step)
            for i in range(width):
                x = self.x0 + i * spacing
                ix = round((x - self.x0) / self.step)
                k, h = i + j * width, ix + iy * self.nx
                heights[k] = self.heights[h] * scale
                if i == 0 or i == width - 1 or j == 0 or j == height - 1:
                    clearance[k] = 0
                else:
                    gx = self.heights[h + 1] - self.heights[h - 1]
                    gy = self.heights[h + self.nx] - self.heights[h - self.nx]
                    if gx * gx + gy * gy > slope_limit:
                        clearance[k] = 0

        # Rasterize each polygon by paired scanline intersections, including courtyards.
        step_up, mech_height = mech.get("step_up", 1.2), mech.get("height", 9)
        for prism in self.prisms:
            x0, y0, x1, y1 = prism.bounds
            for j in range(max(0, math.ceil((y0 - self.y0) / spacing)), min(height, math.ceil((y1 - self.y0) / spacing))):
                y = self.y0 + j * spacing
                cuts = sorted(ex + (y - ey) * sx / sy for ex, ey, sx, sy in prism.bands.get(math.floor(y / 12), ())
                              if (ey > y) != (ey + sy > y))
                for left, right in zip(cuts[::2], cuts[1::2]):
                    start = max(0, math.ceil((left - self.x0) / spacing))
                    end = min(width, math.floor((right - self.x0) / spacing) + 1)
                    for k in range(j * width + start, j * width + end):
                        if prism.base - mech_height < heights[k] < prism.top - step_up:
                            clearance[k] = 0
            # Catch thin walls even when they fall between grid centres.
            for ex, ey, sx, sy in prism.edges:
                count = max(1, math.ceil(math.hypot(sx, sy) / 1.5))
                for step in range(count + 1):
                    i = round((ex + sx * step / count - self.x0) / spacing)
                    j = round((ey + sy * step / count - self.y0) / spacing)
                    if 0 <= i < width and 0 <= j < height:
                        k = j * width + i
                        if prism.base - mech_height < heights[k] < prism.top - step_up:
                            clearance[k] = 0

        # A quarter-metre chamfer distance field permits radius changes after cube pickups.
        for j in range(1, height - 1):
            for k in range(j * width + 1, (j + 1) * width - 1):
                if clearance[k]:
                    clearance[k] = min(clearance[k], clearance[k-1] + 8, clearance[k-width] + 8,
                                       clearance[k-width-1] + 11, clearance[k-width+1] + 11)
        for j in range(height - 2, 0, -1):
            for k in range((j + 1) * width - 2, j * width, -1):
                if clearance[k]:
                    clearance[k] = min(clearance[k], clearance[k+1] + 8, clearance[k+width] + 8,
                                       clearance[k+width-1] + 11, clearance[k+width+1] + 11)
        self.clearance = clearance

        # Coarser route nodes share the fine raster. Each edge stores the tightest passage.
        self.nw, self.nh = (width + 2) // 3, (height + 2) // 3
        self.neighbors = [[] for _ in range(self.nw * self.nh)]
        for j in range(self.nh):
            for i in range(self.nw):
                n = j * self.nw + i
                k = (j * width + i) * 3
                if clearance[k] == 0:
                    continue
                for di, dj in ((1,0), (0,1), (1,1), (-1,1)):
                    ni, nj = i + di, j + dj
                    if not (0 <= ni < self.nw and 0 <= nj < self.nh):
                        continue
                    stride = di + dj * width
                    limit = min(clearance[k], clearance[k+stride], clearance[k+2*stride], clearance[k+3*stride])
                    if di and dj:
                        for offset in (0, stride, 2 * stride):
                            limit = min(limit, clearance[k+offset+di], clearance[k+offset+dj*width])
                    if limit < 10:
                        continue
                    other = nj * self.nw + ni
                    cost = 1.41421356 if di and dj else 1
                    self.neighbors[n].append((other, cost, limit))
                    self.neighbors[other].append((n, cost, limit))

    @staticmethod
    def _minimum_clearance(radius):
        return math.ceil((radius + 0.85) * 4)

    def is_walkable(self, x, y, radius=2.5):
        i, j = math.floor((x - self.x0) / 2 + 0.5), math.floor((y - self.y0) / 2 + 0.5)
        return (0 <= i < self.fw and 0 <= j < self.fh and
                self.clearance[j * self.fw + i] >= self._minimum_clearance(radius))

    def wall_distance(self, x, y):
        """Metres from (x, y) to the nearest wall, steep slope or map edge (capped at 63 m)."""
        i, j = math.floor((x - self.x0) / 2 + 0.5), math.floor((y - self.y0) / 2 + 0.5)
        if not (0 <= i < self.fw and 0 <= j < self.fh):
            return 0.0
        return self.clearance[j * self.fw + i] / 4

    def walk_clear(self, a, b, radius=2.5):
        x, y = (a[0] - self.x0) / 2 + 0.5, (a[1] - self.y0) / 2 + 0.5
        dx, dy = (b[0] - a[0]) / 2, (b[1] - a[1]) / 2
        i, j = math.floor(x), math.floor(y)
        end_i, end_j = math.floor(x + dx), math.floor(y + dy)
        if not (0 <= i < self.fw and 0 <= j < self.fh and 0 <= end_i < self.fw and 0 <= end_j < self.fh):
            return False
        sx, sy = (1 if dx >= 0 else -1), (1 if dy >= 0 else -1)
        tx = ((i + (sx > 0)) - x) / dx if dx else math.inf
        ty = ((j + (sy > 0)) - y) / dy if dy else math.inf
        dt_x, dt_y = (1 / abs(dx) if dx else math.inf), (1 / abs(dy) if dy else math.inf)
        limit = self._minimum_clearance(radius)
        field, fw = self.clearance, self.fw
        while field[j * fw + i] >= limit:
            if i == end_i and j == end_j:
                return True
            if i != end_i and (tx < ty or j == end_j):
                i, tx = i + sx, tx + dt_x
            else:
                j, ty = j + sy, ty + dt_y
        return False

    def _xy(self, node):
        return self.x0 + 6 * (node % self.nw), self.y0 + 6 * (node // self.nw)

    def _components(self, limit):
        if limit not in self._profiles:
            labels = array("i", [-1]) * len(self.neighbors)
            for node, links in enumerate(self.neighbors):
                if labels[node] >= 0 or not any(width >= limit for _, _, width in links):
                    continue
                labels[node] = node
                stack = [node]
                while stack:
                    current = stack.pop()
                    for other, _, width in self.neighbors[current]:
                        if width >= limit and labels[other] == -1:
                            labels[other] = node
                            stack.append(other)
            self._profiles[limit] = labels
        return self._profiles[limit]

    def _nearest_node(self, point, limit, component=None):
        x, y = point[:2]
        i = min(self.nw - 1, max(0, round((x - self.x0) / 6)))
        j = min(self.nh - 1, max(0, round((y - self.y0) / 6)))
        labels = self._components(limit)
        for r in range(26):
            best, distance = None, math.inf
            for nj in range(max(0, j-r), min(self.nh, j+r+1)):
                left, right = max(0, i-r), min(self.nw-1, i+r)
                cols = range(left, right+1) if abs(nj-j) == r else (left, right)
                for ni in cols:
                    node = nj * self.nw + ni
                    if component is not None and labels[node] != component:
                        continue
                    if labels[node] < 0 or self.clearance[3 * (nj * self.fw + ni)] < limit:
                        continue
                    cx, cy = self._xy(node)
                    d = (cx-x)**2 + (cy-y)**2
                    if d < distance:
                        best, distance = node, d
            if best is not None:
                return best
        # A small isolated courtyard may have no node near the distant destination.
        if component is not None:
            return min(
                (n for n, label in enumerate(labels) if label == component),
                key=lambda n: (self.x0 + n % self.nw * 6 - x) ** 2 + (self.y0 + n // self.nw * 6 - y) ** 2,
                default=None,
            )
        return None

    def _path(self, start, goal, limit):
        gi, gj = goal % self.nw, goal // self.nw
        def estimate(node):
            di, dj = abs(node % self.nw - gi), abs(node // self.nw - gj)
            return max(di, dj) + 0.41421356 * min(di, dj)

        frontier = [(estimate(start), 0, start)]
        parents, costs = {}, {start: 0}
        best, best_dist = start, estimate(start)
        closed = 0
        while frontier and closed < 1450:
            _, cost, node = heappop(frontier)
            if cost != costs[node]:
                continue
            closed += 1
            dist = estimate(node)
            if dist < best_dist:
                best, best_dist = node, dist
            if node == goal:
                break
            for other, length, width in self.neighbors[node]:
                if width < limit:
                    continue
                new_cost = cost + length
                if new_cost < costs.get(other, math.inf):
                    costs[other], parents[other] = new_cost, node
                    heappush(frontier, (new_cost + 1.22 * estimate(other), new_cost, other))
        path = []
        while best != start:
            path.append(self._xy(best))
            best = parents[best]
        path.append(self._xy(start))
        path.reverse()
        return path

    def move_toward(self, pos, goal, radius=2.5):
        """World-frame ground movement. An inaccessible goal resolves to a walkable approach."""
        if math.dist(pos[:2], goal[:2]) < 0.3:
            return 0.0, 0.0
        if self.walk_clear(pos, goal, radius):
            return _direction(pos, goal)
        if len(pos) > 2 and len(goal) > 2 and pos[2] > self.ground(*pos[:2]) + 4:
            z = min(pos[2], goal[2]) + 1
            if abs(pos[2] - goal[2]) < 2 and self.clear((*pos[:2], z), (*goal[:2], z)):
                return _direction(pos, goal)
        limit = self._minimum_clearance(radius)
        if (not self._route or self._route_radius != limit or self._goal is None
                or math.dist(self._goal, goal[:2]) > 7 or math.dist(pos[:2], self._route[0]) > 56):
            start = self._nearest_node(pos, limit)
            component = self._components(limit)[start] if start is not None else None
            end = self._nearest_node(goal, limit, component)
            self._route_end = self._xy(end) if end is not None else None
            self._goal, self._route_radius = tuple(goal[:2]), limit
            self._route = self._path(start, end, limit) if start is not None and end is not None else []
        if not self._route:
            return _direction(pos, goal)
        # Advance past reached nodes, then smooth turns as far ahead as we can see clear ground.
        while len(self._route) > 1 and math.dist(pos[:2], self._route[0]) < 1.2 and self.walk_clear(pos, self._route[1], radius):
            self._route.pop(0)
        for i in range(min(5, len(self._route) - 1), 0, -1):
            if self.walk_clear(pos, self._route[i], radius):
                del self._route[:i]
                break
        waypoint = self._route[0]
        if len(self._route) == 1 and math.dist(pos[:2], waypoint) < 1.2:
            if waypoint != self._route_end:
                self._route.clear()
            return 0.0, 0.0
        return _direction(pos, waypoint)
