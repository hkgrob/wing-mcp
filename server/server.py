"""WING Rack MCP - FastMCP entry.

Controls Doug's Behringer WING Rack over OSC. Built from the local-mcp-builder
templates kit; the OAuth and credential halves are absent by design, because the
desk is unauthenticated UDP on the LAN and there is no secret to store.

Layers:
    wing_transport.py  codec, discovery, node read and write, ack-serialized
    wing_profile.py    the generated command map, the writable set, validation
    server.py          this file, the tool surface

Safety, per the project CLAUDE.md and the design's section 7:
    - The profile's writable set is the boundary. 15 addresses are excluded:
      SD card format, recording session and marker deletes, scene recall,
      transport, and console-wide save. Nothing on this surface reaches them.
    - There is no library save on the WING's OSC surface at all, so this server
      cannot overwrite or remove a stored show, scene, preset or snippet.
    - Writes are ON. Doug's call 2026-08-29: channel defaults are saved on the
      desk and everything reachable here is recoverable. The template's
      default-off master switch is deliberately absent; the excluded set
      replaces it and is always on.
    - wing_push_file, added v0.3.0, deliberately carries NO confirm token
      (WING-Remote-Push_Design_v1.md, "No confirm token"): Doug's standing
      rule is that his snapshots are all saved and a scene push overwriting
      the loaded scene is a normal operation to perform rather than warn
      about. wing_push_show, which DID carry the two-step token, is retired
      in the same version - it already claimed to write a complete desk
      state while being structurally unable to (its validation ran against
      the stored profile, which cannot know a node's shape after its model
      changes), and leaving both tools on the surface invited the broken one
      being called for the same job.

Run (stdio):  python server/server.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP  # noqa: E402

import wing_transport as wt  # noqa: E402
from wing_profile import Profile  # noqa: E402
from wing_push_core import PushJob  # noqa: E402

SERVER_VERSION = "0.3.1"

mcp = FastMCP("wing-rack")

_state: dict = {"desk": None, "ident": None, "profile": None}

# Background push jobs, keyed by job id. One push at a time: a new
# wing_push_file call is refused (never queued) while another job's state is
# "running" - see wing_push_file below. Records live in memory for the life
# of this connector process; a status call for a job from before a restart
# returns not-found, the report file on disk being the durable record either
# way (design doc, "Open").
_jobs: dict = {}
_jobs_lock = threading.Lock()


def _err(message: str) -> dict:
    return {"ok": False, "error": message}


def _profile() -> Profile:
    if _state["profile"] is None:
        _state["profile"] = Profile()
    return _state["profile"]


def _desk():
    if _state["desk"] is None:
        raise RuntimeError("Not connected. Call wing_connect first.")
    return _state["desk"]


# ------------------------------------------------------------------- tools

@mcp.tool()
def verify_connection() -> dict:
    """Health check. Returns SERVER_VERSION, the loaded profile's firmware, and
    whether a desk is currently connected. Reading SERVER_VERSION back is the
    side-effect-free proof that a reload took."""
    out = {"ok": True, "server_version": SERVER_VERSION}
    try:
        p = _profile()
        out["profile_firmware"] = p.firmware
        out["profile_nodes"] = len(p.nodes)
        out["profile_counts"] = p.d.get("counts", {})
    except Exception as exc:
        out["profile_error"] = str(exc)
    d = _state["ident"]
    out["connected_to"] = d.get("name") if d else None
    out["desk_firmware"] = d.get("firmware") if d else None
    return out


@mcp.tool()
def wing_list_consoles() -> dict:
    """Every WING answering on the network: console name, address, model,
    serial and firmware. This is what the WING Edit and Mixing Station
    selectors show."""
    try:
        consoles = wt.list_consoles()
    except Exception as exc:
        return _err(str(exc))
    return {"ok": True, "count": len(consoles), "consoles": consoles}


@mcp.tool()
def wing_connect(console_name: str) -> dict:
    """Connect by console NAME, never by address. On a festival network with a
    second WING present, an address match connects to someone else's desk."""
    if not console_name:
        return _err("Provide the console name, for example MyWing.")
    try:
        found = wt.find_by_name(console_name)
    except Exception as exc:
        return _err(str(exc))
    if not found:
        seen = [c.get("name") for c in wt.list_consoles()]
        return _err("No console named %r answered. Seen: %s"
                    % (console_name, ", ".join(seen) or "none"))

    if _state["desk"]:
        _state["desk"].close()
    _state["desk"] = wt.Desk(found["ip"])
    _state["ident"] = found

    out = {"ok": True, "connected_to": found["name"], "address": found["ip"],
           "desk_firmware": found.get("firmware")}
    try:
        p = _profile()
        out["profile_firmware"] = p.firmware
        if p.firmware != found.get("firmware"):
            out["firmware_mismatch"] = (
                "The profile was generated against %s and this desk reports %s. "
                "Run wing_generate_profile before writing."
                % (p.firmware, found.get("firmware")))
    except Exception as exc:
        out["profile_error"] = str(exc)
    return out


@mcp.tool()
def wing_get(nodes: list[str], values: bool = True) -> dict:
    """Read one or more nodes. Returns each node's parameters with their
    current value, read-only flag and legal range, plus its child nodes.
    Read a family root such as /ch and you get the list of children, not
    40 channels of detail."""
    if not nodes:
        return _err("Provide at least one node address, for example /ch/1.")
    try:
        d = _desk()
    except RuntimeError as exc:
        return _err(str(exc))
    out, missing = {}, []
    for n in nodes:
        try:
            got = d.describe(n, with_values=values)
        except Exception as exc:
            return _err("reading %s: %s" % (n, exc))
        if got is None:
            missing.append(n)
        elif got.get("overflow"):
            out[n] = {"overflow": True,
                      "note": "Reply exceeds the desk's buffer. Read its "
                              "children individually."}
        else:
            out[n] = {"children": got["children"], "params": got["params"]}
    res = {"ok": True, "nodes": out}
    if missing:
        res["no_reply"] = missing
    return res


@mcp.tool()
def wing_set(node: str, values: dict, dry_run: bool = False) -> dict:
    """Write named parameters to one node, as a single OSC message however many
    keys it carries. Validation is whole-message: if any key is unknown,
    read-only, excluded, out of range, not in a list's options or over a
    string's length, nothing is sent at all.

    A channel written with name, col or icon but without clink=0 returns a
    warning: with clink=1 the channel renders its input source's identity, so
    the write stores correctly and never appears on the console."""
    if not node or not values:
        return _err("Provide a node and at least one key=value.")
    try:
        p = _profile()
    except Exception as exc:
        return _err("profile not loaded: %s" % exc)

    problems = p.check_write(node, values, desk=_state["desk"])
    if problems:
        return {"ok": False, "status": "rejected", "problems": problems,
                "note": "Nothing was sent."}

    warning = p.identity_warning(node, values)
    payload = wt.encode_kv(values)

    if dry_run:
        return {"ok": True, "status": "dry_run", "would_send":
                '%s ,s "%s"' % (node, payload), "warning": warning}

    try:
        d = _desk()
        ack = d.write_node(node, values)
    except RuntimeError as exc:
        return _err(str(exc))
    except wt.WriteRejected as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err("writing %s: %s" % (node, exc))

    out = {"ok": True, "node": node, "sent": payload,
           "ack": ack.get("ack"), "ms": round(ack.get("ms", 0), 1),
           "warning": warning}
    if ack.get("leafed"):
        out["leaf_written"] = ack["leafed"]
        out["verified"] = ack.get("verified")
        out["note"] = ("Values carrying a space or comma were written to their "
                       "own leaf addresses and read back to confirm; the node "
                       "key=value form mangles those.")
    return out


@mcp.tool()
def wing_dump(root: str = "/ch", out_path: str | None = None,
              max_nodes: int = 2000) -> dict:
    """Read a subtree to a show file. Walks child by child, because the desk
    overflows its reply buffer above roughly 20 KB and a family root such as
    /ch cannot be dumped in one message.

    The file it writes is the same shape wing_push_file and wing_push.py
    read back."""
    try:
        d = _desk()
    except RuntimeError as exc:
        return _err(str(exc))

    lines, queue, seen, count = [], [root], set(), 0
    while queue and count < max_nodes:
        node = queue.pop(0)
        if node in seen:
            continue
        seen.add(node)
        got = d.describe(node, with_values=True)
        if got is None or got.get("overflow"):
            continue
        count += 1
        base = "" if node == "/" else node
        for c in got["children"]:
            queue.append(base + "/" + c)
        kv = {k: v["value"] for k, v in got["params"].items()
              if not v["readonly"] and v["value"] is not None}
        if kv:
            lines.append("%s\t%s" % (node, wt.encode_kv_file(kv)))

    body = "\n".join(lines) + "\n"
    if out_path:
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(body)
        except Exception as exc:
            return _err("writing %s: %s" % (out_path, exc))
    return {"ok": True, "root": root, "nodes_read": count,
            "lines": len(lines), "path": out_path,
            "preview": lines[:15]}


# --- the background push: one implementation, this is the async front door ---

@mcp.tool()
def wing_push_file(path: str, dry_run: bool = False) -> dict:
    """Push a complete show file to the desk as a BACKGROUND job and return a
    job id immediately - a full scene push has run 374 to 1,999 seconds
    (state.md), far past any single MCP call's lifetime. Poll the job with
    wing_push_status(job_id). This does not return per-node results inline;
    the report file wing_push_status names is the per-node record.

    Same staged-write logic as wing_push.py, the command-line pusher: model
    written first where a node's model is changing, re-read to see the shape
    it actually landed on, then the rest of that node's values against THAT
    shape, with a read-back comparison after every write because the desk's
    OK means received, not stored. A node the file only partly specifies is
    written as far as the file goes - the live parameters it omits are
    reported on that node as "partial" rather than assumed harmless. The one
    shape still withheld outright, reported "incomplete" with nothing sent,
    is a model-bearing node whose file line names only the model (optionally
    plus on) and nothing else - the 2026-08-31 defect, where nine dynamics
    nodes shipped a model name alone and every parameter under it silently
    kept the previous scene's values (v0.3.1, Doug 2026-09-01; before this,
    ANY omitted live parameter withheld the whole node, which is why a dry
    run of a 3,232-node show file reported 1,015 nodes incomplete
    for gaps like the '$'-prefixed surface mirrors no show file is expected
    to carry).

    On any failure the job retries the WHOLE push, not the one node, up to
    three attempts, and stops early rather than spending a full pass again if
    the same node fails twice the same way, twenty nodes fail in a row (the
    desk being gone, not a value being wrong), or three attempts complete
    with no clean run. wing_push_status reports which of those happened.

    ONE PUSH AT A TIME: while a job is running, a second call is refused and
    hands back the running job's id rather than queuing.

    Deliberately carries NO confirm token, unlike the wing_push_show this
    replaces: Doug's snapshots are all saved, and a scene push overwriting
    the loaded scene is a normal operation to perform rather than warn
    about (project CLAUDE.md, 2026-08-31)."""
    if not path:
        return _err("Provide the path to a show file.")
    if not os.path.exists(path):
        return _err("show file not found: %s" % path)
    try:
        d = _desk()
    except RuntimeError as exc:
        return _err(str(exc))
    try:
        p = _profile()
    except Exception as exc:
        return _err("profile not loaded: %s" % exc)

    with _jobs_lock:
        for existing in _jobs.values():
            if existing.is_running():
                return {"ok": False, "status": "refused",
                        "error": "A push is already running.",
                        "job_id": existing.job_id,
                        "note": "Not queued. Poll wing_push_status(job_id) "
                                "with the running job's id above."}

        job_id = uuid.uuid4().hex[:12]
        ident = _state["ident"] or {}
        job = PushJob(job_id=job_id, desk=d, profile=p,
                      show_file=os.path.abspath(path), dry_run=dry_run,
                      console_name=ident.get("name", "?"), host=d.host)
        _jobs[job_id] = job
        job.start()

    return {"ok": True, "status": "started", "job_id": job_id,
            "show_file": os.path.abspath(path), "dry_run": dry_run,
            "note": "Poll wing_push_status(job_id) for progress."}


@mcp.tool()
def wing_push_status(job_id: str) -> dict:
    """Poll a background push started by wing_push_file: state (running,
    done, stopped_early or failed), which attempt it is on (of up to 3),
    nodes done against total, elapsed seconds, a running failure count, a
    running incomplete_count (nodes withheld outright - see wing_push_file),
    a running partial_count (nodes written with one or more live parameters
    left as they were because the file omitted them), and the report file's
    path - never the per-node records themselves, those are what the report
    file is for.

    A job id from before this connector process last restarted returns
    not-found; the report file already written to disk is the durable
    record either way."""
    if not job_id:
        return _err("Provide the job_id returned by wing_push_file.")
    job = _jobs.get(job_id)
    if job is None:
        return _err("No job record for %r. Job records live in memory for "
                    "the life of the connector process, so a restart clears "
                    "them - the report file on disk, if one was opened "
                    "before the restart, is the durable record." % job_id)
    return {"ok": True, **job.snapshot()}


@mcp.tool()
def wing_generate_profile(out_path: str | None = None,
                          max_nodes: int = 8000) -> dict:
    """Re-walk the desk and rebuild the command profile. This is the one
    command criterion 1 calls for after a firmware change.

    Read-only against the desk. Takes about half an hour on a full tree, at one
    query per node."""
    try:
        d = _desk()
    except RuntimeError as exc:
        return _err(str(exc))

    import time
    import wing_profile as wp

    t0 = time.time()
    nodes, queue, seen = {}, ["/"], set()
    overflow, noreply = [], []
    while queue and len(nodes) < max_nodes:
        node = queue.pop(0)
        if node in seen:
            continue
        seen.add(node)
        got = d.describe(node, with_values=True)
        if got is None:
            noreply.append(node)
            continue
        if got.get("overflow"):
            overflow.append(node)
            continue
        nodes[node] = {"children": got["children"], "params": got["params"]}
        base = "" if node == "/" else node
        for c in got["children"]:
            queue.append(base + "/" + c)

    raw = {"identity": _state["ident"] or {}, "host": _state["ident"] or {},
           "stats": {"elapsed_s": round(time.time() - t0, 1),
                     "node_count": len(nodes),
                     "overflow_nodes": overflow, "no_reply_nodes": noreply},
           "nodes": nodes}

    map_path = out_path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "wing_map_latest.json")
    with open(map_path, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=1, sort_keys=True)

    prof, prof_path = wp.build(map_path)
    _state["profile"] = None   # force a reload on next use
    return {"ok": True, "map": map_path, "profile": prof_path,
            "firmware": prof["firmware"], "counts": prof["counts"],
            "elapsed_s": raw["stats"]["elapsed_s"],
            "overflow_nodes": overflow, "no_reply_nodes": noreply}


if __name__ == "__main__":
    mcp.run()
