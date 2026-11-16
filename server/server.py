"""WING MCP - FastMCP entry.

Controls a Behringer WING - full-size, Compact or Rack - over OSC. Originally
built for Doug's WING Rack; v0.4.0 picks the command profile by the model the
connected desk reports, so a full WING or a Compact validates against its own
walk once wing_generate_profile has run on it. Built from the local-mcp-builder
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

v0.4.0:
    - Writes from wing_set are always read back and compared through
      wing_values.values_match (the desk's rounding is not a failure), and a
      reply other than OK is a rejection.
    - wing_set stages a model change the way the pusher does: model first,
      re-read, then the rest validated against the shape that model loads.
    - wing_dump and wing_generate_profile run as background jobs when they
      outlast INLINE_WAIT_S, survive overflowing nodes, and write under the
      user dir (wing_profile.USER_DIR) so a reinstall never overwrites them.
    - One background job of any kind at a time; wing_connect is refused while
      one runs, because it would close the socket the job is using.
    - Two desks answering to one name is refused, not resolved to the first.

Run (stdio):  python3 server/server.py
"""

from __future__ import annotations

import os
import sys
import threading
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP  # noqa: E402

import wing_jobs as wj  # noqa: E402
import wing_profile as wp  # noqa: E402
import wing_transport as wt  # noqa: E402
from wing_profile import Profile  # noqa: E402
from wing_push_core import PushJob, firmware_matches  # noqa: E402

SERVER_VERSION = "0.4.0"

mcp = FastMCP("wing")

_state: dict = {"desk": None, "ident": None, "profile": None, "profile_key": None}

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
    """The profile for the connected desk's model (see wing_profile's search
    order), reloaded whenever the file it resolves to changes - so a profile
    wing_generate_profile has just written is picked up on the next call."""
    model = (_state["ident"] or {}).get("model")
    path = (wp.profile_path_for(model) if model else None) or wp.DEFAULT_PROFILE
    key = (path, os.path.getmtime(path))
    if _state["profile_key"] != key:
        _state["profile"] = Profile(path)
        _state["profile_key"] = key
    return _state["profile"]


def _model_warning(p: Profile):
    """A sentence when the loaded profile was walked on a different model
    from the connected desk, else None."""
    model = (_state["ident"] or {}).get("model")
    if model and not p.model_matches(model):
        return ("This desk is a %s (%r) but no profile for it exists yet, so "
                "writes are checked against the %s profile, re-checked live where "
                "that profile would refuse them. Run wing_generate_profile on this "
                "desk to get its own." % (wp.model_name(model), model,
                                          wp.model_name(p.model)))
    return None


def _running_job():
    with _jobs_lock:
        for j in _jobs.values():
            if j.is_running():
                return j
    return None


def _busy(job) -> dict:
    return {"ok": False, "status": "refused",
            "error": "A background %s job is already running." % getattr(job, "kind", "push"),
            "job_id": job.job_id,
            "note": "One job at a time, never queued. Poll wing_job_status(job_id)."}


def _osc_value(v):
    """JSON true/false arrive as Python bools; the desk wants 1/0."""
    if isinstance(v, bool):
        return 1 if v else 0
    return v


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
    out = {"ok": True, "server_version": SERVER_VERSION,
           "user_dir": wp.USER_DIR}
    try:
        p = _profile()
        out["profile_model"] = p.model
        out["profile_path"] = p.path
        out["profile_firmware"] = p.firmware
        out["profile_nodes"] = len(p.nodes)
        out["profile_counts"] = p.d.get("counts", {})
        w = _model_warning(p)
        if w:
            out["model_mismatch"] = w
    except Exception as exc:
        out["profile_error"] = str(exc)
    out["profiles_available"] = [{"model": e["model"], "origin": e["origin"]}
                                 for e in wp.list_profiles()]
    d = _state["ident"]
    out["connected_to"] = d.get("name") if d else None
    out["desk_model"] = d.get("model") if d else None
    out["desk_firmware"] = d.get("firmware") if d else None
    j = _running_job()
    if j:
        out["running_job"] = j.job_id
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
        return _err("Provide the console name as wing_list_consoles shows it.")
    j = _running_job()
    if j:
        return _err("Job %s is running on the current connection; reconnecting "
                    "would close its socket. Wait for it to finish." % j.job_id)
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
           "desk_model": found.get("model"),
           "desk_model_name": wp.model_name(found.get("model")),
           "desk_firmware": found.get("firmware")}
    try:
        p = _profile()
        out["profile_model"] = p.model
        out["profile_path"] = p.path
        out["profile_firmware"] = p.firmware
        w = _model_warning(p)
        if w:
            out["model_mismatch"] = w
        if not firmware_matches(found.get("firmware"), p.firmware):
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
def wing_find(query: str, limit: int = 60) -> dict:
    """Search the loaded profile for commands: every address and parameter
    whose path contains all the words in `query` (for example "talk",
    "dyn thr", "io in LCL g", "fx mdl"). Numbered nodes are collapsed, so
    /ch/N/fdr is one hit standing for all 40 channels. Each hit carries its
    type and range and whether it is writable, read-only or excluded.
    Offline: reads the profile, not the desk. wing_get on an example
    address shows the live value."""
    if not query or not query.strip():
        return _err("Give one or more words to search for.")
    try:
        p = _profile()
    except Exception as exc:
        return _err("profile not loaded: %s" % exc)
    hits, total = p.find(query, limit)
    return {"ok": True, "profile_model": p.model, "total_matches": total,
            "shown": len(hits), "hits": hits}


@mcp.tool()
def wing_set(node: str, values: dict, dry_run: bool = False) -> dict:
    """Write named parameters to one node. Validation is whole-message: if any
    key is unknown, read-only, excluded, out of range, not in a list's
    options or over a string's length, nothing is sent at all.

    Every write is read back and compared, because the desk's OK means
    received, not stored; a value the desk rounds to its own step (fdr=0
    reads back 0.0) counts as landed. Values with a space or comma go to
    their own leaf address, because the node key=value form mangles them.

    Changing a model (mdl) together with that model's parameters is staged:
    the model is written alone, the node re-read, and the other keys
    validated against the shape the new model actually loads before any of
    them is sent. If they fail there, the model change stands and nothing
    else is sent - the reply says so.

    A channel or aux written with name, col or icon but without clink=0
    returns a warning: with clink=1 it renders its input source's identity,
    so the write stores correctly and never appears on the console."""
    if not node or not values:
        return _err("Provide a node and at least one key=value.")
    values = {k: _osc_value(v) for k, v in values.items()}
    try:
        p = _profile()
    except Exception as exc:
        return _err("profile not loaded: %s" % exc)
    desk = _state["desk"]
    model_warning = _model_warning(p)

    # Model change with parameters: stage it.
    rest = {k: v for k, v in values.items() if k != "mdl"}
    if "mdl" in values and rest and desk is not None:
        live = desk.describe(node, with_values=True)
        if live and not live.get("overflow"):
            live_mdl = live["params"].get("mdl", {}).get("value")
            if live_mdl is not None and str(live_mdl) != str(values["mdl"]):
                return _set_model_change(p, desk, node, str(values["mdl"]), rest,
                                         live_mdl, dry_run, model_warning)

    problems = p.check_write(node, values, desk=desk)
    if problems:
        return {"ok": False, "status": "rejected", "problems": problems,
                "note": "Nothing was sent."}

    warning = p.identity_warning(node, values)
    batched = {k: v for k, v in values.items() if not wt.needs_leaf_write(v)}
    leafed = {k: v for k, v in values.items() if wt.needs_leaf_write(v)}
    would = []
    if batched:
        would.append('%s ,s "%s"' % (node, wt.encode_kv(batched)))
    would += ['%s/%s ,s "%s"' % (node, k, v) for k, v in leafed.items()]

    if dry_run:
        return {"ok": True, "status": "dry_run", "would_send": would,
                "warning": warning, "model_mismatch": model_warning}

    try:
        d = _desk()
        ack = d.write_node(node, values, verify=True)
    except RuntimeError as exc:
        return _err(str(exc))
    except wt.WriteRejected as exc:
        return {"ok": False, "error": str(exc),
                "note": "Some keys may have landed (UDP has no rollback). "
                        "wing_get the node to see its current state."}
    except Exception as exc:
        return _err("writing %s: %s" % (node, exc))

    out = {"ok": True, "node": node, "sent": would,
           "ack": ack.get("ack"), "landed": ack.get("landed"),
           "ms": round(ack.get("ms", 0), 1), "warning": warning}
    if ack.get("leafed"):
        out["leaf_written"] = ack["leafed"]
    if model_warning:
        out["model_mismatch"] = model_warning
    return out


def _set_model_change(p, desk, node, target_mdl, rest, live_mdl, dry_run,
                      model_warning):
    excluded = ["%s/%s is excluded: %s" % (node, k, wp.excluded_reason(node, k))
                for k in ["mdl"] + list(rest) if wp.excluded_reason(node, k)]
    if excluded:
        return {"ok": False, "status": "rejected", "problems": excluded,
                "note": "Nothing was sent."}
    if dry_run:
        return {"ok": True, "status": "dry_run",
                "would_send": ['%s ,s "mdl=%s"' % (node, target_mdl),
                               "(then re-read, and %s validated against the "
                               "shape %s loads)" % (", ".join(sorted(rest)), target_mdl)],
                "note": "The other keys cannot be validated until the model has "
                        "loaded on this node; a dry run does not change the model.",
                "model_mismatch": model_warning}
    try:
        desk.write_node(node, {"mdl": target_mdl}, verify=True)
    except wt.WriteRejected as exc:
        return _err("model write rejected, nothing else sent: %s" % exc)
    live = desk.describe(node, with_values=True)
    if live is None or live.get("overflow"):
        return _err("mdl changed %s -> %s but the node could not be re-read; "
                    "nothing else was sent" % (live_mdl, target_mdl))
    lp, problems = live["params"], []
    for k, v in rest.items():
        spec = lp.get(k)
        if spec is None:
            problems.append("%s has no parameter %s under %s" % (node, k, target_mdl))
        elif spec.get("readonly"):
            problems.append("%s/%s is read-only" % (node, k))
        else:
            why = wp.validate(wp.parse_type(spec.get("type")), v)
            if why:
                problems.append("%s/%s=%r %s" % (node, k, v, why))
    if problems:
        return {"ok": False, "status": "model_changed_rest_rejected",
                "model_change": {"from": live_mdl, "to": target_mdl},
                "problems": problems,
                "note": "The model change landed; none of the other keys were sent."}
    try:
        ack = desk.write_node(node, rest, verify=True)
    except wt.WriteRejected as exc:
        return {"ok": False, "model_change": {"from": live_mdl, "to": target_mdl},
                "error": str(exc),
                "note": "The model change landed; some other keys may have too."}
    return {"ok": True, "node": node,
            "model_change": {"from": live_mdl, "to": target_mdl},
            "landed": ack.get("landed"), "model_mismatch": model_warning}


def _start_job(job):
    """Register and start a background job, wait INLINE_WAIT_S, and return
    the job's result if it finished, else its id."""
    with _jobs_lock:
        for existing in _jobs.values():
            if existing.is_running():
                return _busy(existing)
        _jobs[job.job_id] = job
        job.start()
    if job.wait(wj.INLINE_WAIT_S):
        snap = job.snapshot()
        if snap["state"] == "done":
            return {"ok": True, "status": "done", "job_id": job.job_id,
                    **snap["result"]}
        return _err(snap.get("error") or "job failed")
    return {"ok": True, "status": "running", "job_id": job.job_id,
            "note": "Still walking after %ds; poll wing_job_status(job_id)."
                    % wj.INLINE_WAIT_S}


@mcp.tool()
def wing_dump(root: str = "/ch", out_path: str | None = None,
              max_nodes: int = 4000) -> dict:
    """Read a subtree to a show file. Walks child by child, because the desk
    overflows its reply buffer above roughly 20 KB; a node that overflows is
    re-read names-only so its children are still walked, and the file's
    header names anything not captured.

    Small subtrees return their result directly. A large one (a full /ch is
    about 1,800 nodes) keeps running in the background and returns a job id
    for wing_job_status. out_path defaults to the user dir's dumps/ folder;
    a relative path is taken under that folder. The file is the same shape
    wing_push_file reads back."""
    try:
        d = _desk()
    except RuntimeError as exc:
        return _err(str(exc))
    path = wj.resolve_out_path(out_path, root)
    job = wj.DumpJob(uuid.uuid4().hex[:12], d, root, path, max_nodes,
                     _state["ident"])
    res = _start_job(job)
    if res.get("status") == "running":
        res["path"] = path
    return res


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
                return _busy(existing)

        job_id = uuid.uuid4().hex[:12]
        ident = _state["ident"] or {}
        job = PushJob(job_id=job_id, desk=d, profile=p,
                      show_file=os.path.abspath(path), dry_run=dry_run,
                      console_name=ident.get("name", "?"), host=d.host)
        _jobs[job_id] = job
        job.start()

    out = {"ok": True, "status": "started", "job_id": job_id,
           "show_file": os.path.abspath(path), "dry_run": dry_run,
           "note": "Poll wing_push_status(job_id) for progress."}
    w = _model_warning(p)
    if w:
        out["model_mismatch"] = w
    return out


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
def wing_job_status(job_id: str) -> dict:
    """Poll any background job - a push, a dump or a profile walk - by the id
    its tool returned. A finished dump or profile walk carries its result
    here; a push points at its report file."""
    return wing_push_status(job_id)


@mcp.tool()
def wing_generate_profile(max_nodes: int = 12000) -> dict:
    """Walk the whole connected desk and build its command profile: every
    node, every parameter, its type and range, read live. Run it once on any
    desk whose model has no profile yet (a full WING or a Compact, when the
    bundle ships only the Rack's), and again after a firmware change.

    Read-only against the desk. Runs as a background job - a full tree is
    about 6,000 nodes - and returns a job id for wing_job_status. The
    profile is written to the user dir's profiles/<model>.json, outside the
    bundle, so a reinstall never overwrites it, and the server switches to
    it as soon as it lands. The raw map is kept beside it in maps/."""
    try:
        d = _desk()
    except RuntimeError as exc:
        return _err(str(exc))
    ident = dict(_state["ident"] or {})
    job = wj.ProfileJob(uuid.uuid4().hex[:12], d, ident, max_nodes)
    res = _start_job(job)
    res["model"] = ident.get("model")
    res["will_write"] = wp.profile_path_for(ident.get("model"), write=True)
    return res


if __name__ == "__main__":
    mcp.run()
