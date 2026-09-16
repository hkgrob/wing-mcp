#!/usr/bin/env python3
"""Shared staged-write logic for pushing a show file to the desk.

Both front doors import this: `wing_push.py`, the command-line pusher Doug
runs by hand, and `server.py`'s `wing_push_file` / `wing_push_status` tools,
which let Claude trigger a push and poll it without blocking an MCP call for
the full runtime of a scene (see WING-Remote-Push_Design_v1.md). One
implementation, two front doors - writing it twice lets them drift, and the
drift surfaces as a push that works from the command line and not from
Claude, which is expensive to diagnose mid-push.

Three things do NOT extract as cleanly as "move the code" implies, and each
is built rather than moved:

  - Progress. The CLI prints as it goes; a background worker needs state a
    status call can read instead (see PushJob).
  - The graceful stop. `except KeyboardInterrupt` only exists when a human is
    at a terminal; the retry state machine below is the background worker's
    equivalent, and it is not a cancel - see PushJob's early-stop rules.
  - The report. It used to be built once at the end from an in-memory list,
    which left nothing on disk if the process died any other way (including
    a version-bump reload). IncrementalReport opens the file at the start and
    flushes each node's line as it lands, so a killed worker still names
    every node that landed.

The staged-write logic itself - model first, re-read the shape it actually
landed on, then the values, with the step-size read-back comparison - is
unchanged from the original wing_push.py and is not to be touched here. The
one addition to process_node() is the completeness check described in the
design doc's "Idempotence is a template property, not a system one"
section, which is new behaviour, not a change to the staged-write sequence.
As of v0.3.1 that check no longer withholds a node just because the show
file omits a key: it writes what the file gives and reports the rest as
partial, and withholds only the exact 2026-08-31 shape (a model-bearing
node whose file line names the model alone, or the model plus on, and
nothing else) - see _completeness_missing and _is_model_only below.
"""

import math
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wing_profile as wp    # noqa: E402
import wing_transport as wt  # noqa: E402

NODE_PACE_S = 0.02  # explicit outbound cap, on top of the ack/read pacing
                     # write_node and describe() already impose. The desk
                     # carries ten or more simultaneous clients in normal
                     # use (project CLAUDE.md), so this is a deliberate
                     # floor under the gap between messages, not a
                     # correction for anything observed to be too fast.


# ------------------------------------------------------------- value compare

_NUM_K = re.compile(r"^(-?)(\d+)k(\d+)$")


def _to_number(s):
    """Parse a value the desk might hand back: a plain number, its own k
    notation (14k78 = 14780), or -oo. Returns None if it isn't numeric."""
    s = s.strip()
    if s in ("-oo", "-inf"):
        return float("-inf")
    m = _NUM_K.match(s)
    if m:
        sign, whole, frac = m.groups()
        val = float(whole) * 1000.0 + float(frac) * (1000.0 / (10 ** len(frac)))
        return -val if sign else val
    try:
        return float(s)
    except ValueError:
        return None


_STEP_RANGE_RE = re.compile(
    r"^(lin|log|fader)\s*\[\s*(.+?)\s*\.\.\s*(.+?)\s*\]\s*,\s*(\d+)\s*steps"
)
_BOUND_RE = re.compile(r"^(-?\d+(?:\.\d+)?|-?\d+k\d*)\s*(?:dB|Hz|ms|%|m)?$")


def _parse_bound(tok):
    """Parse one end of a declared range: the desk's own k-notation
    (14k78), a unit suffix (dB/Hz/ms/%/m), a leading '+', or -oo/+oo.
    Returns None if it isn't a bound this can reason about numerically."""
    tok = tok.strip()
    if tok in ("-oo", "-inf"):
        return float("-inf")
    if tok in ("+oo", "oo", "inf"):
        return float("inf")
    if tok.startswith("+"):
        tok = tok[1:]
    m = _BOUND_RE.match(tok)
    if not m:
        return None
    num = m.group(1)
    if "k" in num:
        sign = -1.0 if num.startswith("-") else 1.0
        a, b = num.lstrip("-").split("k", 1)
        a = a or "0"
        val = float("%s.%s" % (a, b)) * 1000.0 if b else float(a) * 1000.0
        return sign * val
    return float(num)


def parse_stepped_range(raw_type):
    """Parse a declared 'lin [lo .. hi UNIT], N steps' / 'log [...]' /
    'fader [...]' type string into (kind, lo, hi, n). Returns None when the
    string doesn't match that shape, or either bound isn't a finite number
    (e.g. a fader's -oo floor) -- callers fall back to a looser compare."""
    if not raw_type:
        return None
    m = _STEP_RANGE_RE.match(raw_type.strip())
    if not m:
        return None
    kind, lo_s, hi_s, n_s = m.groups()
    lo, hi, n = _parse_bound(lo_s), _parse_bound(hi_s), int(n_s)
    if lo is None or hi is None or n < 2:
        return None
    if lo in (float("inf"), float("-inf")) or hi in (float("inf"), float("-inf")):
        return None
    return kind, lo, hi, n


def _step_value(kind, lo, hi, n, idx):
    idx = min(max(idx, 0), n - 1)
    if kind == "log":
        return lo * (hi / lo) ** (idx / (n - 1))
    return lo + idx * (hi - lo) / (n - 1)


def nearest_step_match(raw_type, sent, got):
    """True/False if the parameter's own declared step scale settles
    whether `got` is a legitimate landing for `sent`; None if `raw_type`
    doesn't carry a usable finite lin/log/fader range (caller falls back
    to a looser numeric compare in that case).

    The desk quantises a written value to the nearest legal step on that
    parameter's own scale -- evenly spaced for 'lin'/'fader', geometric for
    'log' -- and hands back a display-rounded version of that step. E.g. a
    121-step 'log [4 .. 4000 ms]' asked for rel=35 lands on the continuous
    step 35.650..., which the desk displays as 36 (state.md/CLAUDE.md's
    /ch/21/dyn rel case, and the source of the false FAILED this replaces).
    So the check is: is `got` within half a local step of the nearest legal
    step to what was actually sent? That works on both scales without ever
    having to guess the desk's display precision, because the tolerance is
    derived from the scale's own local spacing at that point, not a fixed
    percentage. Verified against three real desk quantisations recorded in
    this project: rel 35->36 (log, 121 steps), dynsc q 1.50->1.51 (log, 181
    steps), and a 961-step log frequency scale's 60.0->60.1 -- the formula
    below reproduces all three exactly."""
    parsed = parse_stepped_range(raw_type)
    if parsed is None:
        return None
    kind, lo, hi, n = parsed
    sent_n, got_n = _to_number(sent), _to_number(got)
    if sent_n is None or got_n is None:
        return None
    if sent_n == float("-inf") or got_n == float("-inf"):
        return sent_n == got_n
    if kind == "log" and (lo <= 0 or hi <= 0):
        return None  # a log scale can't include zero/negative bounds
    sent_n = min(max(sent_n, min(lo, hi)), max(lo, hi))  # the desk clamps out-of-range asks
    if kind == "log":
        raw_idx = (n - 1) * math.log(sent_n / lo) / math.log(hi / lo)
    else:
        raw_idx = (sent_n - lo) / (hi - lo) * (n - 1) if hi != lo else 0.0
    idx = int(round(raw_idx))
    idx = min(max(idx, 0), n - 1)
    nearest = _step_value(kind, lo, hi, n, idx)
    lower = _step_value(kind, lo, hi, n, idx - 1)
    upper = _step_value(kind, lo, hi, n, idx + 1)
    span = max(nearest - lower, upper - nearest, 0.0)
    tol = max(span / 2.0 * 1.1, 1e-6)  # small pad for display rounding
    if abs(got_n - nearest) <= tol:
        return True

    # Second test, and the one that matters in practice: did the value land
    # within a single step of what was ASKED for? The test above reconstructs
    # the desk's step table from the declared type string and then requires
    # `got` to sit on the step it computes -- which fails whenever the desk's
    # real scale differs slightly from its own declaration. Live case,
    # 2026-08-31: /ch/14/gatesc q on 'log [0.44 .. 10.00], 181 steps', asked
    # 1.75 and got 1.74, where the reconstructed neighbouring steps are 1.7331
    # and 1.7635 and 1.74 is neither. The write was correct; the model of the
    # scale was slightly off. Asking instead whether got is within one local
    # step of sent needs no faith in the reconstruction, only in the step
    # SPACING, which is far more forgiving of a small error in the endpoints.
    # It cannot mask a real failure: a genuine mismatch is many steps away
    # (rel 35 against 999 is ~460 steps), where every quantisation on record
    # here is under one (35->36, 1.50->1.51, 60.0->60.1, 1.75->1.74).
    if span > 0 and abs(got_n - sent_n) <= span * 1.1:
        return True
    return False


def values_match(sent, got, raw_type=None, abs_tol=0.15, rel_tol=0.02):
    """The desk quantises to its nearest legal step and hands that back, so
    a read-back check compares against the nearest step, not for exact
    equality: asked dyn f=60.0, got 60.1. Exact string match first; then,
    when the live parameter's own declared type string is available, a
    comparison against that parameter's real step scale (nearest_step_match)
    -- not a fixed tolerance, because a coarse scale (121 steps over
    4..4000 ms) and a fine one (1024 steps over a fader) do not share one
    percentage that is right for both. Only when no usable stepped range
    can be parsed from raw_type (a list, an int, a fader with an -oo floor,
    or no type supplied) does this fall back to the old tolerant numeric
    compare -- kept as a last resort, not widened, since it still has to
    catch a real mismatch on whatever this can't derive a scale for."""
    sent, got = str(sent), str(got)
    if sent == got:
        return True
    if raw_type:
        stepped = nearest_step_match(raw_type, sent, got)
        if stepped is not None:
            return stepped
    sn, gn = _to_number(sent), _to_number(got)
    if sn is None or gn is None:
        return False
    if sn == float("-inf") or gn == float("-inf"):
        return sn == gn
    return abs(sn - gn) <= max(abs_tol, rel_tol * max(abs(sn), abs(gn), 1.0))


# --------------------------------------------------------------- node family

def firmware_matches(live_fw, profile_fw):
    """Compares the console's two firmware strings on their actual version,
    not byte-for-byte. /$syscfg's $firmware (what `live` reads) omits the
    trailing build-channel tag that the discovery broadcast carries -- the
    2026-08-31 push logged live=3.1-0-g9f314617 against
    profile=3.1-0-g9f314617:release and flagged *** MISMATCH *** for
    identical firmware, because the profile's firmware string was captured
    from that broadcast (see wing_transport.parse_ident) and never had the
    ':release' suffix stripped for comparison. A real firmware difference
    still shows up here -- this only ignores the part after a colon, it
    doesn't ignore the version itself."""
    def base(s):
        return (s or "").split(":", 1)[0]
    return bool(live_fw) and base(live_fw) == base(profile_fw)


def normalize_family(node):
    """/ch/7/gate -> /ch/N/gate. Used only to guess, in --dry-run, whether
    another node of the same shape is already sitting on the model a
    model-changing node is about to switch to."""
    parts = node.split("/")
    return "/".join("N" if p.isdigit() else p for p in parts)


def predict_from_donor(donor_params, remaining_kv):
    """Best-effort dry-run prediction: split remaining_kv into what a donor
    node's live parameter set (same family, already on the target model)
    would accept versus reject. Unverified - the target node has not
    actually loaded the model, this is a sibling's current shape standing
    in for it."""
    accepted, rejected = {}, {}
    for k, v in remaining_kv.items():
        p = donor_params.get(k)
        if p is None:
            rejected[k] = "predicted shape (live sibling on %s) has no parameter %s" % (
                donor_params.get("mdl", {}).get("value", "?"), k)
            continue
        if p.get("readonly"):
            rejected[k] = "predicted shape marks %s read-only" % k
            continue
        why = wp.validate(wp.parse_type(p.get("type")), v)
        if why:
            rejected[k] = "predicted shape rejects %s=%r: %s" % (k, v, why)
            continue
        accepted[k] = v
    return accepted, rejected


def _completeness_missing(profile, node, live_params, original_kv):
    """Which writable, non-excluded, non-surface parameters the live model
    exposes that this show-file line never mentioned. Returns a sorted list
    of missing parameter names, empty when the node is fully specified.

    Since v0.3.1 (Doug, 2026-09-01) this is informational, not a withhold
    gate: a name in the returned list becomes the node's `partial` list in
    process_node, written and reported, not a reason to withhold the node.
    Two kinds of key are never demanded of the file at all, so they never
    appear here:

      - anything whose name starts with '$' - a surface mirror or
        transient, never a value a show file is expected to carry.
      - anything `profile.excluded_reason` marks as policy-excluded (scene
        recall, card format, etc.) - the same policy process_node already
        applies to the file's own keys, so a parameter that could never
        legally be written is never demanded of the file either.

    The real defect this exists for - nine dynamics nodes shipping a model
    name and nothing else on 2026-08-31, so every parameter under that
    model silently kept the previous scene's values - is caught separately
    by _is_model_only, which is the only thing left that withholds a node."""
    required = set()
    for name, p in live_params.items():
        if name.startswith("$"):
            continue
        if p.get("readonly"):
            continue
        if wp.excluded_reason(node, name):
            continue
        required.add(name)
    present = set(original_kv.keys())
    return sorted(required - present)


def _is_model_only(live_params, kv):
    """True only for the exact 2026-08-31 shape: `live_params` names this
    as a model-bearing node (it carries an 'mdl' key - not every node
    family does) AND `kv` - the show file's keys for this node, after
    policy exclusion, before mdl is popped off for a model change - is
    {'mdl'} or {'mdl', 'on'} and nothing else. That is the one file shape a
    retry cannot trust: it names a model (and maybe just turns the node on)
    and specifies not one parameter under that model, so anything under it
    silently keeps whatever the previous scene left there. Every other
    partially-specified node is written as far as the file goes and
    reported partial by _completeness_missing - this is the sole remaining
    withhold condition in v0.3.1."""
    if "mdl" not in live_params:
        return False
    keys = set(kv.keys())
    return keys == {"mdl"} or keys == {"mdl", "on"}


def _model_only_detail(mdl_value):
    """Detail text for the one status="incomplete" case v0.3.1 keeps -
    see _is_model_only."""
    return (
        "model-only node: the file gives mdl%s and no parameters, so the "
        "previous scene's values under it would survive (2026-08-31 "
        "defect); withheld" %
        ("=%s" % mdl_value if mdl_value is not None else ""))


# ------------------------------------------------------------- node staging

def process_node(desk, profile, node, kv, dry_run, model_examples):
    """Stage and (unless dry_run) execute the write for one node.

    Returns a dict: node, status (written/skipped/failed/incomplete), detail,
    written (dict of key -> landed value), skipped (dict of key -> reason),
    partial (sorted list of live parameter names the file never mentioned -
    present only when non-empty), model_change (None or {"from":..,"to":..}),
    warnings (list of str).

    "written" under dry_run means "would be written, validated against a
    live read" - nothing was sent. Since v0.3.1, a live parameter the file
    never mentioned no longer withholds the node: the keys the file DID
    give are written (or, under dry_run, validated) exactly as they would
    be if the file were complete, and the omitted names come back in
    `partial` with a matching warning - see _completeness_missing. The node
    is withheld with status "incomplete" only for the one shape
    _is_model_only recognises: a model-bearing node whose file line names
    only the model (optionally plus on) and nothing else, which is exactly
    the 2026-08-31 defect (nine dynamics nodes shipped a model name and
    nothing else, so every parameter under it silently kept the previous
    scene's values). Everything else about the dict means the same thing in
    both modes."""
    entry = {"node": node, "written": {}, "skipped": {}, "model_change": None,
              "warnings": [], "status": None, "detail": ""}
    original_kv = dict(kv)

    # Exclusions are policy, never re-checked against the live desk, and
    # never overridden by what a show file asks for.
    for k in list(kv.keys()):
        why = wp.excluded_reason(node, k)
        if why:
            entry["skipped"][k] = "excluded by policy: %s" % why
            del kv[k]

    if not kv:
        entry["status"] = "skipped"
        entry["detail"] = "every requested key on this node is excluded by policy"
        return entry

    # Snapshot of the file's keys for this node, after policy exclusion but
    # before anything below (a model change pops "mdl" out of kv) mutates
    # it. This is the shape _is_model_only checks - see there.
    kv_after_exclusion = dict(kv)

    # Live shape, read fresh. This is the node's true current state, never
    # the stored profile - the profile only supplies the exclusion policy.
    got = desk.describe(node, with_values=True)
    if got is None:
        entry["status"] = "failed"
        entry["detail"] = "no reply from node"
        return entry
    if got.get("overflow"):
        got2 = desk.describe(node, with_values=False)
        if got2 is None or got2.get("overflow"):
            entry["status"] = "failed"
            entry["detail"] = "node overflowed on read; live shape unreadable"
            return entry
        got = got2
        entry["warnings"].append(
            "live values unreadable (node overflowed on '#'); shape read names-only")
        values_known = False
    else:
        values_known = True

    live_params = got["params"]
    family = normalize_family(node)
    if values_known:
        live_mdl = live_params.get("mdl", {}).get("value")
        if live_mdl is not None:
            model_examples[(family, live_mdl)] = live_params

    # -------------------------------------------------- model change first
    model_changing = False
    if "mdl" in kv:
        target_mdl = kv["mdl"]
        if not values_known:
            entry["skipped"]["mdl"] = ("cannot confirm the live model with values "
                                       "unreadable; mdl write withheld")
            del kv["mdl"]
        else:
            live_mdl = live_params.get("mdl", {}).get("value")
            if live_mdl != target_mdl:
                model_changing = True

    if model_changing:
        target_mdl = kv.pop("mdl")
        entry["model_change"] = {"from": live_mdl, "to": target_mdl}
        remaining = dict(kv)

        if dry_run:
            donor = model_examples.get((family, target_mdl))
            if donor is None:
                entry["status"] = "skipped"
                entry["detail"] = (
                    "would write mdl %s -> %s first; no live sibling of this node "
                    "already on %s has been seen yet in this run, so the resulting "
                    "shape cannot be predicted without actually writing it" %
                    (live_mdl, target_mdl, target_mdl))
                for k in remaining:
                    entry["skipped"][k] = ("shape unknown until mdl is applied on "
                                           "this node (dry-run, unresolved)")
                return entry
            accepted, rejected = predict_from_donor(donor, remaining)
            entry["written"]["mdl"] = target_mdl
            entry["written"].update(accepted)
            entry["skipped"].update(rejected)
            entry["warnings"].append(
                "shape PREDICTED from a live sibling node currently on %s, not "
                "confirmed on %s itself - unverified" % (target_mdl, node))

            # Completeness check (see _completeness_missing/_is_model_only
            # above), checked here against the PREDICTED donor shape, which
            # carries the same "unverified" caveat as the rest of this
            # dry-run branch. Since v0.3.1 a missing live key no longer
            # withholds the node - it is written (predicted) as far as the
            # file goes and the gap is reported as partial. Only the exact
            # 2026-08-31 model-only shape is still withheld.
            missing = _completeness_missing(profile, node, donor, original_kv)
            if missing and _is_model_only(donor, kv_after_exclusion):
                entry["status"] = "incomplete"
                entry["detail"] = _model_only_detail(target_mdl)
            else:
                entry["status"] = "written"
                entry["detail"] = "dry-run, model change predicted from a live sibling"
                if missing:
                    entry["partial"] = missing
                    entry["warnings"].append(
                        "partial: %d live key(s) not in file, left as they are "
                        "on the desk: %s" % (len(missing), ", ".join(missing)))
            _apply_identity_warning(profile, entry, node, original_kv)
            return entry

        # LIVE: write the model alone, then re-read to see what actually
        # landed. This is the one case where dry-run cannot mirror live
        # behaviour exactly - the new shape genuinely does not exist until
        # the desk has loaded the model.
        try:
            desk.write_node(node, {"mdl": target_mdl})
        except wt.WriteRejected as e:
            entry["status"] = "failed"
            entry["detail"] = "model write rejected: %s" % e
            return entry

        got2 = desk.describe(node, with_values=True)
        if got2 is None or got2.get("overflow"):
            entry["status"] = "failed"
            entry["detail"] = ("wrote mdl=%s but the node could not be read back "
                               "to confirm the new shape" % target_mdl)
            return entry
        new_mdl = got2["params"].get("mdl", {}).get("value")
        if new_mdl != target_mdl:
            entry["status"] = "failed"
            entry["detail"] = "wrote mdl=%s but node reads back mdl=%r" % (
                target_mdl, new_mdl)
            return entry
        entry["written"]["mdl"] = new_mdl
        live_params = got2["params"]
        model_examples[(family, new_mdl)] = live_params
        kv = remaining

    # --------------------------------------- filter the rest against LIVE shape
    to_write = {}
    for k, v in kv.items():
        p = live_params.get(k)
        if p is None:
            entry["skipped"][k] = ("not a parameter of this node under its "
                                   "current model (%s)" %
                                   live_params.get("mdl", {}).get("value", "n/a"))
            continue
        if p.get("readonly"):
            entry["skipped"][k] = "read-only"
            continue
        why = wp.validate(wp.parse_type(p.get("type")), v)
        if why:
            entry["skipped"][k] = "rejected by live type: %s" % why
            continue
        to_write[k] = v

    _apply_identity_warning(profile, entry, node, original_kv)

    # Completeness check. Checked against the LIVE shape - post
    # model-change above, if one happened - so it reflects the model
    # actually in force. Since v0.3.1 a live parameter the file never
    # mentioned no longer withholds the node - to_write below still goes
    # out and the gap is recorded on entry["partial"] with a matching
    # warning, so status falls through to whatever the write below actually
    # produces (written/skipped/failed). The one shape still withheld here,
    # with status "incomplete" and nothing past this point sent, is
    # _is_model_only's: a model-bearing node whose file line names only the
    # model (optionally plus on) and nothing else - the exact 2026-08-31
    # defect. Any model write above already happened either way and stays
    # recorded in entry["written"].
    missing = _completeness_missing(profile, node, live_params, original_kv)
    if missing:
        if _is_model_only(live_params, kv_after_exclusion):
            entry["status"] = "incomplete"
            entry["detail"] = _model_only_detail(
                live_params.get("mdl", {}).get("value"))
            if to_write:
                entry["detail"] += "  (%d other key(s) validated and withheld)" % len(to_write)
            return entry
        entry["partial"] = missing
        entry["warnings"].append(
            "partial: %d live key(s) not in file, left as they are on the "
            "desk: %s" % (len(missing), ", ".join(missing)))

    if not to_write:
        entry["status"] = "written" if entry["written"] else "skipped"
        if not entry["detail"]:
            entry["detail"] = "no further writable keys on this node"
        return entry

    if dry_run:
        entry["written"].update(to_write)
        entry["status"] = "written"
        if not entry["detail"]:
            entry["detail"] = "dry-run, validated against live shape"
        return entry

    # ------------------------------------------------------------ LIVE write
    try:
        desk.write_node(node, to_write)
    except wt.WriteRejected as e:
        entry["status"] = "failed"
        entry["detail"] = "write rejected: %s" % e
        # UDP has no rollback - find out what, if anything, actually landed.
        got3 = desk.describe(node, with_values=True)
        if got3 is not None and not got3.get("overflow"):
            for k, v in to_write.items():
                gv = got3["params"].get(k, {}).get("value")
                raw_type = live_params.get(k, {}).get("type")
                if values_match(v, gv, raw_type=raw_type):
                    entry["written"][k] = gv
        return entry

    # write_node only reads back to confirm when a value needed leaf
    # routing (see the module docstring). Do our own read-back-and-compare
    # unconditionally: the desk's OK means received, not stored.
    got3 = desk.describe(node, with_values=True)
    if got3 is None or got3.get("overflow"):
        entry["status"] = "failed"
        entry["detail"] = "wrote %d key(s) but could not read the node back to confirm" % len(to_write)
        return entry
    mismatches = {}
    for k, v in to_write.items():
        gv = got3["params"].get(k, {}).get("value")
        raw_type = live_params.get(k, {}).get("type")
        if values_match(v, gv, raw_type=raw_type):
            entry["written"][k] = gv
        else:
            mismatches[k] = (v, gv)
    if mismatches:
        entry["status"] = "failed"
        entry["detail"] = "read-back mismatch: " + "; ".join(
            "%s wanted %r got %r" % (k, w, g) for k, (w, g) in sorted(mismatches.items()))
    else:
        entry["status"] = "written"
        if not entry["detail"]:
            entry["detail"] = "confirmed by read-back"
    return entry


def _apply_identity_warning(profile, entry, node, original_kv):
    """A channel written with name/col/icon but no clink=0 stores the value
    and shows nothing on the surface (project CLAUDE.md). This checks what
    the FILE asked for, not what actually landed, because the bug is in
    the source show file either way."""
    warn = profile.identity_warning(node, original_kv)
    if warn:
        entry["warnings"].append(warn)


def failure_class(entry):
    """A stable bucket for one failed entry's detail message, used by the
    retry state machine to compare "the same node failed twice" - never on
    the raw message text, because a read-back mismatch can report a
    slightly different quantised value each time and never compare equal
    (design doc, "Failure and retry"). Only meaningful when
    entry["status"] == "failed"; returns "other" for anything this doesn't
    recognise rather than raising, since a new failure shape should still
    be classifiable as SOME class rather than break the state machine."""
    d = entry.get("detail") or ""
    if d == "no reply from node":
        return "no_reply"
    if d.startswith("node overflowed on read"):
        return "overflow"
    if d.startswith("model write rejected:"):
        return "model_rejected"
    if d.startswith("wrote mdl=") and "could not be read back to confirm" in d:
        return "readback_unavailable_after_model"
    if d.startswith("wrote mdl=") and "reads back mdl=" in d:
        return "model_mismatch"
    if d.startswith("write rejected:"):
        return "write_rejected"
    if d.startswith("wrote ") and "could not read the node back to confirm" in d:
        return "readback_unavailable"
    if d.startswith("read-back mismatch:"):
        return "readback_mismatch"
    return "other"


# ---------------------------------------------------------------- show file

def parse_show_file(path):
    """Yields (lineno, node, kv, problems). node is None and problems is
    non-empty on a line that could not be parsed at all."""
    with open(path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.rstrip("\n").rstrip("\r")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "\t" not in line:
                yield lineno, None, {}, ["no TAB found; not a valid node line: %r" % line]
                continue
            node, _, rest = line.partition("\t")
            if not node.startswith("/"):
                yield lineno, None, {}, ["address does not start with '/': %r" % node]
                continue
            kv, problems = wt.parse_kv_file(rest)
            yield lineno, node, kv, problems


def load_show_file(path):
    """Every node line in a show file, no --only/--limit filtering (that
    stays a CLI-only convenience in wing_push.py). Returns
    (all_nodes, parse_error_count) where all_nodes is a list of
    (lineno, node, kv, problems)."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    all_nodes = []
    parse_errors = 0
    for lineno, node, kv, problems in parse_show_file(path):
        if problems or node is None:
            parse_errors += 1
        all_nodes.append((lineno, node, kv, problems))
    return all_nodes, parse_errors


def read_stat(desk):
    got = desk.describe("/$stat", with_values=True)
    if got is None or got.get("overflow"):
        return None, None
    p = got["params"]
    return p.get("date", {}).get("value"), p.get("time", {}).get("value")


def read_active_lib(desk):
    got = desk.describe("/$ctl/lib", with_values=True)
    if got is None or got.get("overflow"):
        return None
    return got["params"].get("$active", {}).get("value")


# --------------------------------------------------------------- report

class IncrementalReport:
    """Opens the report file and writes the header immediately at
    construction; write_node() appends and flushes one node's line as it
    lands; write_footer() closes it out with totals.

    This is criterion 2 of the design doc: a worker killed by anything other
    than a terminal Ctrl-C - which includes the connector restarting on a
    version bump - used to leave nothing on disk at all, because the old
    write_report() built the whole file at the end from an in-memory list.
    A file with no footer is now a run that died, and it still names every
    node that landed."""

    def __init__(self, show_file, dry_run, header_meta, suffix=None):
        outdir = os.path.dirname(os.path.abspath(show_file))
        stem = os.path.splitext(os.path.basename(show_file))[0]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        mode = "dryrun" if dry_run else "push"
        tag = ("_%s" % suffix) if suffix else ""
        self.path = os.path.join(outdir, "%s_%s-report_%s%s.txt" % (stem, mode, stamp, tag))
        self.dry_run = dry_run
        self._f = open(self.path, "w", encoding="utf-8")
        self._write_header(show_file, header_meta)

    def _flush_to_disk(self):
        self._f.flush()
        try:
            os.fsync(self._f.fileno())
        except OSError:
            pass  # not every filesystem/handle supports fsync; flush is the floor

    def _write_header(self, show_file, m):
        lines = [
            "# %s report for %s" % ("DRY-RUN" if self.dry_run else "PUSH", show_file),
            "# console %s (%s)  firmware live=%s profile=%s%s" % (
                m["console"], m["host"], m["firmware_live"], m["firmware_profile"],
                "  *** MISMATCH ***" if m.get("firmware_mismatch") else ""),
            "# desk clock before: %s %s" % (m["before_date"], m["before_time"]),
            "# active snapshot before: %r  (known to lag a manual recall)" % m["active_before"],
            "# Nodes are appended below as this run processes them, flushed to disk",
            "# immediately after each one. Anything this run did NOT land says so",
            "# on that node's own line. A file with no '# FOOTER' section below is",
            "# a run that stopped before finishing for any reason - everything",
            "# above this point is still exactly what landed.",
            "",
        ]
        self._f.write("\n".join(lines) + "\n")
        self._flush_to_disk()

    def write_node(self, entry):
        status_prefix = "WOULD-" if self.dry_run else ""
        w = ", ".join("%s=%s" % (k, v) for k, v in sorted(entry.get("written", {}).items()))
        s = ", ".join("%s: %s" % (k, v) for k, v in sorted(entry.get("skipped", {}).items()))
        p = ", ".join(sorted(entry.get("partial") or []))
        mc = ""
        if entry.get("model_change"):
            mc = "  model %s->%s" % (entry["model_change"]["from"], entry["model_change"]["to"])
        warn = "  WARNING: " + " | ".join(entry["warnings"]) if entry.get("warnings") else ""
        lines = ["line %-6s %-30s %-14s %s%s" % (
            entry.get("line", ""), entry.get("node") or "(unparsed)",
            status_prefix + entry.get("status", "?").upper(), entry.get("detail", ""), mc)]
        if w:
            lines.append("           written: %s" % w)
        if s:
            lines.append("           skipped: %s" % s)
        if p:
            lines.append("           partial (live key(s) not in file, left as-is): %s" % p)
        if warn:
            lines.append("     " + warn)
        self._f.write("\n".join(lines) + "\n")
        self._flush_to_disk()

    def write_footer(self, m):
        c = m["counts"]
        lines = [
            "",
            "# FOOTER -- run completed",
            "# desk clock after: %s %s" % (m["after_date"], m["after_time"]),
            "# active snapshot after: %r" % m["active_after"],
            "# WALL-CLOCK ELAPSED: %.1f seconds (%.2f minutes)" % (
                m["wall_elapsed"], m["wall_elapsed"] / 60.0),
            "# %d node line(s): written=%d partial=%d skipped=%d incomplete=%d failed=%d parse_errors=%d" % (
                m["total"], c.get("written", 0), c.get("partial", 0), c.get("skipped", 0),
                c.get("incomplete", 0), c.get("failed", 0), c.get("parse_error", 0)),
        ]
        if m.get("interrupted"):
            lines.append("# STOPPED EARLY: %s" % m.get("stop_reason", "interrupted"))
        self._f.write("\n".join(lines) + "\n")
        self._flush_to_disk()

    def close(self):
        try:
            self._f.close()
        except Exception:
            pass


# ---------------------------------------------------------------- push job

class PushJob:
    """One background whole-scene push, triggered by wing_push_file and
    polled by wing_push_status (design doc: "wing_push_file starts a
    background worker and returns a job id straight away").

    Retry is a whole-push RESTART, not a per-node one - up to max_attempts
    full passes over the show file, each producing its own IncrementalReport
    (report_path always points at the current or most recently finished
    attempt). Three conditions stop a job early rather than spending another
    full pass:

      - the same node fails twice with the same failure_class(). Necessarily
        a CROSS-ATTEMPT comparison: within one attempt a node is touched
        exactly once (no per-node retry), so this can only be observed on a
        node's second attempt at earliest - see the fail_seen dict in
        _run_attempts, deliberately declared outside the per-attempt loop.
      - twenty consecutive node failures in one attempt (the desk being gone,
        not a value being wrong - a per-node rule can't catch this because
        every failure lands on a different address). This one resets each
        attempt: a genuinely gone desk fails every node and hits 20 well
        inside attempt 1, long before a restart would matter.
      - max_attempts attempts complete without a clean run (zero failures)

    State lives on the instance so wing_push_status can read it without
    touching the desk; `_lock` only guards the fields a status call reads
    concurrently with the worker thread writing them."""

    MAX_ATTEMPTS = 3
    CONSECUTIVE_FAIL_LIMIT = 20

    def __init__(self, job_id, desk, profile, show_file, dry_run,
                 console_name, host):
        self.job_id = job_id
        self.desk = desk
        self.profile = profile
        self.show_file = show_file
        self.dry_run = dry_run
        self.console_name = console_name
        self.host = host

        self.state = "running"       # running | done | stopped_early | failed
        self.attempt = 0
        self.max_attempts = self.MAX_ATTEMPTS
        self.nodes_done = 0
        self.nodes_total = 0
        self.failed_count = 0
        self.incomplete_count = 0
        self.partial_count = 0
        self.report_path = None
        self.started = time.time()
        self.finished = None
        self.stop_reason = None
        self.error = None

        self._lock = threading.Lock()
        self._thread = None

    def snapshot(self):
        """Everything wing_push_status returns: state, nodes done/total,
        elapsed seconds, a running failure count, a running partial count
        (nodes written with one or more live keys left as-is because the
        file omitted them - see _completeness_missing), and the report
        path. Never the per-node records themselves - the report file is
        that record."""
        with self._lock:
            elapsed = (self.finished or time.time()) - self.started
            return {
                "job_id": self.job_id,
                "state": self.state,
                "attempt": self.attempt,
                "max_attempts": self.max_attempts,
                "nodes_done": self.nodes_done,
                "nodes_total": self.nodes_total,
                "elapsed_seconds": round(elapsed, 1),
                "failed_count": self.failed_count,
                "incomplete_count": self.incomplete_count,
                "partial_count": self.partial_count,
                "report_path": self.report_path,
                "stop_reason": self.stop_reason,
                "error": self.error,
                "show_file": self.show_file,
                "dry_run": self.dry_run,
            }

    def is_running(self):
        with self._lock:
            return self.state == "running"

    def start(self):
        self._thread = threading.Thread(
            target=self._run, name="wing-push-%s" % self.job_id, daemon=True)
        self._thread.start()

    def _set(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def _run(self):
        try:
            self._run_attempts()
        except Exception as exc:
            self._set(state="failed", error="internal error: %s" % exc,
                      finished=time.time())

    def _run_attempts(self):
        try:
            all_nodes, _parse_errors = load_show_file(self.show_file)
        except Exception as exc:
            self._set(state="failed", error="reading show file: %s" % exc,
                      finished=time.time())
            return
        self._set(nodes_total=len(all_nodes))

        live_ident = self.desk.identity()
        live_fw = live_ident.get("$firmware")
        firmware_mismatch = bool(live_fw) and not firmware_matches(
            live_fw, self.profile.firmware)

        # (node, failure_class) -> how many attempts it has failed on.
        # Deliberately OUTSIDE the attempt loop: within a single attempt
        # every node is touched exactly once (no per-node retry - "Failure
        # and retry" in the design doc), so "the same node fails twice"
        # can only ever be observed ACROSS a whole-push restart. Resetting
        # this per attempt would make that early-stop condition
        # unreachable.
        fail_seen = {}

        for attempt in range(1, self.max_attempts + 1):
            self._set(attempt=attempt, nodes_done=0, failed_count=0,
                      incomplete_count=0, partial_count=0)

            before_date, before_time = read_stat(self.desk)
            active_before = read_active_lib(self.desk)
            header_meta = {
                "console": self.console_name, "host": self.host,
                "firmware_live": live_fw, "firmware_profile": self.profile.firmware,
                "firmware_mismatch": firmware_mismatch,
                "before_date": before_date, "before_time": before_time,
                "active_before": active_before,
            }
            report = IncrementalReport(self.show_file, self.dry_run, header_meta,
                                       suffix="attempt%d" % attempt)
            self._set(report_path=report.path)

            model_examples = {}
            counts = {"written": 0, "skipped": 0, "failed": 0,
                     "incomplete": 0, "parse_error": 0, "partial": 0}
            consecutive_fail = 0    # resets each attempt - see class docstring
            stop_reason = None
            wall_t0 = time.time()

            for i, (lineno, node, kv, problems) in enumerate(all_nodes, 1):
                if problems or node is None:
                    entry = {"node": node, "line": lineno, "status": "failed",
                             "detail": "parse error: %s" % "; ".join(problems),
                             "written": {}, "skipped": {}, "warnings": []}
                    counts["parse_error"] += 1
                    counts["failed"] += 1
                else:
                    entry = process_node(self.desk, self.profile, node, dict(kv),
                                         self.dry_run, model_examples)
                    entry["line"] = lineno
                    counts[entry["status"]] = counts.get(entry["status"], 0) + 1
                    # A partial node is still counted under its own status
                    # above (almost always "written") - this is an
                    # additional, alongside count, not a separate status.
                    if entry.get("partial"):
                        counts["partial"] += 1

                report.write_node(entry)

                if entry["status"] == "failed":
                    consecutive_fail += 1
                    fc = failure_class(entry)
                    key = (node, fc)
                    fail_seen[key] = fail_seen.get(key, 0) + 1
                    if fail_seen[key] >= 2:
                        stop_reason = ("node %s failed twice with the same "
                                       "failure class (%s)" % (node, fc))
                    elif consecutive_fail >= self.CONSECUTIVE_FAIL_LIMIT:
                        stop_reason = ("%d consecutive node failures" %
                                       self.CONSECUTIVE_FAIL_LIMIT)
                else:
                    consecutive_fail = 0

                self._set(nodes_done=i, failed_count=counts["failed"],
                          incomplete_count=counts["incomplete"],
                          partial_count=counts["partial"])

                if stop_reason:
                    break

                time.sleep(NODE_PACE_S)

            after_date, after_time = read_stat(self.desk)
            active_after = read_active_lib(self.desk)
            wall_elapsed = time.time() - wall_t0
            report.write_footer({
                "after_date": after_date, "after_time": after_time,
                "active_after": active_after, "wall_elapsed": wall_elapsed,
                "counts": counts, "total": len(all_nodes),
                "interrupted": bool(stop_reason), "stop_reason": stop_reason,
            })
            report.close()

            clean = (not stop_reason) and counts["failed"] == 0
            if clean:
                self._set(state="done", finished=time.time(), stop_reason=None)
                return
            if stop_reason:
                self._set(state="stopped_early", finished=time.time(),
                          stop_reason=stop_reason)
                return
            # This attempt finished (every node processed) without a clean
            # run and without an early-stop condition - retry from the top,
            # unless this was the last attempt.

        self._set(state="stopped_early", finished=time.time(),
                  stop_reason="%d attempts completed without a clean run" %
                  self.max_attempts)
