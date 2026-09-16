#!/usr/bin/env python3
"""
WING transport: OSC codec, console discovery, node read and write.

Everything here was settled against MyWing on firmware 3.1-0,
2026-08-29. The facts this module encodes, none of them assumed:

  - OSC on UDP 2223.
  - `/?` to the broadcast address returns, on address `/*`, the string
    WING,<ip>,<name>,<model>,<serial>,<firmware>
  - A node reply is a FIXED-WIDTH TABLE, not comma-separated key=value:
        name [2:17]  value [17:33]  r/o flag [33:37]  type and range [37:]
    A child node renders as "  name           (node)".
    `,s "?"` omits the value column; `,s "#"` includes it.
  - A node WRITE does take key=value: `/ch/37 ,s "name=X,col=5"`, and the
    desk acknowledges on `/*` with the string "OK".
  - Round trip to first reply is 5.6 to 18.3 ms.
  - `,s "*"` overflows the desk's buffer above roughly 20 KB, so a dump runs
    per child node and never at a family root.

Self-test:  python wing_transport.py selftest
Live check: python wing_transport.py info
"""

import ipaddress
import socket
import struct
import sys
import threading
import time

OSC_PORT = 2223

# Column offsets of the desk's reply table.
NAME_COL, VAL_COL, FLAG_COL, TYPE_COL = 2, 17, 33, 37

# Reply drain window: how long Desk.ask() keeps listening after the first
# reply packet for a second one before deciding the reply is complete.
#
# Measured, not guessed - probe/wing_packet_timing_report_20260831_172537.txt,
# 2026-08-31: 300 queries against 5 addresses ('/ch/1/mute', '/$stat', '/ch/1',
# '/fx/7', '/bus'), both '#' and '?' forms, 30 repeats each, under a 2.0s
# listening window that could not have truncated a split reply. Zero replies
# arrived as more than one packet. Across the 240 queries that got a reply,
# first-packet latency: p95 16.42 ms, max 32.35 ms. 35 ms is roughly 2x that
# observed max - a measured margin, not "a number you picked" (Doug,
# 2026-08-31, of the old 0.25s value).
#
# Two honest limits from the same probe: the largest reply exercised was
# 4,716 bytes, and past roughly 20 KB the desk returns BUFFER OVERFLOW rather
# than splitting a reply across packets (see Desk.describe()'s overflow
# handling) - which is the real reason a short window is safe here, not an
# assumption that a larger reply could never split.
DRAIN_WINDOW_S = 0.035

# Bumped by Desk.ask() every time a reply DOES arrive as more than one UDP
# packet - which the probe above never observed, but a short drain window
# makes a late second packet easy to miss silently (it would sit unread until
# the next call's _flush() drains it with nothing to say it was ever a second
# packet of a prior reply). Read this counter, or watch stderr, rather than
# assuming a multi-packet reply can't happen just because the probe didn't
# see one.
MULTI_PACKET_REPLIES = 0


# ------------------------------------------------------------------- codec

def _pad(b):
    r = len(b) % 4
    return b if r == 0 else b + b"\x00" * (4 - r)


def encode(addr, *args):
    out = _pad(addr.encode("utf-8") + b"\x00")
    tags, body = ",", b""
    for a in args:
        if isinstance(a, str):
            tags += "s"
            body += _pad(a.encode("utf-8") + b"\x00")
        elif isinstance(a, bool):
            tags += "T" if a else "F"
        elif isinstance(a, int):
            tags += "i"
            body += struct.pack(">i", a)
        elif isinstance(a, float):
            tags += "f"
            body += struct.pack(">f", a)
        else:
            raise TypeError("unsupported OSC argument: %r" % (a,))
    return out + _pad(tags.encode("utf-8") + b"\x00") + body


def _read_str(buf, i):
    end = buf.index(b"\x00", i)
    s = buf[i:end].decode("utf-8", "replace")
    i = end + 1
    while i % 4:
        i += 1
    return s, i


def decode(buf):
    try:
        addr, i = _read_str(buf, 0)
        if i >= len(buf):
            return addr, []
        tags, i = _read_str(buf, i)
        args = []
        for t in tags.lstrip(","):
            if t == "s":
                v, i = _read_str(buf, i); args.append(v)
            elif t == "i":
                args.append(struct.unpack(">i", buf[i:i + 4])[0]); i += 4
            elif t == "f":
                args.append(struct.unpack(">f", buf[i:i + 4])[0]); i += 4
            elif t == "T":
                args.append(True)
            elif t == "F":
                args.append(False)
            else:
                args.append("<unparsed tag %r>" % t); break
        return addr, args
    except Exception as e:
        return "<decode failed: %s>" % e, [repr(buf[:200])]


def parse_node(payload, with_values):
    """Returns (children, params). params maps name -> dict with value,
    readonly and type. value is None when with_values is False."""
    children, params = [], {}
    for raw in (payload or "").split("\n"):
        if not raw.strip():
            continue
        name = raw[NAME_COL:VAL_COL].strip() if len(raw) > NAME_COL else raw.strip()
        if not name:
            continue
        rest = raw[VAL_COL:] if len(raw) > VAL_COL else ""
        if "(node)" in raw:
            children.append(name)
            continue
        if with_values:
            params[name] = {
                "value": raw[VAL_COL:FLAG_COL].strip() if len(raw) > VAL_COL else "",
                "readonly": (raw[FLAG_COL:TYPE_COL].strip() if len(raw) > FLAG_COL else "") == "r/o",
                "type": raw[TYPE_COL:].strip() if len(raw) > TYPE_COL else "",
            }
        else:
            params[name] = {"value": None,
                            "readonly": name.startswith("$"),
                            "type": rest.strip()}
    return children, params


def encode_kv(kv):
    """The node write payload form."""
    return ",".join("%s=%s" % (k, v) for k, v in kv.items())


def encode_kv_file(kv):
    """The SHOW FILE payload form: a TAB between pairs, not a comma.

    The desk's own node message separates pairs with commas and therefore
    cannot express a value that contains one - /ch/9 carries tags=#M1,#M2,
    which a comma-separated line splits into a bogus key. A leaf write can
    carry it, so the limitation is the file format's, not the desk's, and the
    file format is ours to fix. Tabs never appear in WING values."""
    return "\t".join("%s=%s" % (k, v) for k, v in kv.items())


def parse_kv_file(payload):
    """Read a show-file payload. Tab-separated where written by this version,
    comma-separated for files written before the tab format, which stay
    readable. Returns (kv, problems)."""
    kv, problems = {}, []
    pairs = payload.split("\t") if "\t" in payload else payload.split(",")
    for pair in pairs:
        if "=" not in pair:
            problems.append("%r is not key=value" % pair)
            continue
        k, v = pair.split("=", 1)
        kv[k.strip()] = v.strip()
    return kv, problems


def needs_leaf_write(value):
    """True when a value cannot survive the node key=value form.

    Measured against the desk 2026-08-29, writing to /ch/40/name:

        value           node key=value    node key="value"   leaf ,s "value"
        Lead Vocal      LeadVocal         "LeadVocal"        Lead Vocal
        Snare (top)     Snare(top)        "Snare(top)"       Snare (top)
        A,B             A                 "A                 A,B

    The desk strips spaces from a node payload and treats a comma as the key
    separator, and quoting fixes neither - it just stores the quotes. Only the
    leaf address with one string argument survives all three. So any value
    carrying a space or a comma is written to its own leaf address.

    This matters beyond names: /ch/11 already carries tags="#D1,#M2", which
    would have split into two bogus keys on any dump-then-push round trip."""
    s = str(value)
    return " " in s or "," in s


# --------------------------------------------------------------- discovery

def local_ipv4s():
    out = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            out.add(info[4][0])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); out.add(s.getsockname()[0]); s.close()
    except Exception:
        pass
    return sorted(a for a in out if not a.startswith("127."))


def parse_ident(blob):
    f = (blob or "").split(",")
    return {"protocol": f[0] if len(f) > 0 else "",
            "ip":       f[1] if len(f) > 1 else "",
            "name":     f[2] if len(f) > 2 else "",
            "model":    f[3] if len(f) > 3 else "",
            "serial":   f[4] if len(f) > 4 else "",
            "firmware": f[5] if len(f) > 5 else ""}


def list_consoles(timeout=0.8):
    """What the WING Edit / Wing Q / Mixing Station selector shows."""
    found = {}
    targets = ["255.255.255.255"]
    for ip in local_ipv4s():
        try:
            targets.append(str(ipaddress.IPv4Network(ip + "/24", strict=False)
                               .broadcast_address))
        except Exception:
            pass
    for bcast in dict.fromkeys(targets):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(timeout)
        try:
            s.sendto(encode("/?"), (bcast, OSC_PORT))
            while True:
                try:
                    data, src = s.recvfrom(65535)
                except socket.timeout:
                    break
                _, args = decode(data)
                ident = parse_ident(args[0] if args and isinstance(args[0], str) else "")
                ident["ip"] = ident.get("ip") or src[0]
                found[ident["ip"]] = ident
        except Exception:
            pass
        finally:
            s.close()
    return list(found.values())


def find_by_name(name, timeout=0.8):
    """Match on console NAME, never on address. A festival network with a
    second WING on it makes an address match wrong rather than merely stale."""
    for c in list_consoles(timeout):
        if c.get("name") == name:
            return c
    return None


# ------------------------------------------------------------------- desk

class WriteRejected(Exception):
    pass


class Desk:
    def __init__(self, host, timeout=1.0):
        self.host, self.timeout = host, timeout
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout)
        # One socket, one desk, one caller at a time. A background push and a
        # concurrent wing_get would otherwise steal each other's replies -
        # UDP has no per-request framing here, so whichever call reads the
        # socket next gets whatever reply is sitting in it, right or wrong.
        # RLock, not Lock: write_node()'s leaf-write confirmation path calls
        # self.describe() -> self.payload() -> self.ask() from inside a
        # write_node() call that already holds the lock, on the same thread -
        # a plain Lock would deadlock on that reentry.
        self._lock = threading.RLock()

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def _flush(self):
        """An unread acknowledgement becomes the first reply to the next query
        and makes a landed write look like a failed one. Cost one afternoon.

        NON-BLOCKING, deliberately. This drains what is already sitting in the
        receive buffer and never waits for anything to arrive. The old 0.05
        wait was a fixed 50 ms tax on EVERY read, since the socket is normally
        empty here and the wait always ran to its full length - larger, after
        v0.2.2, than the 35 ms drain window itself, and the reason a push was
        still costing about 0.33 s per node when the window predicted far less
        (measured live 2026-08-31, roughly 3.5 reads per node at ~85 ms each).

        Waiting buys nothing: a stale acknowledgement has by definition already
        been sent by the desk and is already in the buffer, so a zero-timeout
        drain catches it. The only thing 50 ms could catch is a packet still in
        flight from an exchange that already completed, which does not happen -
        every write path in this module reads its own acknowledgement before
        returning. If that ever stops being true, this is where it will show up
        as a write reported failed that actually landed."""
        self.sock.settimeout(0.0)   # non-blocking; empty buffer raises at once
        try:
            while True:
                try:
                    self.sock.recvfrom(65535)
                except (socket.timeout, BlockingIOError, OSError):
                    break
        finally:
            self.sock.settimeout(self.timeout)

    def ask(self, addr, *args):
        """Returns (first_reply_ms, [(address, args)...]). Latency is to the
        first reply, never to the drain window.

        A second packet has never been observed for this desk's replies (see
        DRAIN_WINDOW_S above), but if one ever arrives, it must not vanish
        without a trace: MULTI_PACKET_REPLIES counts every ask() call that
        saw more than one packet, and a warning goes to stderr the moment it
        happens, so a future split reply is discoverable instead of being
        quietly truncated by a short drain window.

        Locked for the length of one request and reply, so a concurrent
        caller on another thread (a background push and a wing_get, say)
        waits its turn rather than racing this one for the socket. Reads are
        milliseconds, so waiting costs nothing (project CLAUDE.md)."""
        with self._lock:
            self._flush()
            t0 = time.time()
            self.sock.sendto(encode(addr, *args), (self.host, OSC_PORT))
            replies, first = [], None
            while True:
                try:
                    data, _ = self.sock.recvfrom(65535)
                except socket.timeout:
                    break
                except OSError:
                    break
                if first is None:
                    first = (time.time() - t0) * 1000.0
                replies.append(decode(data))
                self.sock.settimeout(DRAIN_WINDOW_S)
            self.sock.settimeout(self.timeout)
            if len(replies) > 1:
                global MULTI_PACKET_REPLIES
                MULTI_PACKET_REPLIES += 1
                print(
                    "wing_transport: MULTI-PACKET REPLY on %s (%d packets, "
                    "drain window %.0fms) - MULTI_PACKET_REPLIES now %d" %
                    (addr, len(replies), DRAIN_WINDOW_S * 1000.0,
                     MULTI_PACKET_REPLIES),
                    file=sys.stderr)
            return first, replies

    def payload(self, addr, q):
        _, replies = self.ask(addr, q)
        for a, args in replies:
            for x in args:
                if isinstance(x, str):
                    return x
        return None

    def describe(self, node, with_values=True):
        raw = self.payload(node, "#" if with_values else "?")
        if raw is None:
            raw = self.payload(node, "?")
            with_values = False
        if raw is None:
            return None
        if "OVERFLOW" in raw.upper():
            return {"overflow": True, "children": [], "params": {}}
        children, params = parse_node(raw, with_values)
        return {"overflow": False, "children": children, "params": params,
                "raw": raw}

    def _await_ack(self, t0, window=None):
        deadline = time.time() + (window if window is not None else self.timeout)
        while time.time() < deadline:
            try:
                data, _ = self.sock.recvfrom(65535)
            except (socket.timeout, OSError):
                break
            addr, args = decode(data)
            for x in args:
                if isinstance(x, str):
                    return {"ack": x, "ms": (time.time() - t0) * 1000.0,
                            "addr": addr}
        return None

    def write_node(self, node, kv, expect_ack=True):
        """Write a node's parameters.

        Keys whose values are safe in the node payload go in ONE message, which
        keeps the common case at one packet per node. Any value carrying a space
        or a comma goes to its own leaf address instead, because the node form
        mangles those (see needs_leaf_write).

        Acknowledgement-serialized by construction: this blocks on the desk's
        reply, so a caller looping over 40 channels is paced by the desk and
        needs no rate constant. Where leaf writes were used, the node is read
        back once and every written value confirmed, because a leaf write is
        not guaranteed to acknowledge and a silent mangle is the failure this
        whole function exists to prevent.

        Raises WriteRejected if the node message goes unacknowledged, or if a
        read-back shows a value did not land as written.

        Locked for the length of the whole write-and-confirm exchange,
        including the leaf-write read-back, so nothing else on another
        thread can interleave a read or write on this socket mid-sequence.
        The leaf-write branch below calls self.describe(), which re-enters
        self.ask() and therefore this lock on the same thread - see the
        RLock note on __init__."""
        with self._lock:
            batched = {k: v for k, v in kv.items() if not needs_leaf_write(v)}
            leafed = {k: v for k, v in kv.items() if needs_leaf_write(v)}

            result = {"node": node, "batched": sorted(batched), "leafed": sorted(leafed)}
            t0 = time.time()

            if batched:
                self._flush()
                self.sock.sendto(encode(node, encode_kv(batched)), (self.host, OSC_PORT))
                if expect_ack:
                    ack = self._await_ack(t0)
                    if ack is None:
                        raise WriteRejected(
                            "no acknowledgement from %s for %s" % (node, batched))
                    result["ack"] = ack["ack"]

            for k, v in leafed.items():
                self._flush()
                self.sock.sendto(encode("%s/%s" % (node, k), str(v)),
                                 (self.host, OSC_PORT))
                self._await_ack(time.time(), window=0.2)

            if leafed:
                got = self.describe(node, with_values=True)
                if got is None or got.get("overflow"):
                    raise WriteRejected(
                        "%s could not be read back to confirm the leaf writes" % node)
                wrong = {k: (str(v), got["params"].get(k, {}).get("value"))
                         for k, v in kv.items()
                         if got["params"].get(k, {}).get("value") != str(v)}
                if wrong:
                    raise WriteRejected(
                        "%s did not land as written: %s" % (node, ", ".join(
                            "%s wanted %r got %r" % (k, w, g)
                            for k, (w, g) in sorted(wrong.items()))))
                result["verified"] = sorted(kv)

            result["ms"] = (time.time() - t0) * 1000.0
            return result

    def identity(self):
        raw = self.payload("/$syscfg", "#")
        if raw is None:
            return {}
        _, p = parse_node(raw, True)
        return {k: p[k]["value"] for k in
                ("consolename", "$firmware", "$serial", "$cnsmdl", "$hwversion")
                if k in p}


# --------------------------------------------------------------- self-test

REAL_HASH = (
    "  in             (node)\n"
    "  clink          0                   int [0 .. 1]\n"
    "  col            9                   int [1 .. 18]\n"
    "  name           Lead Vox            string [16]\n"
    "  $name          Lead Vox        r/o string [16]\n"
    "  fdr            2.9                 fader [-oo .. 10.0 dB], 1024 steps\n"
    "  ptap           5                   list [IN, FILT, 3, 4, 5, PFL, AFL, POST]\n"
    "  tags           #M1                 string [80]\n"
)
REAL_QUERY = (
    "  consolename    string [16]\n"
    "  ipmode         list [DHCP, STATIC]\n"
    "  $firmware      string [64]\n"
    "  io             (node)\n"
)
REAL_IDENT = "WING,192.0.2.10,MyWing,wing-rack,0000000000000,3.1-0-g9f314617:release"


def selftest():
    ok = True

    def check(label, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print("  %-42s %s" % (label, "ok" if good else "FAIL got=%r want=%r" % (got, want)))

    print("codec:")
    for addr, args in [("/ch/1", ("?",)), ("/ch/1", ("name=X,col=5",)),
                       ("/", ("*",)), ("/aux/12/name", ("Accordion",))]:
        b = encode(addr, *args)
        check("%s roundtrip, %%4==0" % addr, (len(b) % 4, decode(b)), (0, (addr, list(args))))

    print("node table, values form:")
    kids, p = parse_node(REAL_HASH, True)
    check("children", kids, ["in"])
    check("name value", p["name"]["value"], "Lead Vox")
    check("name not r/o", p["name"]["readonly"], False)
    check("$name is r/o", p["$name"]["readonly"], True)
    check("fdr value", p["fdr"]["value"], "2.9")
    check("list type intact", p["ptap"]["type"], "list [IN, FILT, 3, 4, 5, PFL, AFL, POST]")
    check("tags with # in value", p["tags"]["value"], "#M1")

    print("node table, query form:")
    kids2, p2 = parse_node(REAL_QUERY, False)
    check("children", kids2, ["io"])
    check("type carried", p2["ipmode"]["type"], "list [DHCP, STATIC]")

    print("identity:")
    i = parse_ident(REAL_IDENT)
    check("name", i["name"], "MyWing")
    check("firmware", i["firmware"], "3.1-0-g9f314617:release")
    check("serial", i["serial"], "0000000000000")

    print("write payload:")
    check("kv encoding", encode_kv({"name": "Fiddle", "col": 9, "clink": 0}),
          "name=Fiddle,col=9,clink=0")

    print("\n%s" % ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


def main(argv):
    if len(argv) > 1 and argv[1] == "selftest":
        return selftest()
    if len(argv) > 1 and argv[1] == "info":
        cs = list_consoles()
        if not cs:
            print("no console answered")
            return 1
        for c in cs:
            print("%-16s %-20s %-10s fw %s" % (c["ip"], c["name"], c["model"],
                                               c["firmware"]))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
