#!/usr/bin/env python3
"""Audio from the WING's USB return: device discovery, capture, and the
routing that puts the signal to be analysed on the USB outputs.

The WING is a 48x48 USB audio interface. Its USB outputs (/io/out/USB/N,
each a {grp, in} pair) are what the computer sees as inputs 1..48. To
analyse something, the server points spare USB outputs at it, records,
and puts the outputs back exactly as they were.

Sources, as accepted by parse_source():
    main:1        Main 1, stereo (L and R legs)       main:1L / main:1R
    bus:3, mtx:2  a bus or matrix, stereo             bus:3L ...
    src:A.13      a raw input (stage box A input 13, LCL.8, AUX.3, USB.5)
    ch:14         the raw input feeding channel 14 (its source, before
                  the channel's processing - what a mic actually picks up)
Output legs: MAIN 1..8, BUS 1..32, MTX 1..16 count mono legs, so bus N is
legs 2N-1 (L) and 2N (R).

Python packages: sounddevice (PortAudio) and numpy/scipy. They are
optional - everything else in the server works without them, and
check_deps() says what is missing.
"""

import importlib
import os
import re
import subprocess
import sys

STEREO_GROUPS = {"main": "MAIN", "bus": "BUS", "mtx": "MTX"}
RAW_GROUPS = {"LCL", "AUX", "A", "B", "C", "SC", "USB", "CRD", "MOD", "PLAY", "AES", "USR", "OSC"}
DEPS = ("numpy", "scipy", "sounddevice")


def check_deps():
    missing = []
    for m in DEPS:
        try:
            importlib.import_module(m)
        except Exception:
            missing.append(m)
    return missing


def install_deps():
    """pip-install the missing packages into the interpreter running the
    server. Homebrew's Python refuses a plain pip install, hence the flag."""
    missing = check_deps()
    if not missing:
        return {"installed": [], "output": "nothing to install"}
    cmd = [sys.executable, "-m", "pip", "install", "--quiet"] + missing
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0 and "externally-managed" in (r.stderr or ""):
        r = subprocess.run(cmd + ["--break-system-packages"], capture_output=True,
                           text=True, timeout=600)
    importlib.invalidate_caches()
    return {"installed": missing if r.returncode == 0 else [],
            "returncode": r.returncode, "output": (r.stdout + r.stderr)[-1500:]}


def find_device(hint=None):
    """The WING's USB audio input: (index, info) or (None, list of inputs)."""
    import sounddevice as sd
    devs = sd.query_devices()
    hint = (hint or os.environ.get("WING_AUDIO_DEVICE") or "WING").upper()
    ins = []
    for i, d in enumerate(devs):
        if d.get("max_input_channels", 0) > 0:
            ins.append({"index": i, "name": d["name"], "inputs": d["max_input_channels"],
                        "rate": d.get("default_samplerate")})
            if hint in d["name"].upper():
                return i, dict(d)
    return None, ins


def capture(device, channels, seconds, rate=None):
    """Record `seconds` from 1-based device input `channels` (a list).
    Returns (samples ndarray (n_channels, n_samples) float32, rate)."""
    import numpy as np
    import sounddevice as sd
    info = sd.query_devices(device)
    rate = int(rate or info.get("default_samplerate") or 48000)
    need = max(channels)
    data = sd.rec(int(seconds * rate), samplerate=rate, device=device, channels=need,
                  dtype="float32", blocking=True)
    return np.ascontiguousarray(data[:, [c - 1 for c in channels]].T), rate


class Stream:
    """Continuous capture into a ring buffer, for live monitors."""

    def __init__(self, device, channels, seconds_kept=60.0, rate=None):
        import numpy as np
        import sounddevice as sd
        info = sd.query_devices(device)
        self.rate = int(rate or info.get("default_samplerate") or 48000)
        self.channels = channels
        self.buf = np.zeros((len(channels), int(seconds_kept * self.rate)), dtype="float32")
        self.pos, self.filled = 0, 0
        self.total = 0
        self._np = np

        def cb(indata, frames, t, status):
            x = indata[:, [c - 1 for c in self.channels]].T
            n = x.shape[1]
            end = self.pos + n
            L = self.buf.shape[1]
            if end <= L:
                self.buf[:, self.pos:end] = x
            else:
                k = L - self.pos
                self.buf[:, self.pos:] = x[:, :k]
                self.buf[:, :n - k] = x[:, k:]
            self.pos = end % L
            self.filled = min(L, self.filled + n)
            self.total += n
            if self.on_block:
                self.on_block(x)

        self.on_block = None
        self.stream = sd.InputStream(device=device, channels=max(channels), samplerate=self.rate,
                                     dtype="float32", callback=cb, blocksize=int(self.rate * 0.1))

    def start(self):
        self.stream.start()

    def stop(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass

    def last(self, seconds):
        n = min(int(seconds * self.rate), self.filled)
        L = self.buf.shape[1]
        idx = (self.pos - n + self._np.arange(n)) % L
        return self.buf[:, idx]


# ---------------------------------------------------------------- routing

def parse_source(spec, desk=None):
    """A source spec -> [(label, grp, in)] in leg order."""
    s = spec.strip()
    m = re.match(r"^(main|bus|mtx):(\d+)([LR]?)$", s, re.I)
    if m:
        kind, n, side = m.group(1).lower(), int(m.group(2)), m.group(3).upper()
        grp = STEREO_GROUPS[kind]
        legs = [("%s:%d L" % (kind, n), grp, 2 * n - 1), ("%s:%d R" % (kind, n), grp, 2 * n)]
        if side == "L":
            return legs[:1]
        if side == "R":
            return legs[1:]
        return legs
    m = re.match(r"^src:([A-Z]+)\.(\d+)$", s, re.I)
    if m and m.group(1).upper() in RAW_GROUPS:
        return [(s, m.group(1).upper(), int(m.group(2)))]
    m = re.match(r"^ch:(\d+)$", s, re.I)
    if m:
        if desk is None:
            raise ValueError("ch: sources need the desk to look up the channel's input")
        n = int(m.group(1))
        p = (desk.describe("/ch/%d/in/conn" % n, with_values=True) or {}).get("params", {})
        grp, inp = p.get("grp", {}).get("value"), p.get("in", {}).get("value")
        if not grp or grp == "OFF":
            raise ValueError("channel %d has no input source" % n)
        return [("ch:%d (%s.%s)" % (n, grp, inp), grp, int(inp))]
    raise ValueError("source %r: use main:1, bus:3, mtx:2 (add L/R for one leg), "
                     "src:A.13 / src:LCL.8, or ch:14" % spec)


def usb_outputs(desk, count=48):
    """Current routing of every USB output: {n: (grp, in)}."""
    out = {}
    for n in range(1, count + 1):
        p = (desk.describe("/io/out/USB/%d" % n, with_values=True) or {}).get("params", {})
        if "grp" in p:
            out[n] = (p["grp"]["value"], p.get("in", {}).get("value"))
    return out


def pick_outputs(desk, needed, configured=None):
    """USB outputs to borrow: the configured ones, else the highest-numbered
    outputs routed to nothing (grp OFF), so a recording or stream feed on
    the low outputs is never touched."""
    if configured:
        return list(configured)[:needed]
    routing = usb_outputs(desk)
    free = [n for n in sorted(routing, reverse=True) if routing[n][0] == "OFF"]
    if len(free) < needed:
        raise RuntimeError("Need %d free USB outputs (routed to OFF) to borrow for analysis; found %d. "
                           "Free some, or name them in ~/.wing-mcp/config.json as "
                           '{"usb_analysis_outs": [47, 48]}.' % (needed, len(free)))
    return sorted(free[:needed])


class Routed:
    """Context manager: point USB outputs at sources, restore on exit."""

    def __init__(self, desk, outs, legs):
        self.desk, self.outs, self.legs, self.saved = desk, outs, legs, {}

    def __enter__(self):
        for n, (_label, grp, inp) in zip(self.outs, self.legs):
            p = (self.desk.describe("/io/out/USB/%d" % n, with_values=True) or {}).get("params", {})
            self.saved[n] = {"grp": p.get("grp", {}).get("value", "OFF"),
                             "in": p.get("in", {}).get("value", "1")}
            self.desk.write_node("/io/out/USB/%d" % n, {"grp": grp, "in": inp}, verify=True)
        return self

    def __exit__(self, *exc):
        for n, kv in self.saved.items():
            try:
                self.desk.write_node("/io/out/USB/%d" % n, kv, verify=True)
            except Exception:
                pass
        return False
