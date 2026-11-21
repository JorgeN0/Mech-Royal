"""Static map model: terrain heights, building grid, line of sight, and time-sliced A*.

Built once in Bot.__init__ from hello.map.dir (protocol.md section 11). Stdlib only.
Grid: C-metre cells over the terrain extent. Cell (i, j) centre = (minX + (i+.5)C, minY + (j+.5)C).
"""
import heapq
import json
import math
import os
from array import array

SQRT2 = math.sqrt(2.0)


class World:
    def __init__(self, map_dir, step_up=1.2, mech_height=9.0, cell=2.0):
        with open(os.path.join(map_dir, "v2", "world.json"), "r", encoding="utf-8") as f:
            wj = json.load(f)
        ter = wj["terrain"]
        self.tnx = int(ter["nx"])
        self.tny = int(ter["ny"])
        self.tminx = float(ter["minX"])
        self.tminy = float(ter["minY"])
        self.tstep = float(ter.get("step", 1.0))
        th = array("h")
        with open(os.path.join(map_dir, "v2", "terrain.i16"), "rb") as f:
            th.frombytes(f.read())
        if th.itemsize != 2:
            raise RuntimeError("int16 array itemsize")
        import sys
        if sys.byteorder != "little":
            th.byteswap()
        self.th = th  # centimetres

        C = self.C = float(cell)
        self.minx = self.tminx
        self.miny = self.tminy
        self.W = W = int(math.ceil(self.tnx * self.tstep / C))
        self.H = H = int(math.ceil(self.tny * self.tstep / C))
        self.maxx = self.minx + W * C
        self.maxy = self.miny + H * C
        n = W * H

        # ground height at each cell centre
        ground = array("f", bytes(4 * n))
        gat = self.ground_at
        for j in range(H):
            y = self.miny + (j + 0.5) * C
            row = j * W
            for i in range(W):
                ground[row + i] = gat(self.minx + (i + 0.5) * C, y)
        self.ground = ground

        # solids: buildings/stadium (bt/bb) and bridges (rt/rb); top < base means empty
        bt = array("f", [-1e9]) * n
        bb = array("f", [1e9]) * n
        rt = array("f", [-1e9]) * n
        rb = array("f", [1e9]) * n
        blocked = bytearray(n)
        clear = mech_height
        for p in wj.get("prisms", []):
            kind = p.get("kind", "building")
            base = float(p["base"])
            top = float(p["top"])
            rings = [p["ring"]] + list(p.get("holes") or [])
            for idx in self._raster(rings):
                g = ground[idx]
                if kind == "bridge":
                    if top > rt[idx]:
                        rt[idx] = top
                    if base < rb[idx]:
                        rb[idx] = base
                    # low bridge parts (abutments) block; decks you can walk under or onto do not
                    if base < g + clear and top - g > step_up and base > g + 0.5:
                        pass
                    continue
                if top > bt[idx]:
                    bt[idx] = top
                if base < bb[idx]:
                    bb[idx] = base
                if top - g > step_up and base < g + clear:
                    blocked[idx] = 1
        self.bt, self.bb, self.rt, self.rb = bt, bb, rt, rb
        self.solid = bytes(blocked)  # un-inflated building footprint

        # inflate by one cell so paths keep clearance for the 2.5 m mech radius; map edge blocked
        inf = bytearray(blocked)
        for j in range(H):
            row = j * W
            for i in range(W):
                if blocked[row + i]:
                    for dj in (-1, 0, 1):
                        jj = j + dj
                        if 0 <= jj < H:
                            r2 = jj * W
                            for di in (-1, 0, 1):
                                ii = i + di
                                if 0 <= ii < W:
                                    inf[r2 + ii] = 1
        for i in range(W):
            inf[i] = 1
            inf[(H - 1) * W + i] = 1
        for j in range(H):
            inf[j * W] = 1
            inf[j * W + W - 1] = 1
        self.blocked = inf
        self.extra = bytearray(n)  # cells marked blocked at runtime (stuck spots)

    # ---------------------------------------------------------------- terrain
    def ground_at(self, x, y):
        fx = (x - self.tminx) / self.tstep
        fy = (y - self.tminy) / self.tstep
        nx, ny = self.tnx, self.tny
        if fx < 0:
            fx = 0.0
        elif fx > nx - 1.001:
            fx = nx - 1.001
        if fy < 0:
            fy = 0.0
        elif fy > ny - 1.001:
            fy = ny - 1.001
        i = int(fx)
        j = int(fy)
        ax = fx - i
        ay = fy - j
        th = self.th
        k = j * nx + i
        h00 = th[k]
        h10 = th[k + 1]
        h01 = th[k + nx]
        h11 = th[k + nx + 1]
        return ((h00 * (1 - ax) + h10 * ax) * (1 - ay) + (h01 * (1 - ax) + h11 * ax) * ay) * 0.01

    # ---------------------------------------------------------------- grid helpers
    def cell_of(self, x, y):
        i = int((x - self.minx) / self.C)
        j = int((y - self.miny) / self.C)
        if i < 0:
            i = 0
        elif i >= self.W:
            i = self.W - 1
        if j < 0:
            j = 0
        elif j >= self.H:
            j = self.H - 1
        return j * self.W + i

    def center(self, idx):
        j, i = divmod(idx, self.W)
        return (self.minx + (i + 0.5) * self.C, self.miny + (j + 0.5) * self.C)

    def is_free(self, idx):
        return not self.blocked[idx] and not self.extra[idx]

    def free_xy(self, x, y):
        if x < self.minx or y < self.miny or x >= self.maxx or y >= self.maxy:
            return False
        return self.is_free(self.cell_of(x, y))

    def nearest_free(self, idx, max_r=12):
        if self.is_free(idx):
            return idx
        W, H = self.W, self.H
        j0, i0 = divmod(idx, W)
        for r in range(1, max_r + 1):
            best = None
            bd = 1e18
            for dj in range(-r, r + 1):
                for di in (-r, r) if abs(dj) != r else range(-r, r + 1):
                    i, j = i0 + di, j0 + dj
                    if 0 <= i < W and 0 <= j < H:
                        k = j * W + i
                        if self.is_free(k):
                            d = di * di + dj * dj
                            if d < bd:
                                bd, best = d, k
            if best is not None:
                return best
        return None

    def _raster(self, rings):
        """Cells whose centre is inside the polygon (even-odd over ring + holes)."""
        C, minx, miny, W, H = self.C, self.minx, self.miny, self.W, self.H
        edges = []
        ylo, yhi = 1e18, -1e18
        for r in rings:
            m = len(r) // 2
            if m < 3:
                continue
            for k in range(m):
                x0, y0 = r[2 * k], r[2 * k + 1]
                k2 = (k + 1) % m
                x1, y1 = r[2 * k2], r[2 * k2 + 1]
                if y0 != y1:
                    edges.append((x0, y0, x1, y1))
                ylo = min(ylo, y0)
                yhi = max(yhi, y0)
        if not edges:
            return
        j0 = max(0, int(math.floor((ylo - miny) / C - 0.5)))
        j1 = min(H - 1, int(math.ceil((yhi - miny) / C - 0.5)))
        for j in range(j0, j1 + 1):
            yc = miny + (j + 0.5) * C
            xs = []
            for x0, y0, x1, y1 in edges:
                if (y0 <= yc < y1) or (y1 <= yc < y0):
                    xs.append(x0 + (yc - y0) * (x1 - x0) / (y1 - y0))
            if len(xs) < 2:
                continue
            xs.sort()
            row = j * W
            for a in range(0, len(xs) - 1, 2):
                i0 = max(0, int(math.ceil((xs[a] - minx) / C - 0.5)))
                i1 = min(W - 1, int(math.floor((xs[a + 1] - minx) / C - 0.5)))
                for i in range(i0, i1 + 1):
                    yield row + i

    # ---------------------------------------------------------------- sight / shots
    def los(self, a, b, step=1.5):
        """True if the 3D segment a->b is not blocked by terrain, buildings or bridges."""
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        d = math.hypot(dx, dy)
        n = int(d / step)
        if n < 2:
            return True
        inv = 1.0 / n
        minx, miny, C, W, H = self.minx, self.miny, self.C, self.W, self.H
        ground, bt, bb, rt, rb = self.ground, self.bt, self.bb, self.rt, self.rb
        for k in range(1, n):
            f = k * inv
            x = a[0] + dx * f
            y = a[1] + dy * f
            z = a[2] + dz * f
            i = int((x - minx) / C)
            j = int((y - miny) / C)
            if i < 0 or j < 0 or i >= W or j >= H:
                return False
            c = j * W + i
            if z < ground[c] - 0.5:
                return False
            if bb[c] <= z <= bt[c]:
                return False
            if rb[c] <= z <= rt[c]:
                return False
        return True

    def walk_line(self, x0, y0, x1, y1, skip=3.0):
        """True if the straight walk x0,y0 -> x1,y1 crosses only free cells.
        The first `skip` metres are ignored so a mech hugging a wall can still leave it."""
        d = math.hypot(x1 - x0, y1 - y0)
        if d < 1e-6:
            return True
        n = int(d / (self.C * 0.5)) + 1
        k0 = int(skip / d * n) + 1 if d > skip else n
        for k in range(k0, n + 1):
            f = k / n
            if not self.free_xy(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f):
                return False
        return True


class AStar:
    """Weighted A* on the World grid that can be resumed across decisions."""

    MAX_EXPANSIONS = 80000

    def __init__(self, world, sx, sy, gx, gy, weight=1.5):
        self.w = world
        self.weight = weight
        self.state = "failed"
        self.path = None
        s = world.nearest_free(world.cell_of(sx, sy))
        g = world.nearest_free(world.cell_of(gx, gy), max_r=30)
        if s is None or g is None:
            return
        self.start, self.goal = s, g
        self.gj, self.gi = divmod(g, world.W)
        self.gcost = {s: 0.0}
        self.parent = {s: -1}
        self.open = [(self._h(s), s)]
        self.closed = set()
        self.expanded = 0
        self.state = "searching"

    def _h(self, k):
        j, i = divmod(k, self.w.W)
        dx = abs(i - self.gi)
        dy = abs(j - self.gj)
        return self.weight * ((dx + dy) + (SQRT2 - 2.0) * min(dx, dy))

    def step(self, deadline, clock):
        if self.state != "searching":
            return self.state
        w = self.w
        W, H = w.W, w.H
        blocked, extra, ground = w.blocked, w.extra, w.ground
        gcost, parent, openh, closed = self.gcost, self.parent, self.open, self.closed
        goal = self.goal
        nbrs = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                (1, 1, SQRT2), (1, -1, SQRT2), (-1, 1, SQRT2), (-1, -1, SQRT2))
        rise_o = 2.0  # max uphill rise between orthogonal cell centres (~50 deg over 2 m)
        rise_d = 2.8
        push, pop, hfun = heapq.heappush, heapq.heappop, self._h
        n = 0
        while openh:
            n += 1
            if (n & 63) == 0 and clock() > deadline:
                return "searching"
            _, cur = pop(openh)
            if cur in closed:
                continue
            if cur == goal:
                self._build(cur)
                return self.state
            closed.add(cur)
            self.expanded += 1
            if self.expanded > self.MAX_EXPANSIONS:
                self.state = "failed"
                return self.state
            cj, ci = divmod(cur, W)
            gc = gcost[cur]
            hc = ground[cur]
            for di, dj, cost in nbrs:
                ni, nj = ci + di, cj + dj
                if ni < 0 or nj < 0 or ni >= W or nj >= H:
                    continue
                nk = nj * W + ni
                if blocked[nk] or extra[nk] or nk in closed:
                    continue
                if cost > 1.0:
                    # no corner cutting
                    if blocked[cj * W + ni] or blocked[nj * W + ci]:
                        continue
                    if ground[nk] - hc > rise_d:
                        continue
                elif ground[nk] - hc > rise_o:
                    continue
                ng = gc + cost
                if ng < gcost.get(nk, 1e18):
                    gcost[nk] = ng
                    parent[nk] = cur
                    push(openh, (ng + hfun(nk), nk))
        self.state = "failed"
        return self.state

    def _build(self, k):
        pts = []
        parent = self.parent
        while k != -1:
            pts.append(self.w.center(k))
            k = parent[k]
        pts.reverse()
        # drop collinear points
        out = [pts[0]]
        for a in range(1, len(pts) - 1):
            p0, p1, p2 = out[-1], pts[a], pts[a + 1]
            if abs((p1[0] - p0[0]) * (p2[1] - p1[1]) - (p1[1] - p0[1]) * (p2[0] - p1[0])) > 1e-6:
                out.append(p1)
        if len(pts) > 1:
            out.append(pts[-1])
        self.path = out
        self.state = "found"
        # free memory
        self.gcost = self.parent = self.open = self.closed = None
