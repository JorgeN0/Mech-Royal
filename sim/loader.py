"""Load bot classes from files (in-process) and wrap them as match controllers."""
import copy
import hashlib
import importlib.util
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import mechfight  # noqa: E402

mechfight.log = lambda *a: None  # bots log to stderr; keep sim output clean


def clone_world(w):
    c = copy.copy(w)
    c.extra = bytearray(len(w.extra))
    return c


_CACHE = {}


def parse_spec(spec):
    """'bot.py:pref_gun=140,ambush=0' -> (abs path, {param: value})."""
    path, _, rest = spec.partition(":")
    params = {}
    for kv in filter(None, rest.split(",")):
        k, v = kv.split("=")
        params[k] = float(v)
    return os.path.abspath(path), params


def load_bot_class(spec, shared_world=None):
    if spec in _CACHE:
        return _CACHE[spec]
    path, params = parse_spec(spec)
    name = "simbot_" + hashlib.md5(spec.encode()).hexdigest()[:8]
    mspec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(mspec)
    captured = []
    orig = mechfight.run
    mechfight.run = lambda cls: captured.append(cls)
    try:
        mspec.loader.exec_module(mod)
    finally:
        mechfight.run = orig
    cls = captured[0] if captured else getattr(mod, "Bot")
    if shared_world is not None and hasattr(mod, "World") and mod.World is not None:
        mod.World = lambda *a, **k: clone_world(shared_world)
    if params:
        if not hasattr(mod, "PARAMS"):
            raise ValueError("%s has no PARAMS to override" % path)
        unknown = set(params) - set(mod.PARAMS)
        if unknown:
            raise ValueError("unknown params %s" % unknown)
        mod.PARAMS.update(params)
    _CACHE[spec] = cls
    return cls


class BotCtrl:
    kind = "bot"

    def __init__(self, cls, hello, name):
        self.name = name
        self.bot = cls(hello)
        self.worst_ms = 0.0
        self.total_ms = 0.0
        self.n = 0

    def act(self, obs):
        mechfight.draw.clear()
        t0 = time.perf_counter()
        a = self.bot.act(obs)
        ms = (time.perf_counter() - t0) * 1000.0
        mechfight.draw.take()
        self.n += 1
        self.total_ms += ms
        self.slow = getattr(self, "slow", {})
        for lim in (10, 20, 35, 50):
            if ms > lim:
                self.slow[lim] = self.slow.get(lim, 0) + 1
        if ms > 20:
            k = getattr(self.bot, "mode", "?")
            self.slow_modes = getattr(self, "slow_modes", {})
            self.slow_modes[k] = self.slow_modes.get(k, 0) + 1
        if ms > self.worst_ms:
            self.worst_ms = ms
            self.worst_at = (round(obs["t"], 1), getattr(self.bot, "mode", "?"))
        return a
