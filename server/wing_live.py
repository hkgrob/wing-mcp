#!/usr/bin/env python3
"""Real-time features: timed fades and the live change feed.

Fades
    A fade moves one or more parameters (faders, send levels, any number)
    from where they are to a target over a time, in ~25 steps a second,
    linear in dB (how a fader sounds when moved by hand) or S-curved. Each
    runs on its own thread so a 10-second fade never blocks a tool call;
    a new fade on the same parameter replaces the old one. The final value
    is written and read back like any other write.

Change feed
    The WING sends every parameter change, from the surface, WING Edit,
    a phone app or this server, to whoever holds its OSC event
    subscription (/*S, renewed every few seconds; it lapses after 10 s).
    The desk allows ONE subscription at a time, so turning the feed on can
    take it away from another app relying on it (Bitfocus Companion's
    feedbacks, for one) - which is why it is off until asked for. Changes
    this server made are tagged "self" by matching them to its own recent
    writes, so "what did someone else change?" has an answer.
"""

import collections
import socket
import threading
import time

import wing_transport as wt

OSC_PORT = wt.OSC_PORT


# ----------------------------------------------------------------- fades

def _to_db(v):
    s = str(v).strip().replace("+", "")
    if s in ("-oo", "-inf"):
        return float("-inf")
    try:
        return float(s)
    except ValueError:
        return None


class Fade:
    FLOOR = -90.0      # where -oo is treated as a number for interpolation
    RATE_HZ = 25.0

    def __init__(self, fid, desk, targets, seconds, curve):
        self.fid, self.desk, self.targets = fid, desk, targets  # [(node,key,start,end)]
        self.seconds, self.curve = max(0.0, float(seconds)), curve
        self.state, self.error, self.progress = "running", None, 0.0
        self.started = time.time()
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="fade-%s" % fid)

    def keys(self):
        return {(n, k) for n, k, _, _ in self.targets}

    def stop(self):
        self._stop.set()

    def _shape(self, x):
        return x * x * (3 - 2 * x) if self.curve == "s" else x

    def _value(self, a, b, x):
        a2 = self.FLOOR if a == float("-inf") else a
        b2 = self.FLOOR if b == float("-inf") else b
        v = a2 + (b2 - a2) * self._shape(x)
        if x >= 1.0:
            return "-oo" if b == float("-inf") else round(b, 1)
        if v <= self.FLOOR + 0.05:
            return "-oo"
        return round(v, 1)

    def _run(self):
        try:
            steps = max(1, int(self.seconds * self.RATE_HZ))
            t0 = time.time()
            for i in range(1, steps + 1):
                if self._stop.is_set():
                    self.state = "stopped"
                    return
                x = i / float(steps)
                by_node = collections.defaultdict(dict)
                for node, key, a, b in self.targets:
                    by_node[node][key] = self._value(a, b, x)
                for node, kv in by_node.items():
                    self.desk.write_node(node, kv, verify=(i == steps))
                self.progress = x
                lag = t0 + i / self.RATE_HZ - time.time()
                if lag > 0 and i < steps:
                    time.sleep(lag)
            self.state = "done"
        except Exception as exc:
            self.state, self.error = "failed", "%s: %s" % (type(exc).__name__, exc)

    def snapshot(self):
        return {"fade_id": self.fid, "state": self.state, "seconds": self.seconds,
                "progress": round(self.progress, 2), "error": self.error,
                "targets": [{"node": n, "param": k,
                             "from": "-oo" if a == float("-inf") else a,
                             "to": "-oo" if b == float("-inf") else b}
                            for n, k, a, b in self.targets]}


class Fader:
    """Registry of running fades."""

    def __init__(self):
        self.fades, self.lock, self.n = {}, threading.Lock(), 0

    def start(self, desk, targets, seconds, curve):
        with self.lock:
            self.n += 1
            f = Fade("f%d" % self.n, desk, targets, seconds, curve)
            keys = f.keys()
            for other in self.fades.values():
                if other.state == "running" and other.keys() & keys:
                    other.stop()
            self.fades[f.fid] = f
        f.thread.start()
        return f

    def stop(self, fid=None):
        stopped = []
        with self.lock:
            for f in self.fades.values():
                if f.state == "running" and (fid is None or f.fid == fid):
                    f.stop()
                    stopped.append(f.fid)
        return stopped

    def active(self):
        return [f.snapshot() for f in self.fades.values() if f.state == "running"]


# ----------------------------------------------------------- change feed

class ChangeFeed:
    RENEW_S = 7.0

    def __init__(self, host, maxlen=20000):
        self.host = host
        self.events = collections.deque(maxlen=maxlen)   # (t, addr, value, origin)
        self.running = False
        self.started = None
        self.sock = None
        self._recent_self = {}        # addr -> time of our own write
        self._lock = threading.Lock()
        self._thread = None

    # Desk.write_node calls this for every write it sends.
    def note_self_write(self, node, kv):
        now = time.time()
        with self._lock:
            for k in kv:
                self._recent_self["%s/%s" % (node, k)] = now

    def start(self):
        if self.running:
            return
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.settimeout(0.5)
        self.running, self.started = True, time.time()
        self._thread = threading.Thread(target=self._run, daemon=True, name="wing-feed")
        self._thread.start()

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass

    def _subscribe(self):
        try:
            self.sock.sendto(wt.encode("/*S"), (self.host, OSC_PORT))
        except OSError:
            pass

    def _run(self):
        last = 0.0
        while self.running:
            if time.time() - last > self.RENEW_S:
                self._subscribe()
                last = time.time()
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            addr, args = wt.decode(data)
            if not addr.startswith("/") or addr in ("/*", "/?"):
                continue
            val = args[0] if args else None
            now = time.time()
            with self._lock:
                t_self = self._recent_self.get(addr)
            origin = "self" if t_self and now - t_self < 3.0 else "external"
            self.events.append((now, addr, val, origin))

    def changes(self, since_s=60.0, prefix=None, origin=None, mirrors=False, limit=200):
        cutoff = time.time() - since_s
        rows = {}
        for t, addr, val, org in list(self.events):
            if t < cutoff:
                continue
            if prefix and not addr.startswith(prefix):
                continue
            if origin and org != origin:
                continue
            leaf = addr.rsplit("/", 1)[-1]
            if not mirrors and leaf.startswith("$"):
                continue
            r = rows.get(addr)
            if r is None:
                rows[addr] = {"address": addr, "first": t, "last": t, "from": val,
                              "to": val, "count": 1, "origin": org}
            else:
                r.update(last=t, to=val, count=r["count"] + 1)
                if org == "external":
                    r["origin"] = "external" if r["origin"] == "external" else "mixed"
        out = sorted(rows.values(), key=lambda r: r["last"], reverse=True)[:limit]
        now = time.time()
        for r in out:
            r["seconds_ago"] = round(now - r.pop("last"), 1)
            r.pop("first")
            if isinstance(r["from"], float):
                r["from"] = round(r["from"], 2)
            if isinstance(r["to"], float):
                r["to"] = round(r["to"], 2)
        return out

    def status(self):
        return {"running": self.running, "events_buffered": len(self.events),
                "listening_for_s": round(time.time() - self.started, 1) if self.started else 0}
