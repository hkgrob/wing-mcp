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

import json
import os
import sys
import threading
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP  # noqa: E402

import wing_catalog as wc  # noqa: E402
import wing_jobs as wj  # noqa: E402
import wing_profile as wp  # noqa: E402
import wing_transport as wt  # noqa: E402
from wing_profile import Profile  # noqa: E402
from wing_push_core import PushJob, firmware_matches  # noqa: E402

SERVER_VERSION = "0.6.0"

mcp = FastMCP("wing")

# Tool annotations tell the client which tools only read the desk and
# which change it, so a person can approve each group once instead of every
# call. Older mcp packages without annotation support fall back to a plain
# registration rather than failing to start.
try:
    from mcp.types import ToolAnnotations as _TA  # noqa: E402

    def _tool(read_only, title=None):
        return mcp.tool(title=title, annotations=_TA(
            title=title, readOnlyHint=read_only, destructiveHint=False,
            idempotentHint=read_only, openWorldHint=False))
except Exception:  # pragma: no cover
    def _tool(read_only, title=None):
        return mcp.tool()

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

@_tool(True, 'Health check')
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


@_tool(True, 'List consoles')
def wing_list_consoles() -> dict:
    """Every WING answering on the network: console name, address, model,
    serial and firmware. This is what the WING Edit and Mixing Station
    selectors show."""
    try:
        consoles = wt.list_consoles()
    except Exception as exc:
        return _err(str(exc))
    return {"ok": True, "count": len(consoles), "consoles": consoles}


@_tool(True, 'Connect to console')
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


@_tool(True, 'Read nodes')
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


@_tool(True, 'Search commands')
def wing_find(query: str, limit: int = 60, include_models: bool = True) -> dict:
    """Search the loaded profile for commands: every address and parameter
    whose path contains all the words in `query` (for example "talk",
    "dyn thr", "io in LCL g", "fx mdl"). Numbered nodes are collapsed, so
    /ch/N/fdr is one hit standing for all 40 channels. Each hit carries its
    type and range and whether it is writable, read-only or excluded.
    Offline: reads the profile, not the desk. wing_get on an example
    address shows the live value. With include_models (default), also
    searches the model catalog, so controls of EQ/gate/dynamics/FX models
    not loaded on the desk right now are found too (under model_hits)."""
    if not query or not query.strip():
        return _err("Give one or more words to search for.")
    try:
        p = _profile()
    except Exception as exc:
        return _err("profile not loaded: %s" % exc)
    hits, total = p.find(query, limit)
    out = {"ok": True, "profile_model": p.model, "total_matches": total,
           "shown": len(hits), "hits": hits}
    if include_models:
        chits, ctotal = wc.find(query.split(), limit)
        out["model_hits"] = chits
        out["model_matches"] = ctotal
    return out


@_tool(True, 'List models')
def wing_models(section: str | None = None, model: str | None = None) -> dict:
    """The processing models the WING offers and their controls, from the
    bundled model catalog (libwing, firmware 3.0.5) - including models not
    currently loaded anywhere on the desk.

    No arguments: the sections (ch/eq, ch/gate, ch/dyn, ch/flt, bus/dyn,
    fx, ...). section only: that section's models. section and model: every
    control of that model with its type and range. Reference only; the
    desk validates every write."""
    cat = wc.catalog()["sections"]
    if not section:
        return {"ok": True, "source": wc.catalog().get("source"),
                "sections": {k: len(v["models"]) for k, v in sorted(cat.items())}}
    if section not in cat:
        return _err("No section %r. Sections: %s" % (section, ", ".join(sorted(cat))))
    if not model:
        return {"ok": True, "section": section, "models": wc.models(section)}
    m = cat[section]["models"].get(model)
    if not m:
        return _err("No model %r in %s. Models: %s" % (model, section, ", ".join(sorted(cat[section]["models"]))))
    return {"ok": True, "section": section, "model": model, "label": m["label"],
            "controls": m["params"],
            "note": "Set the model with wing_set (mdl plus these keys is staged automatically)."}


@_tool(False, 'Write parameters')
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


@_tool(False, 'Dump to show file')
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

@_tool(False, 'Push show file')
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


@_tool(True, 'Push status')
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


@_tool(True, 'Job status')
def wing_job_status(job_id: str) -> dict:
    """Poll any background job - a push, a dump or a profile walk - by the id
    its tool returned. A finished dump or profile walk carries its result
    here; a push points at its report file."""
    return wing_push_status(job_id)


@_tool(False, 'Build profile')
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


# ------------------------------------------------- meters and RTA (native)

import wing_native as wn  # noqa: E402

_meter_lock = threading.Lock()


def _meter_collect(strips, seconds, rta=False):
    d = _desk()
    coll, layout = wn.collection(strips, rta=rta)
    sess = wn.MeterSession(d.host)
    with _meter_lock:
        frames = sess.collect(coll, max(0.5, min(float(seconds), 30.0)))
    meters, spectrum = wn.summarize(frames, layout)
    diag = {"packets": sess.packets, "foreign_packets": sess.foreign}
    if not frames:
        diag["first_raw_packet"] = sess.raw_first
        diag["hint"] = ("No meter data arrived. The desk sends meters by UDP to this "
                        "computer: check the macOS firewall and Little Snitch allow "
                        "incoming UDP for this Python, and that the desk can reach "
                        "this computer's address (same subnet, no client isolation).")
    return meters, spectrum, diag


@_tool(True, 'Read meters')
def wing_meters(strips: list[str] | str = "ch:1-40", seconds: float = 2.0,
                detail: bool = False) -> dict:
    """Live levels from the desk's meter stream, over a short window.

    strips: "ch:1-8,12", "bus:1-4", "main:1", "aux", "mtx", "dca", or a
    list of them. Per strip: input and output peak in dBFS, input average,
    and gate/compressor gain reduction in dB where there is any; strips
    below -90 dBFS are listed under "silent". detail=true returns every
    meter (L/R, keys, averages). Read-only. Uses the WING native protocol
    (TCP 2222 out, UDP back to this computer), not OSC."""
    try:
        parsed = wn.parse_strips(strips)
        meters, _, diag = _meter_collect(parsed, seconds)
    except RuntimeError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err("meters: %s: %s" % (type(exc).__name__, exc))
    if not detail:
        meters, silent = _compact_meters(meters)
        return {"ok": bool(meters) or bool(silent), "seconds": seconds,
                "units": "dBFS peaks; gr = gain reduction dB",
                "meters": meters, "silent": silent, **diag}
    return {"ok": bool(meters), "seconds": seconds, "meters": meters, **diag}


def _compact_meters(meters, floor=-90.0):
    """One short row per strip, and strips with nothing on them collapsed
    into a list - a 40-channel read stays readable."""
    out, silent = {}, []
    for k, m in meters.items():
        ip, op = m.get("input_peak_db"), m.get("output_peak_db")
        if (ip is None or ip <= floor) and (op is None or op <= floor):
            silent.append(k)
            continue
        row = {"in": ip, "out": op}
        if "in_l" in m:
            row["in_avg"] = max(m["in_l"]["avg"], m.get("in_r", m["in_l"])["avg"])
        for src, dst in (("gate_reduction_db", "gate_gr"), ("dyn_reduction_db", "comp_gr")):
            if m.get(src, {}).get("max"):
                row[dst] = m[src]["max"]
        for f in ("pre_l", "post_l"):
            if f in m:
                row[f[:-2]] = m[f]["peak"]
        out[k] = row
    return out, silent


_RTA_TAPS = ["IN", "POST", "FILT", "PREEQ", "POSTEQ", "PREFDR"]


@_tool(False, 'Read RTA')
def wing_rta(source: str = "ch:1", tap: str = "PREEQ", seconds: float = 3.0,
             detector: str = "RMS", full: bool = False) -> dict:
    """Read the desk's 120-band RTA (semitones, 20.6 Hz to 19.9 kHz) on one
    strip, averaged over a window. source: "ch:9", "bus:2", "main:1"...
    tap: where in the strip to listen - PREEQ shows the raw source (find
    resonances to cut), POSTEQ shows what the EQ produces, IN/FILT/PREFDR/
    POST also accepted.

    Points the desk's metering RTA (/cfg/rta rtasrc/rtatap/rtadet) at the
    source for the read and restores the previous setting afterwards. That
    is the meter RTA, not the on-screen EQ analyser.

    Returns third-octave bands, the bands that stand out above their
    neighbours, and with full=true all 120 values. On music the stand-out
    bands are usually the notes being played (a run like 98/196/294 Hz is a
    harmonic series, not a fault); they mean "resonance" on a single mic
    being spoken or sung into, or on a frequency that rings."""
    kind, _, num = source.partition(":")
    kind = kind.strip().lower()
    if kind not in wn.RTA_SRC_BASE or not num.isdigit():
        return _err("source must look like ch:9, aux:1, bus:2, main:1 or mtx:1")
    idx = wn.RTA_SRC_BASE[kind] + int(num)
    tap = tap.upper()
    if tap not in _RTA_TAPS:
        return _err("tap must be one of %s" % ", ".join(_RTA_TAPS))
    try:
        d = _desk()
        before = d.describe("/cfg/rta", with_values=True)
        prev = {k: before["params"][k]["value"] for k in ("rtasrc", "rtatap", "rtadet")
                if before and k in before.get("params", {})}
        d.write_node("/cfg/rta", {"rtasrc": idx, "rtatap": tap, "rtadet": detector.upper()},
                     verify=True)
        try:
            import time as _t
            _t.sleep(0.4)
            _, spectrum, diag = _meter_collect([], seconds, rta=True)
        finally:
            if prev:
                try:
                    d.write_node("/cfg/rta", prev)
                except Exception:
                    pass
    except RuntimeError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err("rta: %s: %s" % (type(exc).__name__, exc))
    if not spectrum:
        return {"ok": False, "source": source, "tap": tap, **diag}
    out = {"ok": True, "source": source, "tap": tap, "seconds": seconds,
           "note": "Levels in dB as the desk reports them (1/256 dB units). "
                   "Compare shapes, not absolute values.",
           "third_octave": wn.rta_bands(spectrum),
           "resonances": wn.rta_peaks(spectrum), **diag}
    if full:
        out["semitones"] = [{"hz": round(f, 1), "avg": a, "peak": p}
                            for f, (a, p) in zip(wn.RTA_FREQS, spectrum)]
    return out


@_tool(False, 'Gain check')
def wing_gain_check(channels: str = "ch:1-40", target_peak_db: float = -13.0,
                    tolerance_db: float = 1.0, apply: bool = False,
                    seconds: float = 6.0, max_passes: int = 6,
                    max_step_db: float = 12.0, compensate_fader: bool = True,
                    fine_db: float = 3.0, trim_limit_db: float = 6.0) -> dict:
    """Soundcheck gain staging in ONE call. Measures each channel's input
    peak while it is played and, with apply=true, adjusts and re-measures
    in a loop until every channel with signal sits within
    target_peak_db +/- tolerance_db (dBFS), or max_passes is reached.

    Controls: where the source has a preamp (stage box, local inputs), the
    preamp takes coarse moves in its 0.5 dB steps and the channel trim
    smooths the rest in 0.1 dB steps - a correction smaller than fine_db
    goes to the trim alone, a larger one to the preamp with the remainder
    on the trim. The trim is kept within +/- trim_limit_db; past that the
    preamp takes the move. Sources without a preamp (aux line inputs, USB,
    modules, playback) use the trim only. The trim is per channel, so a
    fine move never touches other channels or direct outs sharing the
    input; a preamp move does. With compensate_fader (default), the channel fader
    moves the opposite way so the room and post-fader sends keep their
    level; pre-fader sends follow the input change.

    Built for programme material, which swings a couple of dB between
    windows: a channel stops as soon as it is inside the range rather than
    chasing the exact target, and a correction that reverses direction is
    halved so a loud bar and a quiet bar don't ping-pong the gain.

    apply=false measures once and suggests. Channels below -60 dBFS are
    "no signal" and left alone. A preamp feeds every mix including
    monitors and in-ears: use at soundcheck, never during a service."""
    if apply and _running_job():
        return _err("A background job is running; gain changes wait for it.")
    try:
        parsed = [s for s in wn.parse_strips(channels) if s[0] == "ch"]
        d = _desk()
    except RuntimeError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err("gain check: %s" % exc)
    if not parsed:
        return _err("No channels to check.")

    def num(v):
        try:
            return float(str(v).replace("+", ""))
        except (TypeError, ValueError):
            return None

    # Resolve each channel's gain control once.
    state = {}
    for _, n in parsed:
        cp = (d.describe("/ch/%d/in/conn" % n, with_values=True) or {}).get("params", {})
        grp, inp = cp.get("grp", {}).get("value"), cp.get("in", {}).get("value")
        src = "/io/in/%s/%s" % (grp, inp)
        sp = (d.describe(src, with_values=True) or {}).get("params", {})
        st = {"channel": n, "source": "%s.%s" % (grp, inp),
              "name": sp.get("name", {}).get("value"), "history": [],
              "done": False, "last_step": 0.0}
        setp = (d.describe("/ch/%d/in/set" % n, with_values=True) or {}).get("params", {})
        st["trim"] = num(setp["trim"]["value"]) if "trim" in setp else None
        if "g" in sp:
            st.update(control="preamp + trim" if st["trim"] is not None else "preamp",
                      node=src, key="g", lo=-3.0, hi=45.5, q=0.5,
                      gain=num(sp["g"]["value"]))
        else:
            if "trim" in setp:
                st.update(control="channel trim", node="/ch/%d/in/set" % n, key="trim",
                          lo=-18.0, hi=18.0, q=0.1, gain=num(setp["trim"]["value"]))
            else:
                st.update(control=None, done=True, result="no gain control found")
        st["start_gain_db"] = st.get("gain")
        st["start_trim_db"] = st.get("trim") if st.get("key") == "g" else None
        state[n] = st

    lo_ok, hi_ok = target_peak_db - tolerance_db, target_peak_db + tolerance_db
    passes, diag = 0, {}
    for passes in range(1, (max_passes if apply else 1) + 1):
        todo = [("ch", n) for n, st in state.items() if not st["done"]]
        if not todo:
            passes -= 1
            break
        try:
            meters, _, diag = _meter_collect(todo, seconds)
        except Exception as exc:
            return _err("gain check pass %d: %s" % (passes, exc))
        if not meters:
            return {"ok": False, "passes": passes, **diag}
        for _, n in todo:
            st = state[n]
            peak = (meters.get("ch:%d" % n) or {}).get("input_peak_db")
            st["input_peak_db"] = peak
            if peak is None or peak < -60:
                st.update(done=True, result="no signal - play it and re-run")
                continue
            if lo_ok <= peak <= hi_ok:
                st.update(done=True, result="in range")
                continue
            if st["gain"] is None:
                st.update(done=True, result="gain unreadable")
                continue
            delta = target_peak_db - peak
            if st["last_step"] and (delta > 0) != (st["last_step"] > 0):
                delta /= 2.0          # reversal: programme swing, not real error
            step = max(-max_step_db, min(max_step_db, delta))
            moves = _plan_gain(st, step, fine_db, trim_limit_db)
            if not moves:
                st.update(done=True, result="at the %s limit (%s dB)" % (st["control"], st["gain"]))
                continue
            if not apply:
                st.update(done=True, result="suggest " + ", ".join(
                    "%s %s -> %s dB" % (lbl, a, b) for lbl, _n, _k, a, b in moves))
                continue
            try:
                applied = 0.0
                entry = {"pass": passes, "peak_db": peak}
                for lbl, node, key, a, b in moves:
                    d.write_node(node, {key: b}, verify=True)
                    entry["%s_db" % lbl] = "%s -> %s" % (a, b)
                    applied += b - a
                    if key == "g":
                        st["gain"] = b
                    else:
                        st["trim"] = b
                        if st["key"] == "trim":
                            st["gain"] = b
                applied = round(applied, 1)
                if compensate_fader and applied:
                    fp = (d.describe("/ch/%d" % n, with_values=True) or {}).get("params", {})
                    f0 = num(fp.get("fdr", {}).get("value"))
                    if f0 is not None and f0 != float("-inf"):
                        f1 = round(max(-144.0, min(10.0, f0 - applied)), 1)
                        d.write_node("/ch/%d" % n, {"fdr": f1}, verify=True)
                        entry["fader_db"] = "%s -> %s" % (f0, f1)
                st["history"].append(entry)
                st["last_step"] = applied
            except wt.WriteRejected as exc:
                st.update(done=True, result="write failed: %s" % exc)
        if not apply:
            break

    report = []
    for n, st in state.items():
        row = {k: st.get(k) for k in ("channel", "name", "source", "control",
                                       "input_peak_db", "start_gain_db")}
        row["gain_db"] = st.get("gain")
        if st.get("key") == "g" and st.get("trim") is not None:
            row["trim_db"] = st.get("trim")
            row["start_trim_db"] = st.get("start_trim_db")
        row["result"] = st.get("result") or ("not converged after %d passes" % passes)
        if st["history"]:
            row["steps"] = st["history"]
        report.append(row)
    converged = all(st["done"] and st.get("result") in ("in range", "no signal - play it and re-run",
                                                         "no gain control found")
                    for st in state.values())
    return {"ok": True, "applied": apply, "target_peak_db": target_peak_db,
            "range_db": [lo_ok, hi_ok], "passes": passes, "converged": converged,
            "channels": report, "packets_last_pass": diag.get("packets")}


def _plan_gain(st, step, fine_db, trim_limit_db):
    """Split one correction between preamp (coarse, 0.5 dB) and channel trim
    (fine, 0.1 dB). Returns [(label, node, key, old, new), ...]."""
    def q(x, unit):
        return round(round(x / unit) * unit, 1)
    n = st["channel"]
    trim_node = "/ch/%d/in/set" % n
    if st["key"] == "trim":               # no preamp: trim does everything
        new = q(max(st["lo"], min(st["hi"], st["gain"] + step)), 0.1)
        return [("trim", trim_node, "trim", st["gain"], new)] if new != st["gain"] else []
    t = st.get("trim")
    moves = []
    if t is not None and abs(step) < fine_db and abs(t + step) <= trim_limit_db:
        new_t = q(t + step, 0.1)
        if new_t != t:
            moves.append(("trim", trim_node, "trim", t, new_t))
        return moves
    g = st["gain"]
    new_g = q(max(-3.0, min(45.5, g + step)), 0.5)
    if new_g != g:
        moves.append(("preamp", st["node"], "g", g, new_g))
    rest = step - (new_g - g)
    if t is not None and abs(rest) >= 0.1 and abs(t + rest) <= trim_limit_db:
        new_t = q(t + rest, 0.1)
        if new_t != t:
            moves.append(("trim", trim_node, "trim", t, new_t))
    return moves


@_tool(False, 'Write many')
def wing_set_many(writes: list[dict], dry_run: bool = False) -> dict:
    """Several wing_set writes in ONE call: writes is a list of
    {"node": "/ch/1/eq", "values": {"2g": -5}}. Every write is validated
    first; if any fails validation nothing is sent. Then each is written
    and read back in order; a write that fails stops the batch and the
    reply says which landed. Use for a set of EQ edits or a scene of small
    changes, so a batch needs one approval instead of one per node."""
    if not writes:
        return _err("Provide a list of {node, values}.")
    try:
        p = _profile()
    except Exception as exc:
        return _err("profile not loaded: %s" % exc)
    desk = _state["desk"]
    problems = []
    for i, w in enumerate(writes):
        node, vals = w.get("node"), {k: _osc_value(v) for k, v in (w.get("values") or {}).items()}
        if not node or not vals:
            problems.append("#%d needs node and values" % i)
            continue
        if "mdl" in vals and len(vals) > 1:
            problems.append("#%d %s changes mdl with other keys; use wing_set for staged model changes" % (i, node))
            continue
        problems += ["#%d %s" % (i, x) for x in p.check_write(node, vals, desk=desk)]
    if problems:
        return {"ok": False, "status": "rejected", "problems": problems, "note": "Nothing was sent."}
    if dry_run:
        return {"ok": True, "status": "dry_run", "validated": len(writes)}
    try:
        d = _desk()
    except RuntimeError as exc:
        return _err(str(exc))
    landed = []
    for i, w in enumerate(writes):
        vals = {k: _osc_value(v) for k, v in w["values"].items()}
        try:
            ack = d.write_node(w["node"], vals, verify=True)
            landed.append({"node": w["node"], "landed": ack.get("landed")})
        except wt.WriteRejected as exc:
            return {"ok": False, "landed": landed, "failed": {"node": w["node"], "error": str(exc)},
                    "not_sent": [x["node"] for x in writes[i + 1:]]}
    return {"ok": True, "written": len(landed), "landed": landed}


# ------------------------------------------------------------- fades

import re as _re  # noqa: E402
import time as _time  # noqa: E402

import wing_harvest as _wh  # noqa: E402
import wing_live as wl  # noqa: E402

_fader = wl.Fader()


def _resolve_targets(targets, param):
    """'ch:9', 'ch:1-4', 'bus:3', 'dca:2', 'main:1' or raw node addresses
    -> [(node, key)]."""
    if isinstance(targets, str):
        targets = [t.strip() for t in targets.split(";") if t.strip()]
    out = []
    for t in targets:
        if t.startswith("/"):
            is_send = "/send/" in t or _re.match(r"^/(ch|aux|bus)/\d+/main/\d+$", t)
            key = param or ("lvl" if is_send else "fdr")
            out.append((t.rstrip("/"), key))
        else:
            for kind, n in wn.parse_strips(t):
                out.append(("/%s/%d" % (kind, n), param or "fdr"))
    return out


@_tool(False, 'Fade')
def wing_fade(targets: list[str] | str, to_db: float | str | None = None,
              by_db: float | None = None, seconds: float = 3.0,
              curve: str = "linear", param: str | None = None,
              wait: bool = False) -> dict:
    """Move one or more faders (or send levels, or any numeric parameter)
    smoothly over `seconds`. Give to_db for an absolute target (a number or
    "-oo" for off) or by_db to move relative to where each one is now.

    targets: "ch:9", "ch:1-4", "bus:3", "dca:2", "main:1", a list of those,
    or node addresses such as "/ch/9/send/3" (send level) - param defaults
    to fdr for strips and lvl for sends. curve: "linear" (in dB, like a
    hand on the fader) or "s" (eased in and out). Runs in the background
    and returns at once unless wait=true; a new fade on the same fader
    replaces a running one. The final value is read back."""
    if (to_db is None) == (by_db is None):
        return _err("Give exactly one of to_db or by_db.")
    if curve not in ("linear", "s"):
        return _err("curve must be linear or s")
    try:
        d = _desk()
        pairs = _resolve_targets(targets, param)
    except RuntimeError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err("targets: %s" % exc)
    if not pairs:
        return _err("No targets.")
    plan, problems = [], []
    for node, key in pairs:
        got = d.describe(node, with_values=True)
        p = (got or {}).get("params", {}).get(key)
        if p is None:
            problems.append("%s has no parameter %s" % (node, key))
            continue
        start = wl._to_db(p.get("value"))
        if start is None:
            problems.append("%s/%s=%r is not numeric" % (node, key, p.get("value")))
            continue
        if to_db is not None:
            end = wl._to_db(to_db)
        else:
            end = (wl.Fade.FLOOR if start == float("-inf") else start) + float(by_db)
        bounds = wp._number_bounds(p.get("type", ""))
        if bounds and end != float("-inf"):
            end = max(bounds[0] if bounds[0] != float("-inf") else -144.0, min(bounds[1], end))
        if end is None:
            problems.append("bad target %r" % to_db)
            continue
        plan.append((node, key, start, round(end, 1) if end != float("-inf") else end))
    if problems:
        return {"ok": False, "problems": problems, "note": "Nothing was moved."}
    f = _fader.start(d, plan, seconds, curve)
    if wait:
        f.thread.join(min(float(seconds) + 5.0, 60.0))
    return {"ok": True, **f.snapshot()}


@_tool(True, 'Fade status')
def wing_fade_status() -> dict:
    """Fades currently running."""
    return {"ok": True, "running": _fader.active()}


@_tool(True, 'Stop fades')
def wing_fade_stop(fade_id: str | None = None) -> dict:
    """Stop a running fade where it is (or every fade, with no id)."""
    return {"ok": True, "stopped": _fader.stop(fade_id)}


# ------------------------------------------------------- change feed

_feed: dict = {"feed": None}


@_tool(False, 'Watch changes')
def wing_watch(on: bool = True) -> dict:
    """Start (or stop) listening to every parameter change on the desk -
    from the surface, other apps, or this server - via the WING's OSC event
    subscription. Read them with wing_changes.

    The WING allows only ONE event subscription at a time. Turning this on
    can take it from another app that relies on it (Bitfocus Companion's
    button feedback, for example); turn it off again when done if you use
    one."""
    try:
        d = _desk()
    except RuntimeError as exc:
        return _err(str(exc))
    feed = _feed["feed"]
    if on:
        if feed is None or feed.host != d.host or not feed.running:
            if feed:
                feed.stop()
            feed = wl.ChangeFeed(d.host)
            _feed["feed"] = feed
            if feed.note_self_write not in d.write_hooks:
                d.write_hooks.append(feed.note_self_write)
            feed.start()
        return {"ok": True, **feed.status()}
    if feed:
        feed.stop()
        if feed.note_self_write in d.write_hooks:
            d.write_hooks.remove(feed.note_self_write)
    return {"ok": True, "running": False}


@_tool(True, 'Recent changes')
def wing_changes(seconds: float = 120.0, prefix: str | None = None,
                 external_only: bool = False, include_mirrors: bool = False,
                 limit: int = 100) -> dict:
    """What changed on the desk in the last `seconds`, newest first, one row
    per parameter (first and latest value, how many times it moved, and
    whether the change came from this server ("self") or somewhere else -
    the surface, WING Edit, a phone). prefix narrows it ("/ch/11",
    "/bus"). Needs wing_watch on; it only knows changes since then."""
    feed = _feed["feed"]
    if feed is None or not feed.running:
        return _err("The change feed is off. Turn it on with wing_watch.")
    rows = feed.changes(seconds, prefix, "external" if external_only else None,
                        include_mirrors, limit)
    return {"ok": True, **feed.status(), "changes": rows}


# ------------------------------------------------------------ presets

def _preset_dir():
    p = os.path.join(wp.USER_DIR, "presets")
    os.makedirs(p, exist_ok=True)
    return p


def _preset_path(name):
    safe = _re.sub(r"[^A-Za-z0-9 _.-]+", "-", name).strip()
    if not safe:
        raise ValueError("empty preset name")
    return os.path.join(_preset_dir(), safe + ".wing")


@_tool(False, 'Save preset')
def wing_preset_save(name: str, roots: list[str] | str, description: str = "",
                     overwrite: bool = False) -> dict:
    """Save part of the desk as a named preset: roots is one or more node
    addresses, each saved with everything under it ("/ch/11" is the whole
    Pastor strip; "/ch/11/eq" just its EQ; "/bus/3", "/fx/2"). Saved to the
    user folder's presets/, never to the console's library. Surface state
    ($-prefixed: solo, overrides, preamp mirrors) is left out, so a recall
    sets the mix, not the solo buttons. Recall with wing_preset_load."""
    if isinstance(roots, str):
        roots = [roots]
    try:
        d = _desk()
        path = _preset_path(name)
    except (RuntimeError, ValueError) as exc:
        return _err(str(exc))
    if os.path.exists(path) and not overwrite:
        return _err("A preset named %r exists. Pass overwrite=true to replace it." % name)
    lines, lost, total = [], [], 0
    for root in roots:
        walked, stats = _wh.walk_tree(d, root.rstrip("/"), 1500)
        total += stats["nodes"]
        lost += stats["overflow"] + stats["no_reply"] + stats["no_values"]
        for node, got, known in walked:
            if not known:
                continue
            kv = {k: v["value"] for k, v in got["params"].items()
                  if not v["readonly"] and v["value"] is not None and not k.startswith("$")}
            if kv:
                lines.append("%s\t%s" % (node, wt.encode_kv_file(kv)))
    ident = _state["ident"] or {}
    hdr = ["# preset: %s" % name, "# %s" % description if description else "#",
           "# roots: %s" % ", ".join(roots),
           "# from %s (%s) fw %s, %s" % (ident.get("name"), ident.get("model"),
                                       ident.get("firmware"), _time.strftime("%Y-%m-%d %H:%M")), ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(hdr + lines) + "\n")
    out = {"ok": True, "name": name, "path": path, "nodes": total, "lines": len(lines)}
    if lost:
        out["not_captured"] = lost
    return out


@_tool(True, 'List presets')
def wing_preset_list() -> dict:
    """Saved presets: name, description, what they cover and when."""
    rows = []
    for fn in sorted(os.listdir(_preset_dir())):
        if not fn.endswith(".wing"):
            continue
        p = os.path.join(_preset_dir(), fn)
        meta = {"name": fn[:-5], "path": p}
        with open(p, encoding="utf-8") as f:
            head = [next(f, "") for _ in range(4)]
        meta["description"] = head[1][2:].strip() if len(head) > 1 else ""
        meta["roots"] = head[2].split(":", 1)[-1].strip() if len(head) > 2 else ""
        meta["saved"] = head[3].strip("# \n") if len(head) > 3 else ""
        rows.append(meta)
    return {"ok": True, "presets": rows}


@_tool(False, 'Load preset')
def wing_preset_load(name: str, retarget: dict | None = None,
                     dry_run: bool = False) -> dict:
    """Recall a saved preset onto the desk, as a background push (poll
    wing_job_status) with the usual validation and read-back.

    retarget applies it somewhere else: {"/ch/11": "/ch/12"} puts the
    Pastor strip's settings on channel 12 - which also makes this the way
    to copy a strip, or to move strips (save each, load each retargeted).
    Prefixes match whole path segments. dry_run checks everything and sends
    nothing."""
    try:
        src = _preset_path(name)
    except ValueError as exc:
        return _err(str(exc))
    if not os.path.exists(src):
        return _err("No preset %r. See wing_preset_list." % name)
    path = src
    if retarget:
        maps = sorted(retarget.items(), key=lambda kv: -len(kv[0]))
        out = []
        for line in open(src, encoding="utf-8").read().splitlines():
            if line.startswith("/"):
                node, sep, rest = line.partition("\t")
                for a, b in maps:
                    a, b = a.rstrip("/"), b.rstrip("/")
                    if node == a or node.startswith(a + "/"):
                        node = b + node[len(a):]
                        break
                line = node + sep + rest
            out.append(line)
        tmpdir = os.path.join(_preset_dir(), ".applied")
        os.makedirs(tmpdir, exist_ok=True)
        path = os.path.join(tmpdir, "%s_%s.wing" % (os.path.basename(src)[:-5],
                                                     _time.strftime("%Y%m%d-%H%M%S")))
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
    res = wing_push_file(path, dry_run=dry_run)
    res["preset"] = name
    if retarget:
        res["retargeted_file"] = path
    return res


# --------------------------------------------------- auto-gate / comp

def _num(v):
    try:
        return float(str(v).replace("+", "").split(":")[0])
    except (TypeError, ValueError):
        return None


def _clamp_to(spec_type, x, unit=0.5):
    b = wp._number_bounds(spec_type or "")
    if b:
        x = max(b[0], min(b[1], x))
    return round(round(x / unit) * unit, 1)


@_tool(False, 'Auto gate')
def wing_auto_gate(channels: str, phase: str = "noise", margin_db: float = 8.0,
                   seconds: float = 6.0, apply: bool = False,
                   enable: bool = False) -> dict:
    """Set gate thresholds from what the meters hear, in two steps:

    1. phase="noise" while the sources are SILENT (mics open, nobody
       playing): measures each channel's noise and spill floor.
    2. phase="signal" while they are PLAYED: sets the threshold to the
       floor + margin_db, provided that still sits well under the playing
       level; otherwise reports too little separation and leaves it.

    apply=false (default) suggests only. enable=true also switches the gate
    on. Only gate models with a threshold (GATE, E88, 9000G, ...) are set;
    others are reported. Run at soundcheck."""
    if phase not in ("noise", "signal"):
        return _err("phase must be noise or signal")
    try:
        parsed = [s for s in wn.parse_strips(channels) if s[0] in ("ch", "aux")]
        meters, _, diag = _meter_collect(parsed, seconds)
        d = _desk()
    except RuntimeError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err("auto gate: %s" % exc)
    if not meters:
        return {"ok": False, **diag}
    floors = _state.setdefault("noise_floor", {})
    rows = []
    for kind, n in parsed:
        key = "%s:%d" % (kind, n)
        peak = (meters.get(key) or {}).get("input_peak_db")
        if phase == "noise":
            floors[key] = peak
            rows.append({"strip": key, "noise_peak_db": peak})
            continue
        row = {"strip": key, "signal_peak_db": peak, "noise_peak_db": floors.get(key)}
        if floors.get(key) is None:
            row["result"] = "no noise reading - run phase=noise first"
            rows.append(row)
            continue
        gp = (d.describe("/%s/%d/gate" % (kind, n), with_values=True) or {}).get("params", {})
        row["model"] = gp.get("mdl", {}).get("value")
        if "thr" not in gp:
            row["result"] = "model %s has no threshold" % row["model"]
            rows.append(row)
            continue
        thr = _clamp_to(gp["thr"].get("type"), floors[key] + margin_db)
        row["threshold_db"] = "%s -> %s" % (gp["thr"]["value"], thr)
        if peak is None or peak < thr + 6:
            row["result"] = "too little separation (signal %s vs threshold %s); left alone" % (peak, thr)
            rows.append(row)
            continue
        if apply:
            kv = {"thr": thr}
            if enable:
                kv["on"] = 1
            try:
                d.write_node("/%s/%d/gate" % (kind, n), kv, verify=True)
                row["result"] = "set" + (" and switched on" if enable else "")
            except wt.WriteRejected as exc:
                row["result"] = "write failed: %s" % exc
        else:
            row["result"] = "suggested"
        rows.append(row)
    return {"ok": True, "phase": phase, "applied": apply and phase == "signal",
            "channels": rows,
            "next": "Now play the sources and run phase=signal." if phase == "noise" else None}


@_tool(False, 'Auto compress')
def wing_auto_compress(channels: str, target_gr_db: float = 4.0,
                       seconds: float = 8.0, apply: bool = False,
                       enable: bool = False) -> dict:
    """Set compressor thresholds so the loud parts get about target_gr_db
    of gain reduction, from the compressor's own detector level measured
    while the source is played (the dyn key meter; the input meter if the
    key reads nothing). Uses the model's current ratio:
    threshold = peak - target * ratio / (ratio - 1).

    Only models with a threshold and ratio (COMP, EXP, B160, 9000C, ...)
    are set; character models without them (LA, 76LA) are reported. With
    apply=true the result is measured once more and the gain reduction the
    desk reports is returned (that meter's scale is not yet verified on
    hardware - judge by ear). enable=true also switches the compressor on."""
    try:
        parsed = [s for s in wn.parse_strips(channels) if s[0] in ("ch", "aux", "bus", "main", "mtx")]
        meters, _, diag = _meter_collect(parsed, seconds)
        d = _desk()
    except RuntimeError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err("auto compress: %s" % exc)
    if not meters:
        return {"ok": False, **diag}
    rows, written = [], []
    for kind, n in parsed:
        key = "%s:%d" % (kind, n)
        m = meters.get(key) or {}
        level = m.get("dyn_key", {}).get("peak") if "dyn_key" in m else None
        if level is None or level <= -100:
            level = m.get("input_peak_db")
        row = {"strip": key, "detector_peak_db": level}
        node = "/%s/%d/dyn" % (kind, n)
        dp = (d.describe(node, with_values=True) or {}).get("params", {})
        row["model"] = dp.get("mdl", {}).get("value")
        ratio = _num(dp.get("ratio", {}).get("value"))
        if "thr" not in dp or not ratio or ratio <= 1:
            row["result"] = "model %s has no threshold/ratio to set" % row["model"]
            rows.append(row)
            continue
        if level is None or level < -60:
            row["result"] = "no signal - play it and re-run"
            rows.append(row)
            continue
        want = level - target_gr_db * ratio / (ratio - 1)
        thr = _clamp_to(dp["thr"].get("type"), want)
        row["ratio"] = ratio
        if want < thr - 1:
            row["warning"] = ("at ratio %s:1 this target needs a threshold of %.1f dB, below the "
                              "control's range; raise the ratio for that much reduction" % (ratio, want))
        row["threshold_db"] = "%s -> %s" % (dp["thr"]["value"], thr)
        if apply:
            kv = {"thr": thr}
            if enable:
                kv["on"] = 1
            try:
                d.write_node(node, kv, verify=True)
                row["result"] = "set" + (" and switched on" if enable else "")
                written.append((kind, n))
            except wt.WriteRejected as exc:
                row["result"] = "write failed: %s" % exc
        else:
            row["result"] = "suggested"
        rows.append(row)
    if written:
        try:
            after, _, _ = _meter_collect(written, seconds)
            for row in rows:
                a = after.get(row["strip"]) or {}
                if "dyn_reduction_db" in a:
                    row["measured_gr_db"] = a["dyn_reduction_db"]["max"]
        except Exception:
            pass
    return {"ok": True, "applied": apply, "target_gr_db": target_gr_db, "channels": rows}


# ------------------------------------------------------------- scenes

_SCENE_ACTIONS = ("GO", "GOTAG", "NEXT", "PREV", "GONEXT", "GOPREV")


def _scene_enabled():
    if os.environ.get("WING_SCENE_CONTROL", "").lower() in ("1", "true", "yes", "on"):
        return True
    try:
        with open(os.path.join(wp.USER_DIR, "config.json"), encoding="utf-8") as f:
            return bool(json.load(f).get("scene_control"))
    except (OSError, ValueError):
        return False


def _lib_state(d):
    out = {}
    for k in ("$actshow", "$active", "$actidx", "$activeid", "$scenes"):
        v = d.get_leaf("/$ctl/lib/%s" % k)
        if v is not None:
            out[k.lstrip("$")] = v
    return out


@_tool(True, 'Scene status')
def wing_scene_status() -> dict:
    """The open show, the active scene and the show's scene list (as the
    desk reports them; the active name is known to lag a recall made on the
    surface). Read-only. Whether scene control is enabled is included."""
    try:
        d = _desk()
    except RuntimeError as exc:
        return _err(str(exc))
    st = _lib_state(d)
    if st.get("scenes"):
        st["scenes"] = [x.strip() for x in str(st["scenes"]).split(",") if x.strip()]
    return {"ok": True, "scene_control_enabled": _scene_enabled(), **st}


@_tool(False, 'Recall scene')
def wing_scene(action: str, index: int | None = None) -> dict:
    """Show control: recall scenes from the show open on the desk.

    action: GO (load scene number `index` in the show's list), GOTAG (load
    the scene tagged `index`), NEXT / PREV (move the selection), GONEXT /
    GOPREV (move and load). A recall reconfigures the whole desk at once,
    so this is OFF unless enabled: create ~/.wing-mcp/config.json with
    {"scene_control": true} (or set WING_SCENE_CONTROL=1) and restart.
    A show must be open on the desk."""
    if not _scene_enabled():
        return _err("Scene control is off. To enable it, create %s containing "
                    '{"scene_control": true} and restart the extension.'
                    % os.path.join(wp.USER_DIR, "config.json"))
    action = (action or "").upper()
    if action not in _SCENE_ACTIONS:
        return _err("action must be one of %s" % ", ".join(_SCENE_ACTIONS))
    if action in ("GO", "GOTAG") and index is None:
        return _err("%s needs an index." % action)
    if _running_job():
        return _err("A background job is running; a recall waits for it.")
    try:
        d = _desk()
        before = _lib_state(d)
        if not before.get("actshow"):
            return _err("No show is open on the desk; open one on the surface first.")
        idx = 0 if action in ("NEXT", "PREV", "GONEXT", "GOPREV") else int(index)
        d.write_node("/$ctl/lib", {"$actionidx": idx})
        d.write_node("/$ctl/lib", {"$action": action})
        _time.sleep(1.0)
        after = _lib_state(d)
    except RuntimeError as exc:
        return _err(str(exc))
    except wt.WriteRejected as exc:
        return _err("The desk refused the recall: %s" % exc)
    return {"ok": True, "action": action, "index": index,
            "before": {k: before.get(k) for k in ("active", "actidx")},
            "after": {k: after.get(k) for k in ("active", "actidx")},
            "note": "The desk's active-scene name can lag; check the surface."}


if __name__ == "__main__":
    mcp.run()
