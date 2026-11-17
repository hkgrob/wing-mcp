#!/usr/bin/env python3
"""
WING profile: the command map, the writable set, and write validation.

Turns a raw node map produced by probe/wing_map.py into the profile the server
loads. Everything the server is allowed to do to the desk is decided here.

The profile is generated off the hardware, never transcribed. Its only external
input is the exclusion policy below, which names capabilities rather than
addresses wherever it can.

    python wing_profile.py build ../outputs/WING-Profile_<fw>_<stamp>.json
    python wing_profile.py show                     # list every profile it can load
    python wing_profile.py check /ch/1 name=Fiddle  # validate without sending
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# The profile the bundle has always shipped: generated on a WING Rack,
# firmware 3.1-0. It stays here as the fallback of last resort.
DEFAULT_PROFILE = os.path.join(HERE, "wing_profile.json")

# Profiles are per MODEL. The full WING, the Compact and the Rack share one
# firmware and almost all of one address tree, but the hardware-bound parts
# (local I/O, talkback, GPIO, surface controls) are each desk's own, so a
# profile names the model it was walked on and the server picks by the model
# the connected desk reports in discovery.
#
# Search order for a model's profile:
#   1. <user dir>/profiles/<model>.json   - generated on your own desk; lives
#      outside the bundle so reinstalling or upgrading never overwrites it
#   2. <bundle>/server/profiles/<model>.json - shipped with a release
#   3. <bundle>/server/wing_profile.json  - the Rack profile, flagged as a
#      model mismatch when the desk is not a Rack
# The user dir is $WING_MCP_HOME if set, else ~/.wing-mcp on every platform.
USER_DIR = os.environ.get("WING_MCP_HOME") or os.path.join(
    os.path.expanduser("~"), ".wing-mcp")
USER_PROFILES = os.path.join(USER_DIR, "profiles")
BUNDLED_PROFILES = os.path.join(HERE, "profiles")


# What discovery's model field and /$syscfg/$cnsmdl report for each desk,
# per Patrick-Gilles Maillot's "WING Remote Protocols" (V 3.1.2-a): the
# full-size desk is "ngc-full" (or "wing-bk"), not "wing"; a full-size desk on firmware 3.1.2 reports "wing-fullsize" in discovery (seen 2026-11-17). Hardware differs
# where it shows: the full desk has 8 local Midas preamps against 24 on the
# Rack and Compact, 2x2 GPIO against 1x2 on the Compact, and the Compact has
# its own layer/user-control nodes. Unknown strings still work - they just
# display as themselves.
MODEL_NAMES = {
    "wing-fullsize": "WING (full size)",
    "ngc-full": "WING (full size)",
    "wing-bk": "WING (full size, black)",
    "wing": "WING (full size)",
    "wing-rack": "WING Rack",
    "wing-compact": "WING Compact",
}


def model_name(model):
    return MODEL_NAMES.get(model_slug(model), model or "unknown")


def model_slug(model):
    """A discovery model string as a safe file stem: 'wing-rack' stays
    'wing-rack'; anything odd collapses to dashes."""
    s = re.sub(r"[^a-z0-9]+", "-", (model or "").strip().lower()).strip("-")
    return s or "unknown"


def profile_path_for(model, write=False):
    """Where a model's profile is, or where a new one for it goes
    (write=True: always the user dir)."""
    name = model_slug(model) + ".json"
    if write:
        return os.path.join(USER_PROFILES, name)
    for d in (USER_PROFILES, BUNDLED_PROFILES):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


def list_profiles():
    """Every profile this install can load: path, where it came from."""
    out = []
    for origin, d in (("user", USER_PROFILES), ("bundled", BUNDLED_PROFILES)):
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn.endswith(".json"):
                    out.append({"model": fn[:-5], "origin": origin,
                                "path": os.path.join(d, fn)})
    if os.path.exists(DEFAULT_PROFILE):
        out.append({"model": "wing-rack", "origin": "bundled-default",
                    "path": DEFAULT_PROFILE})
    return out


# --------------------------------------------------------------- exclusions
#
# Confirmed against the full 5,980-node map of firmware 3.1-0 on 2026-08-29.
# A parameter matching any rule here is never writable, whatever the desk says.

EXCLUDED = [
    # Destroys recorded material on the WLIVE SD cards.
    (r"^/cards/wlive/\d+/\$ctl$", {"formatsdcard", "deletesession", "deletemarker"},
     "destroys recorded material or the card itself"),

    # Recalls a scene, which reconfigures the whole desk in one write.
    (r"^/\$ctl/lib$", {"$action", "$actionidx"},
     "scene recall - reconfigures the entire desk"),

    # Transport. Stopping a running recording loses the tail of it.
    (r"^/rec$", {"$action", "$actionidx"}, "recorder transport"),
    (r"^/play$", {"$action", "$actionidx"}, "player transport"),

    # Console-wide save behaviour. Effect not established on hardware, so out.
    (r"^/\$ctl/\$globals$", {"$savenow", "$noautosave"},
     "console-wide save behaviour, effect unverified"),

    # Navigation on the recorder - harmless but pointless, and it is an action.
    (r"^/cards/wlive/\d+/\$ctl$", {"gotomarker"}, "recorder navigation"),

    # v0.4.0: the desk's own network identity. A write here can take the
    # desk off the network mid-show, and the only fix is at the surface.
    # Found cross-checking the profile against "WING Remote Protocols"
    # V 3.1.2-a, where every one of these is listed as settable.
    (r"^/\$syscfg$", {"ip0", "ip1", "ip2", "ip3", "msk0", "msk1", "msk2", "msk3",
                      "gw0", "gw1", "gw2", "gw3", "ipmode", "$ipapply", "eth_cfg"},
     "desk network settings - a bad write drops the desk off the network"),

    # Locks: either would lock remote control out until someone at the
    # surface turns it back off.
    (r"^/\$syscfg$", {"tcplock"}, "locks out TCP remote control (WING Edit etc.)"),
    (r"^/\$ctl/(OSC|osc)$", {"ronly"}, "makes OSC read-only, locking this server out"),
]

# Exclusion counts before v0.4.0 were 15; docs that still say "fifteen"
# refer to the original list.

# Parameters whose absence from a push would silently produce an invisible
# result. See the project CLAUDE.md: a channel with clink=1 renders its
# source's identity, not its own.
IDENTITY_KEYS = ("name", "col", "icon")
CLINK_KEY = "clink"


def excluded_reason(node, param):
    for pattern, names, reason in EXCLUDED:
        if param in names and re.match(pattern, node):
            return reason
    return None


# ------------------------------------------------------------ type handling

_RANGE_INT = re.compile(r"^int \[\s*(-?\d+)\s*\.\.\s*(-?\d+)\s*\]")
_RANGE_LIST = re.compile(r"^list \[(.*)\]\s*$")
_RANGE_STR = re.compile(r"^string \[(\d+)\]")
_RANGE_NUM = re.compile(r"^(lin|log|fader)\s*\[")


def parse_type(t):
    """Turn the desk's type string into something checkable."""
    t = (t or "").strip()
    m = _RANGE_INT.match(t)
    if m:
        return {"kind": "int", "min": int(m.group(1)), "max": int(m.group(2))}
    m = _RANGE_LIST.match(t)
    if m:
        return {"kind": "list",
                "options": [o.strip() for o in m.group(1).split(",")]}
    m = _RANGE_STR.match(t)
    if m:
        return {"kind": "string", "maxlen": int(m.group(1))}
    if _RANGE_NUM.match(t):
        # fader carries -oo as a legal value, so these are checked loosely:
        # the desk clamps, and a wrong number here is recoverable.
        return {"kind": "number", "raw": t}
    return {"kind": "opaque", "raw": t}


# The desk writes frequencies in its own notation - 14k78, 1k50, 12k00, 10k0 -
# and hands those values straight back on a dump. A value the desk emitted for a
# parameter is legal for that parameter by construction, so the checker accepts
# the desk's notation rather than demanding a plain decimal. Rejecting 14k78 was
# the checker being wrong about the desk, not the desk being wrong.
_NUMBER = re.compile(r"^[-+]?(\d+(\.\d+)?|\d+k\d+|-oo)$")


def validate(spec, value):
    """Returns None if acceptable, else a reason string."""
    k = spec["kind"]
    v = str(value)
    if k == "int":
        if not re.match(r"^-?\d+$", v):
            return "expected an integer"
        n = int(v)
        if not (spec["min"] <= n <= spec["max"]):
            return "out of range %d..%d" % (spec["min"], spec["max"])
    elif k == "list":
        if v not in spec["options"]:
            return "not one of %s" % ", ".join(spec["options"])
    elif k == "string":
        if len(v) > spec["maxlen"]:
            return "longer than %d characters" % spec["maxlen"]
    elif k == "number":
        if v != "-oo" and not _NUMBER.match(v):
            return "expected a number, the desk's k notation, or -oo"
        bounds = _number_bounds(spec.get("raw", ""))
        if bounds and v != "-oo":
            from wing_values import _to_number
            x = _to_number(v)
            lo, hi = bounds
            if x is not None and not (lo - 1e-6 <= x <= hi + 1e-6):
                return "out of range %s" % _RANGE_TEXT.search(spec["raw"]).group(1)
    return None


_RANGE_TEXT = re.compile(r"\[(.*?)\]")


def _number_bounds(raw):
    """(lo, hi) from 'lin [-15.0 .. +15.0 dB], 301 steps', 'log [20.0 ..
    20k00 Hz]' or 'fader [-oo .. 10.0 dB]' (-oo as -inf). None when the
    range can't be read, so validation stays permissive rather than wrong."""
    m = _RANGE_TEXT.search(raw or "")
    if not m or ".." not in m.group(1):
        return None
    from wing_values import _parse_bound  # noqa: F401  (lazy: avoid cycles)
    a, b = [x.strip() for x in m.group(1).split("..", 1)]
    lo, hi = _parse_bound(a), _parse_bound(b)
    if lo is None or hi is None:
        return None
    return lo, hi


# ----------------------------------------------------------------- building

def build(map_path, out_path=None):
    with open(map_path, encoding="utf-8") as f:
        raw = json.load(f)

    nodes = raw["nodes"]
    profile = {
        "identity": raw.get("identity", {}),
        "firmware": raw.get("identity", {}).get("firmware", "unknown"),
        "generated_from": os.path.basename(map_path),
        "nodes": {},
    }

    counts = {"writable": 0, "readonly": 0, "excluded": 0, "params": 0}
    excluded_list = []

    for node, d in nodes.items():
        entry = {"children": d["children"], "params": {}}
        for pname, p in d["params"].items():
            counts["params"] += 1
            spec = parse_type(p.get("type"))
            reason = excluded_reason(node, pname)
            if p.get("readonly"):
                mode, why = "readonly", "marked r/o by the desk"
                counts["readonly"] += 1
            elif reason:
                mode, why = "excluded", reason
                counts["excluded"] += 1
                excluded_list.append((node, pname, reason))
            else:
                mode, why = "writable", None
                counts["writable"] += 1
            # Kept deliberately lean: this file loads on every server start and
            # ships inside the bundle. `raw_type` and the desk's value at
            # profile time are useful when reading the map by hand, and the map
            # is still on disk for that. Validation needs mode, type, and a
            # reason only where something is excluded.
            rec = {"mode": mode, "type": spec}
            if mode == "excluded":
                rec["why"] = why
            entry["params"][pname] = rec
        profile["nodes"][node] = entry

    if out_path is None:
        out_path = profile_path_for(profile["identity"].get("model"), write=True)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    profile["counts"] = counts
    profile["excluded"] = [{"node": n, "param": p, "reason": r}
                           for n, p, r in sorted(excluded_list)]
    # Written compact: this ships inside the bundle and is read by machine.
    # The pretty, human-readable version of the same data is the map in
    # outputs/, which is kept.
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(profile, f, sort_keys=True, separators=(",", ":"))
    return profile, out_path


# ------------------------------------------------------------------ loading

class Profile:
    def __init__(self, path=DEFAULT_PROFILE):
        self.path = path
        with open(path, encoding="utf-8") as f:
            self.d = json.load(f)
        self.nodes = self.d["nodes"]

    @classmethod
    def for_model(cls, model):
        """The best profile for a desk model. Check .model_matches(model)
        afterwards: a fallback profile from another model loads, but the
        caller must say so."""
        p = profile_path_for(model) if model else None
        return cls(p or DEFAULT_PROFILE)

    @property
    def firmware(self):
        return self.d.get("firmware", "unknown")

    @property
    def model(self):
        return self.d.get("identity", {}).get("model", "unknown")

    def model_matches(self, model):
        return model_slug(self.model) == model_slug(model)

    def find(self, query, limit=60):
        """Addresses and parameter names containing every word of `query`
        (case-insensitive), numbered nodes collapsed to one pattern so /ch/1
        through /ch/40 come back as one hit. For finding a command when its
        address isn't known."""
        words = [w.lower() for w in query.split() if w]
        pats = {}
        for node, n in self.nodes.items():
            pat = re.sub(r"/\d+(?=/|$)", "/N", node)
            for pname, p in n["params"].items():
                hay = ("%s/%s" % (pat, pname)).lower()
                if all(w in hay for w in words):
                    key = (pat, pname)
                    if key not in pats:
                        pats[key] = {"address": "%s/%s" % (pat, pname),
                                     "example": "%s/%s" % (node, pname),
                                     "mode": p["mode"], "type": p["type"]}
        hits = sorted(pats.values(), key=lambda h: (len(h["address"]), h["address"]))
        return hits[:limit], len(hits)

    def check_write(self, node, kv, desk=None):
        """kv is a dict of key -> value. Returns a list of problems, empty if
        the whole write is acceptable. Whole-message validation: nothing is
        sent unless every key passes.

        A node's parameter set is NOT fixed. The gate and dynamics sections
        change which parameters exist according to the selected model:
        /ch/9/gate under mdl=76LA carries in, out, att, rel and ratio, while
        the same node under another model carries none of them, and
        /ch/9/dyn takes ratio=3.0 under COMP where the profile expects the
        list 1.5:1, 2:1, 3:1. A single generated profile therefore cannot
        validate those sections.

        So the profile is consulted first, for speed, and any key it would
        REJECT is re-checked against the node's live shape before the
        rejection stands. Correct in every case, and it costs a round trip
        only on the path that was about to refuse the write. Pass `desk` to
        enable it; without one, the stored profile is the last word.

        Exclusions are never re-checked. Those are policy, not shape."""
        problems, doubtful = [], []
        n = self.nodes.get(node)
        if n is None:
            doubtful.append(("%s is not a node on this desk" % node, None))
            n = {"params": {}}
        for k, v in kv.items():
            # Policy first, from the rules above rather than the stored
            # mode, so a rule added after a profile was built still applies.
            why_ex = excluded_reason(node, k)
            if why_ex:
                problems.append("%s/%s is excluded: %s" % (node, k, why_ex))
                continue
            p = n["params"].get(k)
            if p is None:
                doubtful.append(("%s has no parameter %s" % (node, k), k))
                continue
            if p["mode"] == "excluded":
                problems.append("%s/%s is excluded: %s"
                                % (node, k, p.get("why", "excluded by policy")))
            elif p["mode"] == "readonly":
                doubtful.append(("%s/%s is read-only" % (node, k), k))
            else:
                why = validate(p["type"], v)
                if why:
                    doubtful.append(("%s/%s=%r %s" % (node, k, v, why), k))

        if doubtful and desk is not None:
            live = desk.describe(node, with_values=False)
            if live is not None and not live.get("overflow"):
                lp = live["params"]
                for message, key in doubtful:
                    if key is None:
                        problems.append(message)
                        continue
                    p = lp.get(key)
                    if p is None:
                        problems.append(message + " (confirmed live)")
                    elif p.get("readonly"):
                        problems.append("%s/%s is read-only (confirmed live)"
                                        % (node, key))
                    else:
                        why = validate(parse_type(p.get("type")), kv[key])
                        if why:
                            problems.append("%s/%s=%r %s (live)"
                                            % (node, key, kv[key], why))
                return problems
        problems.extend(m for m, _ in doubtful)
        return problems

    def identity_warning(self, node, kv):
        """A channel written with a name, colour or icon but no clink=0 will
        store the value and display nothing. Returns a warning or None."""
        # Any node whose profile carries clink (channels and aux inputs on
        # firmware 3.1), not only /ch - an aux named without clink=0 has the
        # same invisible-name problem.
        n = self.nodes.get(node)
        if n is None or CLINK_KEY not in n.get("params", {}):
            if not re.match(r"^/(ch|aux)/\d+$", node):
                return None
        if not any(k in kv for k in IDENTITY_KEYS):
            return None
        if kv.get(CLINK_KEY) == "0" or kv.get(CLINK_KEY) == 0:
            return None
        return ("%s sets %s without clink=0. With clink=1 the channel renders "
                "its source's identity, so this write lands in the parameter "
                "and never appears on the console."
                % (node, ", ".join(k for k in IDENTITY_KEYS if k in kv)))


# -------------------------------------------------------------------- cli

def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1]

    if cmd == "build":
        if len(argv) < 3:
            print("usage: wing_profile.py build <map.json>")
            return 1
        prof, path = build(argv[2])
        c = prof["counts"]
        print("Built %s" % path)
        print("  firmware  %s" % prof["firmware"])
        print("  nodes     %d" % len(prof["nodes"]))
        print("  params    %d" % c["params"])
        print("    writable %d" % c["writable"])
        print("    readonly %d" % c["readonly"])
        print("    excluded %d" % c["excluded"])
        print("\n  excluded addresses:")
        for e in prof["excluded"]:
            print("    %-28s %-14s %s" % (e["node"], e["param"], e["reason"]))
        return 0

    if cmd == "show":
        for e in list_profiles():
            p = Profile(e["path"])
            c = p.d["counts"]
            print("%-12s %-16s fw %s   %d nodes   %d params (%d writable, %d r/o, "
                  "%d excluded)  %s" % (p.model, e["origin"], p.firmware,
                                        len(p.nodes), c["params"], c["writable"],
                                        c["readonly"], c["excluded"], e["path"]))
        return 0

    if cmd == "check":
        p = Profile()
        node = argv[2]
        kv = dict(a.split("=", 1) for a in argv[3:])
        probs = p.check_write(node, kv)
        if probs:
            print("REJECTED:")
            for x in probs:
                print("  " + x)
            return 1
        print("OK: %s <- %s" % (node, kv))
        warn = p.identity_warning(node, kv)
        if warn:
            print("WARNING: " + warn)
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
