"""Offline checks for world.py: grid stats, A* timing on random pairs, and a PNG of the nav grid.

usage: python tools/navtest.py [out.png]
"""
import math
import os
import random
import struct
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from world import World, AStar  # noqa: E402


def png(path, w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb[j * w * 3:(j + 1) * w * 3]) for j in range(h))

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(data)


def main():
    root = os.path.dirname(HERE)
    t0 = time.perf_counter()
    w = World(os.path.join(root, "arena"))
    print("build %.2fs grid %dx%d cell %.1f" % (time.perf_counter() - t0, w.W, w.H, w.C))
    n = w.W * w.H
    print("solid cells %d (%.1f%%), blocked(inflated) %d (%.1f%%)" % (
        sum(w.solid), 100.0 * sum(w.solid) / n, sum(w.blocked), 100.0 * sum(w.blocked) / n))
    print("ground range %.1f .. %.1f" % (min(w.ground), max(w.ground)))

    rng = random.Random(1)
    free = [k for k in range(n) if w.is_free(k)]
    times, fails, lens = [], 0, []
    for _ in range(40):
        a = w.center(rng.choice(free))
        b = w.center(rng.choice(free))
        t1 = time.perf_counter()
        s = AStar(w, a[0], a[1], b[0], b[1])
        st = s.step(time.perf_counter() + 5.0, time.perf_counter)
        dt = time.perf_counter() - t1
        times.append(dt * 1000)
        if st != "found":
            fails += 1
        else:
            L = sum(math.dist(s.path[i], s.path[i + 1]) for i in range(len(s.path) - 1))
            lens.append(L / max(1.0, math.dist(a, b)))
    times.sort()
    print("A* 40 random pairs: p50 %.0f ms p90 %.0f ms max %.0f ms, fails %d, path/straight p50 %.2f" % (
        times[20], times[36], times[-1], fails, sorted(lens)[len(lens) // 2] if lens else 0))

    # LOS sanity: ray through the middle of a big building should be blocked
    t1 = time.perf_counter()
    cnt = 0
    for _ in range(2000):
        a = w.center(rng.choice(free))
        b = w.center(rng.choice(free))
        if math.dist(a, b) < 200:
            w.los((a[0], a[1], w.ground_at(*a) + 8), (b[0], b[1], w.ground_at(*b) + 4.5))
            cnt += 1
    print("LOS: %d rays < 200 m in %.1f ms (%.3f ms each)" % (cnt, (time.perf_counter() - t1) * 1000,
                                                           (time.perf_counter() - t1) * 1000 / max(1, cnt)))

    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "tools", "navgrid.png")
    gmin, gmax = min(w.ground), max(w.ground)
    rgb = bytearray(n * 3)
    for j in range(w.H):
        for i in range(w.W):
            k = j * w.W + i
            o = ((w.H - 1 - j) * w.W + i) * 3  # north up
            if w.solid[k]:
                rgb[o:o + 3] = b"\xd0\x40\x40"
            elif w.blocked[k]:
                rgb[o:o + 3] = b"\x90\x70\x40"
            else:
                g = int(40 + 180 * (w.ground[k] - gmin) / max(1e-6, gmax - gmin))
                rgb[o:o + 3] = bytes((g // 3, g, g // 3))
            if w.rt[k] > -1e8 and not w.solid[k]:
                rgb[o:o + 3] = b"\x40\x80\xff"
    png(out, w.W, w.H, rgb)
    print("wrote", out)


if __name__ == "__main__":
    main()
