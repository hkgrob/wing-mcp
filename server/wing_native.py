#!/usr/bin/env python3
"""WING native protocol: meters and RTA.

OSC (wing_transport) reads and writes parameters but carries no meter or
analyzer data. Those come from the desk's native binary protocol, per
"WING Remote Protocols" V 3.1.2-a, "Channel 3: Metering":

  - TCP to the desk on port 2222. The stream is split into channels; the
    two bytes 0xDF 0xD<n> select channel n. Channel 3 is meter requests.
  - Inside channel 3, tokens:
        0xD3 <port:2>       the UDP port on this machine to send meters to
        0xD4 <id:4>         report id; repeat it to keep the stream alive
                            (meter data stops 5 s after the last renewal)
        0xDC ... 0xDE       a meter collection: type byte, then 0-based
                            indexes (0x00 = strip 1), repeatable
  - The desk then streams UDP packets to that port:
        <report id:4> <n x int16 big-endian>
    Levels are in 1/256 dB. Gate and dynamics gain values are scaled so
    256 = 20 dB of reduction (the standard WING gate: 60 dB).
  - A literal 0xDF in the data is escaped as 0xDF 0xDE.

Words per strip, from the document's Meter Data table:
    ch/aux/bus/main/mtx: in L, in R, out L, out R, gate key, gate gain,
                         dyn key, dyn gain                         (8)
    dca: pre L, pre R, post L, post R                              (4)
    rta: 120 semitone bands, E0 20.6 Hz .. D#10 19.9 kHz          (120)

Everything here was written from the document, not measured on hardware;
MeterSession keeps the first raw packet so a mismatch is diagnosable.
"""

import math
import socket
import struct
import threading
import time

NATIVE_PORT = 2222
ESC = 0xDF
CH_METER = 3

TYPES = {  # name -> (type byte, words per index, max index)
    "ch": (0xA0, 8, 40), "aux": (0xA1, 8, 8), "bus": (0xA2, 8, 16),
    "main": (0xA3, 8, 4), "mtx": (0xA4, 8, 8), "dca": (0xA5, 4, 16),
}
STRIP_FIELDS = ["in_l", "in_r", "out_l", "out_r",
                "gate_key", "gate_gain", "dyn_key", "dyn_gain"]
DCA_FIELDS = ["pre_l", "pre_r", "post_l", "post_r"]
RTA_TYPE, RTA_BANDS = 0xAA, 120
RTA_FREQS = [20.602 * 2 ** (i / 12.0) for i in range(RTA_BANDS)]

# /cfg/rta/rtasrc index: strips in desk order.
RTA_SRC_BASE = {"ch": 0, "aux": 40, "bus": 48, "main": 64, "mtx": 68}


def escape(data):
    """Escape a payload for the native stream: 0xDF followed by a byte in
    0xD0..0xDE would read as a channel switch, so it becomes 0xDF 0xDE."""
    out = bytearray()
    for b in data:
        out.append(b)
        if b == ESC:
            out.append(ESC - 1)
    return bytes(out)


def select_channel(ch):
    return bytes([ESC, 0xD0 + ch])


def parse_strips(spec):
    """'ch:1-8,12' / ['ch:1-4', 'bus:1'] -> [(kind, n), ...] in order."""
    if isinstance(spec, str):
        spec = [spec]
    out = []
    for item in spec:
        kind, _, rng = item.partition(":")
        kind = kind.strip().lower()
        if kind not in TYPES:
            raise ValueError("unknown strip type %r (use %s)" % (kind, ", ".join(TYPES)))
        mx = TYPES[kind][2]
        for part in (rng or "1-%d" % mx).split(","):
            a, _, b = part.strip().partition("-")
            lo, hi = int(a), int(b or a)
            if not (1 <= lo <= hi <= mx):
                raise ValueError("%s:%s out of range 1..%d" % (kind, part, mx))
            out += [(kind, n) for n in range(lo, hi + 1)]
    return out


def collection(strips, rta=False):
    """Meter collection bytes. Indexes of one type are grouped, in request
    order within the type; the returned layout says how to read replies."""
    body, layout = bytearray([0xDC]), []
    order = []
    for kind, _n in strips:
        if kind not in order:
            order.append(kind)
    for kind in order:
        tb, words, _ = TYPES[kind]
        body.append(tb)
        for k, n in strips:
            if k == kind:
                body.append(n - 1)
                layout.append((kind, n, words))
    if rta:
        body.append(RTA_TYPE)
        layout.append(("rta", None, RTA_BANDS))
    body.append(0xDE)
    return bytes(body), layout


class MeterSession:
    """One subscription: open, collect for `seconds`, close."""

    def __init__(self, host, report_id=0x57494E01):
        self.host = host
        self.report_id = report_id
        self.raw_first = None
        self.packets = 0
        self.foreign = 0

    def collect(self, coll, seconds):
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(("0.0.0.0", 0))
        port = udp.getsockname()[1]
        # Some host firewalls only pass inbound UDP that answers something we
        # sent; one harmless packet from this socket toward the desk opens
        # that path where it can be opened.
        try:
            udp.sendto(b"/?\x00\x00", (self.host, 2223))
        except OSError:
            pass
        udp.settimeout(0.25)
        tcp = socket.create_connection((self.host, NATIVE_PORT), timeout=3)
        rid = struct.pack(">I", self.report_id)
        try:
            tcp.sendall(select_channel(CH_METER) +
                        escape(bytes([0xD3]) + struct.pack(">H", port)))
            tcp.sendall(escape(bytes([0xD4]) + rid + coll))
            frames, t0, renew = [], time.time(), time.time()
            while time.time() - t0 < seconds:
                if time.time() - renew > 2.0:
                    tcp.sendall(escape(bytes([0xD4]) + rid))
                    renew = time.time()
                try:
                    data, _ = udp.recvfrom(65535)
                except socket.timeout:
                    continue
                if self.raw_first is None:
                    self.raw_first = data[:64].hex()
                body = data
                if body[:2] == select_channel(CH_METER):  # tolerate framing
                    body = body[2:]
                if body[:4] != rid:
                    self.foreign += 1
                    continue
                n = (len(body) - 4) // 2
                frames.append(struct.unpack(">%dh" % n, body[4:4 + 2 * n]))
                self.packets += 1
            return frames
        finally:
            try:
                tcp.close()
            finally:
                udp.close()


def _db(v):
    return round(v / 256.0, 1)


def _gr(v):
    """Gain-reduction word to dB of reduction (256 = 20 dB)."""
    return round(abs(v) / 256.0 * 20.0, 1)


def summarize(frames, layout):
    """Peak and average per meter over the window."""
    if not frames:
        return {}, None
    out, rta, pos = {}, None, 0
    for kind, n, words in layout:
        cols = [[f[pos + i] for f in frames if len(f) > pos + i] for i in range(words)]
        pos += words
        if kind == "rta":
            rta = [(_db(sum(c) / len(c)) if c else None, _db(max(c)) if c else None) for c in cols]
            continue
        names = DCA_FIELDS if kind == "dca" else STRIP_FIELDS
        rec = {}
        for name, c in zip(names, cols):
            if not c:
                continue
            if name.endswith("_gain"):
                rec[name.replace("_gain", "_reduction_db")] = {
                    "max": _gr(min(c, key=lambda x: -abs(x))), "avg": _gr(sum(c) / len(c))}
            else:
                rec[name] = {"peak": _db(max(c)), "avg": _db(sum(c) / len(c))}
        if "in_l" in rec:
            rec["input_peak_db"] = max(rec["in_l"]["peak"], rec.get("in_r", rec["in_l"])["peak"])
        if "out_l" in rec:
            rec["output_peak_db"] = max(rec["out_l"]["peak"], rec.get("out_r", rec["out_l"])["peak"])
        out["%s:%d" % (kind, n)] = rec
    return out, rta


def rta_bands(rta, per_band=4):
    """Fold 120 semitone bands into third-octave-ish groups of `per_band`
    semitones (4 = one third of an octave), reporting the average level."""
    groups = []
    for i in range(0, len(rta), per_band):
        chunk = [a for a, _ in rta[i:i + per_band] if a is not None]
        if not chunk:
            continue
        f = math.sqrt(RTA_FREQS[i] * RTA_FREQS[min(i + per_band, len(rta)) - 1])
        groups.append({"hz": round(f), "db": round(sum(chunk) / len(chunk), 1)})
    return groups


def rta_peaks(rta, n=6):
    """The n semitone bands that stand furthest above their neighbourhood
    (one octave each side), i.e. resonances rather than overall tilt."""
    av = [a for a, _ in rta]
    scored = []
    for i, a in enumerate(av):
        if a is None:
            continue
        nb = [x for x in av[max(0, i - 12):i + 13] if x is not None]
        local = sorted(nb)[len(nb) // 2]
        scored.append((a - local, i))
    scored.sort(reverse=True)
    return [{"hz": round(RTA_FREQS[i]), "above_local_db": round(d, 1),
             "level_db": av[i]} for d, i in scored[:n] if d > 0]


def lock():
    return threading.Lock()
