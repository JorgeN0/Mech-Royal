"""Tree canopy and trunk sight blocking for the simulator (protocol.md section 8; trees never stop shots).

Crown: ellipsoid centred over (crownX, crownY) between z+crownBase and z+height, horizontal radius crownR.
A sight line is blocked when exp(-canopyDensity * metres-of-crown-crossed) < canopyBlockT, or when it
passes through a trunk (radius trunkR, from z to z+crownBase). This is our approximation of the real rule.
"""
import json
import math
import os

BUCKET = 20.0


class TreeIndex:
    def __init__(self, map_dir):
        with open(os.path.join(map_dir, "v2", "world.json"), "r", encoding="utf-8") as f:
            wj = json.load(f)
        t = wj["trees"]
        fl = t["fields"]
        d = t["data"]
        n = len(fl)
        ix = {k: fl.index(k) for k in fl}
        rules = wj.get("rules", {})
        self.density = float(rules.get("canopyDensity", 0.35))
        self.block_t = float(rules.get("canopyBlockT", 0.25))
        self.max_len = -math.log(self.block_t) / self.density  # crown metres that block sight
        self.trees = []
        self.grid = {}
        for i in range(0, len(d), n):
            r = d[i:i + n]
            z = r[ix["z"]]
            cb = z + r[ix["crownBase"]]
            top = z + r[ix["height"]]
            cx = r[ix["crownX"]] if "crownX" in ix else r[ix["x"]]
            cy = r[ix["crownY"]] if "crownY" in ix else r[ix["y"]]
            tr = (r[ix["x"]], r[ix["y"]], z, cb, top, r[ix["crownR"]], r[ix["trunkR"]], cx, cy,
                  (cb + top) * 0.5, max(0.5, (top - cb) * 0.5))
            k = len(self.trees)
            self.trees.append(tr)
            reach = max(r[ix["crownR"]], 1.0)
            for gx in range(int(math.floor((min(cx, r[ix["x"]]) - reach) / BUCKET)),
                            int(math.floor((max(cx, r[ix["x"]]) + reach) / BUCKET)) + 1):
                for gy in range(int(math.floor((min(cy, r[ix["y"]]) - reach) / BUCKET)),
                                int(math.floor((max(cy, r[ix["y"]]) + reach) / BUCKET)) + 1):
                    self.grid.setdefault((gx, gy), []).append(k)

    def _candidates(self, a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy)
        n = max(1, int(L / (BUCKET * 0.5)))
        seen = set()
        out = []
        for s in range(n + 1):
            f = s / n
            key = (int(math.floor((a[0] + dx * f) / BUCKET)), int(math.floor((a[1] + dy * f) / BUCKET)))
            for k in self.grid.get(key, ()):
                if k not in seen:
                    seen.add(k)
                    out.append(k)
        return out

    def blocks(self, a, b):
        """True if trees block sight along the segment a -> b (3D points)."""
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        seg = math.sqrt(dx * dx + dy * dy + dz * dz)
        if seg < 1e-6:
            return False
        L2 = dx * dx + dy * dy
        acc = 0.0
        for k in self._candidates(a, b):
            tx, ty, z0, cb, top, cr, trr, cx, cy, cz, ch = self.trees[k]
            # trunk: vertical cylinder from the ground to the crown base
            if L2 > 1e-9:
                s = ((tx - a[0]) * dx + (ty - a[1]) * dy) / L2
                if 0.0 < s < 1.0:
                    px, py = a[0] + dx * s - tx, a[1] + dy * s - ty
                    if px * px + py * py < trr * trr:
                        zz = a[2] + dz * s
                        if z0 <= zz <= cb:
                            return True
            # crown: chord through the ellipsoid, in unit-sphere space
            ox, oy, oz = (a[0] - cx) / cr, (a[1] - cy) / cr, (a[2] - cz) / ch
            ux, uy, uz = dx / cr, dy / cr, dz / ch
            qa = ux * ux + uy * uy + uz * uz
            qb = 2.0 * (ox * ux + oy * uy + oz * uz)
            qc = ox * ox + oy * oy + oz * oz - 1.0
            disc = qb * qb - 4.0 * qa * qc
            if disc <= 0.0:
                continue
            sq = math.sqrt(disc)
            t1 = max(0.0, (-qb - sq) / (2.0 * qa))
            t2 = min(1.0, (-qb + sq) / (2.0 * qa))
            if t2 > t1:
                acc += (t2 - t1) * seg
                if acc >= self.max_len:
                    return True
        return False
