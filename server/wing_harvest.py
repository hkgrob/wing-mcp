#!/usr/bin/env python3
"""Harvest a subtree of the desk to a show file.

Same job as the wing_dump tool, run as a command instead. A full /ch walk is
1,761 nodes and takes several minutes, which outlives an MCP tool call; this
does not care how long it takes and prints progress while it works.

    python3 wing_harvest.py BandA-Baseline
    python3 wing_harvest.py BandB-Baseline --root /ch
    python3 wing_harvest.py BandA-Baseline --root /bus --root /dca

With no --root it takes the whole post-show debrief set, DEBRIEF_ROOTS below.

Read only. Writes one show file per root into ../outputs/, named
<label>_<root>_v1.wing, plus a summary of what it found.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wing_transport as wt   # noqa: E402

OUTDIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "outputs")

# What a post-show debrief has to diff against what was pushed. /ch, /bus,
# /main, /mtx and /dca are the layers the templates write. /fx holds the
# effects rack, which no harvest before 2026-08-31 captured, so an effects
# setting survived only as long as the scene it sat in. /io carries the
# source-side preamp gain, which is the control moved most at soundcheck and
# lives nowhere else. /aux carries talkback on Aux 8 with real processing on
# it. /cfg carries the talkback assignment and level, /mgrp the mute group
# names and states, both of which a template writes.
DEBRIEF_ROOTS = ["/ch", "/aux", "/bus", "/main", "/mtx", "/dca",
                 "/fx", "/mgrp", "/cfg", "/io"]


def walk_tree(desk, root, max_nodes=4000, progress=None):
    """Breadth-first walk of a subtree, one query per node. Returns
    (nodes, stats): nodes is a list of (address, got, values_known) in visit
    order, where got is Desk.describe()'s dict.

    Shared by the harvest CLI, the wing_dump tool and the profile walk in
    wing_generate_profile, so all three survive an overflow the same way.

    The desk refuses a reply it cannot fit and returns an error in its
    place. That error carries no child list, and the child list is the only
    way this walk knows where to go next - so taking the overflow at face
    value loses every node BELOW this one and still exits clean, which is
    indistinguishable from a full walk. Names only drops the value column
    and fits where the full answer did not, so the walk continues past it.
    That node's own values are the only thing lost, and they are recorded
    as lost (stats["no_values"])."""
    out, queue, seen = [], [root], set()
    overflow, noreply, novalues = [], [], []
    t0 = time.time()
    while queue and len(out) < max_nodes:
        node = queue.pop(0)
        if node in seen:
            continue
        seen.add(node)
        got = desk.describe(node, with_values=True)
        if got is None:
            noreply.append(node)
            continue
        values_known = True
        if got.get("overflow"):
            got = desk.describe(node, with_values=False)
            if got is None or got.get("overflow"):
                overflow.append(node)
                continue
            novalues.append(node)
            values_known = False
        out.append((node, got, values_known))
        base = "" if node == "/" else node
        for c in got["children"]:
            queue.append(base + "/" + c)
        if progress:
            progress(len(out), len(queue))
    return out, {"nodes": len(out), "overflow": overflow, "no_reply": noreply,
                 "no_values": novalues, "unread": len(queue),
                 "seconds": round(time.time() - t0, 1)}


def show_lines(walked):
    """Show-file lines for a walk: writable, non-null values only."""
    lines = []
    for node, got, values_known in walked:
        if not values_known:
            continue
        kv = {k: v["value"] for k, v in got["params"].items()
              if not v["readonly"] and v["value"] is not None}
        if kv:
            lines.append("%s\t%s" % (node, wt.encode_kv_file(kv)))
    return lines


def loss_header(stats):
    """Comment lines naming everything a dump does NOT contain. A dump that
    lost a subtree otherwise looks exactly like a full one."""
    header = []
    for label, nodes in (("values not captured, names only", stats["no_values"]),
                         ("SUBTREE LOST, overflow at", stats["overflow"]),
                         ("no reply from", stats["no_reply"])):
        if nodes:
            header.append("# %s: %s" % (label, ", ".join(nodes)))
    if stats["unread"]:
        header.append("# INCOMPLETE: node cap reached, %d node(s) never "
                      "visited" % stats["unread"])
    return header


def harvest(desk, root, log, max_nodes=4000):
    def progress(read, queued):
        if read % 100 == 0:
            log("    %4d nodes, %4d queued" % (read, queued))
    walked, stats = walk_tree(desk, root, max_nodes, progress)
    lines = show_lines(walked)
    stats["lines"] = len(lines)
    return lines, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("label", help="what this desk state is, e.g. BandA-Baseline")
    ap.add_argument("--root", action="append", default=None,
                    help="subtree to harvest; repeatable. Default is the "
                         "post-show debrief set: " + " ".join(DEBRIEF_ROOTS))
    ap.add_argument("--host")
    ap.add_argument("--console", default=os.environ.get("WING_CONSOLE"),
                    help="console name to match (default: $WING_CONSOLE)")
    a = ap.parse_args()
    roots = a.root or DEBRIEF_ROOTS

    def log(s=""):
        print(s)

    host = a.host
    ident = {}
    if not host:
        if not a.console:
            seen = [c.get("name") for c in wt.list_consoles()]
            print("Name the desk with --console or $WING_CONSOLE. Answering: %s"
                  % (", ".join(seen) or "none"))
            return 1
        try:
            found = wt.find_by_name(a.console)
        except wt.AmbiguousConsole as e:
            print(e)
            return 1
        if not found:
            print("No console named %r answered." % a.console)
            return 1
        host, ident = found["ip"], found
    log("desk %s  %s  fw %s" % (host, ident.get("name", ""), ident.get("firmware", "?")))

    # The desk's own idea of what is loaded. It has been seen to lag a manual
    # console recall, so it is recorded rather than trusted.
    d = wt.Desk(host)
    raw = d.payload("/$ctl/lib", "#")
    active = ""
    if raw:
        _, p = wt.parse_node(raw, True)
        active = (p.get("$active", {}) or {}).get("value", "")
    log("desk reports active snapshot: %r  (known to lag a manual recall)" % active)

    os.makedirs(OUTDIR, exist_ok=True)
    for root in roots:
        log("\nharvesting %s" % root)
        lines, stats = harvest(d, root, log)
        name = "%s_%s_v1.wing" % (a.label, root.strip("/").replace("/", "-") or "root")
        path = os.path.join(OUTDIR, name)
        header = [
            "# %s - harvested from %s" % (a.label, ident.get("name", host)),
            "# %s   firmware %s" % (time.strftime("%Y-%m-%d %H:%M"),
                                    ident.get("firmware", "?")),
            "# desk reported active snapshot: %s" % (active or "(none)"),
            "# root %s, %d nodes read, %d lines" % (root, stats["nodes"], stats["lines"]),
            "# Pairs are TAB separated: a value may contain a comma.",
        ]
        # Anything this file does NOT contain says so inside the file.
        header += loss_header(stats)
        header.append("")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(header + lines) + "\n")
        log("  %d nodes, %d lines, %.0fs -> %s"
            % (stats["nodes"], stats["lines"], stats["seconds"], path))
        if stats["no_values"]:
            log("  names only, values not captured at: %s"
                % ", ".join(stats["no_values"]))
        if stats["overflow"]:
            log("  SUBTREE LOST, overflow survived names-only at: %s"
                % ", ".join(stats["overflow"]))
        if stats["no_reply"]:
            log("  no reply from %d node(s)" % len(stats["no_reply"]))
        if stats["unread"]:
            log("  INCOMPLETE: node cap reached, %d node(s) never visited"
                % stats["unread"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
