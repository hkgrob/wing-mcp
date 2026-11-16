#!/usr/bin/env python3
"""Value comparison for read-back checks.

The desk quantises a written value to its nearest legal step and hands back a
display-rounded version (asked fdr=0, got 0.0; asked rel=35, got 36). Every
read-back in this project compares through values_match() rather than by
string equality. Lives in its own module so wing_transport (the leaf-write
read-back) and wing_push_core (the push read-back) share one implementation
without importing each other.
"""

import math
import re


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
