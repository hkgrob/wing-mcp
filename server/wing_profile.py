#!/usr/bin/env python3
"""
WING profile: the command map, the writable set, and write validation.

Turns a raw node map produced by probe/wing_map.py into the profile the server
loads. Everything the server is allowed to do to the desk is decided here.

The profile is generated off the hardware, never transcribed. Its only external
input is the exclusion policy below, which names capabilities rather than
addresses wherever it can.

    python wing_profile.py build ../outputs/WING-Profile_<fw>_<stamp>.json
    python wing_profile.py show                     # summarize the built profile
    python wing_profile.py check /ch/1 name=Fiddle  # validate without sending
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PROFILE = os.path.join(HERE, "wing_profile.json")


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
]

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
_NUMBER = re.compile(r"^-?(\d+(\.\d+)?|\d+k\d+|-oo)$")


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
    return None


# ----------------------------------------------------------------- building

def build(map_path, out_path=DEFAULT_PROFILE):
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
        with open(path, encoding="utf-8") as f:
            self.d = json.load(f)
        self.nodes = self.d["nodes"]

    @property
    def firmware(self):
        return self.d.get("firmware", "unknown")

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
        if not re.match(r"^/ch/\d+$", node):
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
        p = Profile()
        c = p.d["counts"]
        print("firmware %s   %d nodes   %d params (%d writable, %d r/o, %d excluded)"
              % (p.firmware, len(p.nodes), c["params"], c["writable"],
                 c["readonly"], c["excluded"]))
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
