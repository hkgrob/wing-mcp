#!/usr/bin/env python3
"""Push a show file to the desk. The write-side twin of wing_harvest.py.

Same reason as the read side: a full scene is 2,637 nodes and outlives an
MCP tool call, so this runs as a command instead and prints progress while
it works.

    py wing_push.py ../outputs/BandA_Show_v1.wing --dry-run
    py wing_push.py ../outputs/BandA_Show_v1.wing
    py wing_push.py ../outputs/BandA_Show_v1.wing --only /ch/1 --limit 5 --dry-run
    py wing_push.py ../outputs/BandA_Show_v1.wing --host 192.0.2.10

This is now a thin command-line wrapper over wing_push_core.py, which also
backs the wing_push_file / wing_push_status MCP tools in server.py (see
WING-Remote-Push_Design_v1.md - "one implementation, two front doors"). The
staged-write logic, the CLI's arguments, its stdout output and its exit
codes are unchanged from before that split. What changed under the hood:
the report is now written incrementally (see wing_push_core.IncrementalReport)
instead of built once at the end from an in-memory list, so a process killed
by anything other than the Ctrl-C this CLI already handles still leaves a
report naming every node that landed.

Two things make a plain replay of a show file unsafe:

1. A node's parameter set is not fixed - it depends on the model currently
   loaded (see wing_transport.py and the project CLAUDE.md). Writing
   mdl=76LA,in=5.0 in one message is validated against whatever model is
   loaded BEFORE the write, so the desk rejects it. The fix: write mdl
   alone, re-read the node to see the model it actually landed on, then
   write the rest against THAT shape.
2. The desk's OK on a node write means the message was received, never
   that it was stored (wing_transport.needs_leaf_write and the node
   key=value mangling it exists to route around). write_node() only reads
   a node back to confirm when at least one value needed leaf routing -
   a write whose keys are all "safe" gets no confirmation at all from that
   function. This script does its own read-back-and-compare after every
   node write, batched or leafed, because "OK" is not evidence here.

Every write is staged against the node's LIVE shape, read fresh, never
against the stored profile (server/wing_profile.json) - that file is a
snapshot from 2026-08-29 and the model on any given node today may not be
the model it was generated under. The profile is still authoritative for
the fifteen excluded addresses, because those are policy, not shape, and
are never re-checked against the desk.

Read only where the file says to read, write only where the file says to
write. Nothing here invents a value. --dry-run performs every read and
every shape decision and sends no packet that mutates desk state.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wing_profile as wp    # noqa: E402
import wing_transport as wt  # noqa: E402
from wing_push_core import (  # noqa: E402
    NODE_PACE_S, IncrementalReport, firmware_matches, parse_show_file,
    process_node, read_active_lib, read_stat,
)


# ------------------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("show_file", help="path to a .wing file, as produced by "
                                      "wing_harvest.py or a template generator")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve every write against the live desk but send nothing")
    ap.add_argument("--console", default="MyWing",
                    help="match the console by name, never by address")
    ap.add_argument("--host", help="skip discovery and talk to this IP directly")
    ap.add_argument("--limit", type=int, default=None,
                    help="process at most this many node lines (after --only)")
    ap.add_argument("--only", default=None,
                    help="only process node addresses starting with this prefix, "
                         "e.g. --only /ch/1")
    a = ap.parse_args()

    def log(s=""):
        print(s, flush=True)

    host = a.host
    ident = {}
    if not host:
        found = wt.find_by_name(a.console)
        if not found:
            log("No console named %r answered." % a.console)
            return 1
        host, ident = found["ip"], found
    log("desk %s  %s  fw %s" % (host, ident.get("name", ""), ident.get("firmware", "?")))

    profile = wp.Profile()
    counts_p = profile.d["counts"]
    log("profile firmware %s  (%d nodes, %d writable, %d excluded)" % (
        profile.firmware, len(profile.nodes), counts_p["writable"], counts_p["excluded"]))

    if not os.path.exists(a.show_file):
        log("show file not found: %s" % a.show_file)
        return 1

    d = wt.Desk(host)

    live_ident = d.identity()
    live_fw = live_ident.get("$firmware")
    firmware_mismatch = bool(live_fw) and not firmware_matches(live_fw, profile.firmware)
    if firmware_mismatch:
        log("WARNING: live firmware %r does not match the profile's %r - "
            "types, ranges and the writable/excluded split may be stale. "
            "Regenerate wing_profile.json before trusting this run." %
            (live_fw, profile.firmware))

    before_date, before_time = read_stat(d)
    active_before = read_active_lib(d)
    log("desk clock before push: %s %s" % (before_date, before_time))
    log("active snapshot before push: %r  (known to lag a manual recall)" % active_before)

    all_nodes = []
    parse_errors = 0
    for lineno, node, kv, problems in parse_show_file(a.show_file):
        if problems or node is None:
            parse_errors += 1
        if a.only and node is not None and not node.startswith(a.only):
            continue
        all_nodes.append((lineno, node, kv, problems))
    if a.limit is not None:
        all_nodes = all_nodes[:a.limit]

    log("%d node line(s) to process%s%s" % (
        len(all_nodes), " (dry run, nothing will be sent)" if a.dry_run else "",
        (", %d parse error(s) in the file overall" % parse_errors) if parse_errors else ""))

    report = IncrementalReport(a.show_file, a.dry_run, {
        "console": ident.get("name", a.console), "host": host,
        "firmware_live": live_fw, "firmware_profile": profile.firmware,
        "firmware_mismatch": firmware_mismatch,
        "before_date": before_date, "before_time": before_time,
        "active_before": active_before,
    })
    log("report opened, written incrementally as nodes land: %s" % report.path)

    model_examples = {}
    counts = {"written": 0, "skipped": 0, "failed": 0, "incomplete": 0,
              "parse_error": 0, "partial": 0}
    wall_t0 = time.time()
    interrupted = False
    total_entries = 0

    try:
        for i, (lineno, node, kv, problems) in enumerate(all_nodes, 1):
            if problems or node is None:
                entry = {"node": node, "line": lineno, "status": "failed",
                        "detail": "parse error: %s" % "; ".join(problems),
                        "written": {}, "skipped": {}, "warnings": []}
                counts["parse_error"] += 1
                counts["failed"] += 1
            else:
                entry = process_node(d, profile, node, dict(kv), a.dry_run, model_examples)
                entry["line"] = lineno
                counts[entry["status"]] = counts.get(entry["status"], 0) + 1
                # Alongside its own status (almost always "written"), not a
                # replacement for it - see wing_push_core._run_attempts.
                if entry.get("partial"):
                    counts["partial"] += 1
            report.write_node(entry)
            total_entries += 1

            if entry["status"] == "failed":
                log("  [%d/%d] %-28s FAILED: %s" % (i, len(all_nodes), node, entry.get("detail", "")))
            elif entry["status"] == "incomplete":
                log("  [%d/%d] %-28s INCOMPLETE: %s" % (i, len(all_nodes), node, entry.get("detail", "")))
            elif entry["status"] == "skipped" and not entry.get("written"):
                log("  [%d/%d] %-28s skipped: %s" % (i, len(all_nodes), node, entry.get("detail", "")))
            elif entry.get("partial"):
                log("  [%d/%d] %-28s partial: %d live key(s) left as-is: %s" % (
                    i, len(all_nodes), node, len(entry["partial"]), ", ".join(entry["partial"])))

            if i % 100 == 0 or i == len(all_nodes):
                elapsed = time.time() - wall_t0
                rate = i / elapsed if elapsed else 0.0
                log("    %5d/%5d nodes, %6.0fs elapsed, %.2f nodes/s, "
                    "written=%d partial=%d skipped=%d incomplete=%d failed=%d" % (
                        i, len(all_nodes), elapsed, rate,
                        counts.get("written", 0), counts.get("partial", 0),
                        counts.get("skipped", 0),
                        counts.get("incomplete", 0), counts.get("failed", 0)))

            time.sleep(NODE_PACE_S)
    except KeyboardInterrupt:
        interrupted = True
        log("\nINTERRUPTED - the desk has no rollback, so the report already on "
            "disk names exactly what landed before this point.")

    after_date, after_time = read_stat(d)
    active_after = read_active_lib(d)
    wall_elapsed = time.time() - wall_t0

    log("")
    log("desk clock after push:  %s %s" % (after_date, after_time))
    log("active snapshot after push: %r" % active_after)
    log("=" * 64)
    log("WALL-CLOCK ELAPSED: %.1f seconds (%.2f minutes) for %d node line(s)%s" % (
        wall_elapsed, wall_elapsed / 60.0, total_entries,
        " [DRY RUN, no writes sent]" if a.dry_run else
        (" [INTERRUPTED]" if interrupted else "")))
    log("=" * 64)
    log("written=%d  partial=%d  skipped=%d  incomplete=%d  failed=%d  parse_errors=%d  (of %d processed, %d in file)" % (
        counts.get("written", 0), counts.get("partial", 0), counts.get("skipped", 0),
        counts.get("incomplete", 0), counts.get("failed", 0), counts.get("parse_error", 0),
        total_entries, len(all_nodes)))

    report.write_footer({
        "after_date": after_date, "after_time": after_time,
        "active_after": active_after, "wall_elapsed": wall_elapsed,
        "counts": counts, "total": total_entries,
        "interrupted": interrupted,
        "stop_reason": "keyboard interrupt" if interrupted else None,
    })
    report.close()
    log("full per-node report: %s" % report.path)

    d.close()
    return 1 if (counts.get("failed", 0) or interrupted) else 0


if __name__ == "__main__":
    sys.exit(main())
