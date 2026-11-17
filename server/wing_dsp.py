#!/usr/bin/env python3
"""Signal analysis for audio captured from the WING's USB return.

Every method here is a published standard or textbook technique,
implemented from its specification (no code copied from other projects):

  loudness      ITU-R BS.1770-4 K-weighting, gating and integrated
                loudness; EBU Tech 3341 momentary/short-term; EBU Tech
                3342 loudness range; true peak by 4x oversampling (BS.1770
                Annex 2). Checked against EBU Tech 3341 test signals.
  correlation   normalised cross-correlation for polarity and arrival
                time between two microphones on one source.
  feedback      long-FFT spectral peaks that stay narrow and prominent
                across frames - ringing tones, not musical notes.
  transfer      dual-channel FFT (H1 estimator, Welch averaging) with
                magnitude-squared coherence: the technique of Smaart and
                Open Sound Meter, comparing what is sent to the PA with
                what a measurement mic hears, independent of the stimulus
                (pink noise or the programme itself).
  smoothing     fractional-octave smoothing of the cross- and auto-spectra
                before forming H, so smoothing keeps the phase meaningful.
  room          impulse response from the transfer function; Schroeder
                backward integration per octave band: EDT, T20, T30, C50,
                C80, D50 (ISO 3382-1).
  eq fit        RBJ "Audio EQ Cookbook" peaking/shelving biquads, fitted
                by bounded least squares to the difference between a
                measurement and a target curve, cuts preferred.
"""

import math

import numpy as np
from scipy import optimize, signal

# --------------------------------------------------------------- loudness

def _kweight_sos(fs):
    """BS.1770 K-weighting for any sample rate, as two biquads derived from
    the standard's analogue prototypes by the bilinear transform (the
    formulation that reproduces the published 48 kHz coefficients exactly):
    a +4 dB high shelf about 1.68 kHz, then the RLB high-pass at 38 Hz."""
    G, Q, fc = 3.99984385397, 0.7071752369554196, 1681.9744509555319
    K = math.tan(math.pi * fc / fs)
    Vh = 10 ** (G / 20.0)
    Vb = Vh ** 0.4996667741545416
    a0 = 1.0 + K / Q + K * K
    shelf = [(Vh + Vb * K / Q + K * K) / a0, 2.0 * (K * K - Vh) / a0,
             (Vh - Vb * K / Q + K * K) / a0, 1.0, 2.0 * (K * K - 1.0) / a0,
             (1.0 - K / Q + K * K) / a0]
    Q2, fc2 = 0.5003270373253953, 38.13547087613982
    K = math.tan(math.pi * fc2 / fs)
    a0 = 1.0 + K / Q2 + K * K
    hp = [1.0, -2.0, 1.0, 1.0, 2.0 * (K * K - 1.0) / a0, (1.0 - K / Q2 + K * K) / a0]
    return np.array([shelf, hp])


def _block_power(y, fs, win_s, hop_s):
    n, h = int(round(win_s * fs)), int(round(hop_s * fs))
    if y.shape[-1] < n:
        return np.zeros((y.shape[0], 0))
    idx = np.arange(0, y.shape[-1] - n + 1, h)
    c = np.cumsum(np.concatenate([np.zeros((y.shape[0], 1)), y ** 2], axis=1), axis=1)
    return (c[:, idx + n] - c[:, idx]) / n


def _lufs(p):
    with np.errstate(divide="ignore"):
        return -0.691 + 10 * np.log10(np.maximum(p, 1e-20))


def loudness(x, fs, weights=None):
    """x: (channels, samples) float in [-1, 1]. Returns integrated, max
    momentary, max short-term, loudness range (LU), true peak (dBTP) and
    sample peak (dBFS), per BS.1770-4 / EBU R128."""
    x = np.atleast_2d(np.asarray(x, dtype=np.float64))
    w = np.ones(x.shape[0]) if weights is None else np.asarray(weights, float)
    y = signal.sosfilt(_kweight_sos(fs), x, axis=-1)
    m = _block_power(y, fs, 0.4, 0.1)          # 400 ms, 75 % overlap
    zm = (w[:, None] * m).sum(0)
    lm = _lufs(zm)
    out = {"seconds": round(x.shape[-1] / fs, 1)}
    if zm.size:
        g1 = lm > -70.0
        if g1.any():
            rel = _lufs(zm[g1].mean()) - 10.0
            g2 = g1 & (lm > rel)
            out["integrated_lufs"] = round(float(_lufs(zm[g2].mean())), 1) if g2.any() else None
        else:
            out["integrated_lufs"] = None
        out["momentary_max_lufs"] = round(float(lm.max()), 1)
    s = _block_power(y, fs, 3.0, 0.1)
    if s.shape[1]:
        ls = _lufs((w[:, None] * s).sum(0))
        out["short_term_max_lufs"] = round(float(ls.max()), 1)
        out["short_term_last_lufs"] = round(float(ls[-1]), 1)
        g = ls[ls > -70.0]
        if g.size:
            g = g[g > _lufs((10 ** ((g + 0.691) / 10)).mean()) - 20.0]
            if g.size > 1:
                out["loudness_range_lu"] = round(float(np.percentile(g, 95) - np.percentile(g, 10)), 1)
    over = signal.resample_poly(x, 4, 1, axis=-1)
    tp = float(np.abs(over).max()) if over.size else 0.0
    sp = float(np.abs(x).max()) if x.size else 0.0
    out["true_peak_dbtp"] = round(20 * math.log10(tp), 1) if tp > 0 else None
    out["sample_peak_dbfs"] = round(20 * math.log10(sp), 1) if sp > 0 else None
    return out


# ------------------------------------------------------------ correlation

def _bandpass(x, fs, lo, hi):
    sos = signal.butter(4, [lo, min(hi, 0.45 * fs)], btype="band", fs=fs, output="sos")
    return signal.sosfiltfilt(sos, x, axis=-1)


def xcorr_delay(a, b, fs, max_ms=30.0, band=(100.0, 5000.0)):
    """Where and with what sign b best matches a. Positive lag: b arrives
    later than a. Returns lag (samples, ms, metres at 343 m/s), the
    normalised correlation at the peak (its sign is the polarity) and how
    clearly that peak stands above the rest."""
    a = _bandpass(np.asarray(a, float), fs, *band)
    b = _bandpass(np.asarray(b, float), fs, *band)
    n = 1 << int(math.ceil(math.log2(len(a) + len(b))))
    r = np.fft.irfft(np.fft.rfft(b, n) * np.conj(np.fft.rfft(a, n)), n)
    norm = math.sqrt(float((a ** 2).sum() * (b ** 2).sum())) or 1.0
    m = int(max_ms * fs / 1000)
    lags = np.concatenate([np.arange(0, m + 1), np.arange(-m, 0)])
    vals = np.concatenate([r[:m + 1], r[-m:]]) / norm
    i = int(np.argmax(np.abs(vals)))
    lag, peak = float(lags[i]), float(vals[i])
    j = (i - 1) % len(vals), (i + 1) % len(vals)
    y0, y1, y2 = abs(vals[j[0]]), abs(vals[i]), abs(vals[j[1]])
    d = y0 - 2 * y1 + y2
    if d != 0:
        lag += 0.5 * (y0 - y2) / d
    others = np.abs(vals).copy()
    others[max(0, i - 5):i + 6] = 0
    clarity = float(abs(peak) / (others.max() or 1e-9))
    lag = float(lag)
    return {"lag_samples": round(lag, 2), "lag_ms": round(lag / fs * 1000, 3),
            "lag_m": round(lag / fs * 343.0, 3), "correlation": round(float(peak), 3),
            "polarity": "inverted" if peak < 0 else "same", "clarity": round(float(clarity), 2)}


# --------------------------------------------------------------- feedback

def ringing_peaks(x, fs, top=5, nfft=32768, fmin=60.0, fmax=16000.0,
                  min_prominence_db=10.0, min_presence=0.6):
    """Narrow tones that persist: in each frame, bins standing well above
    their third-octave neighbourhood; a peak present in most frames at the
    same frequency is ringing (feedback or a resonance being excited),
    where music moves from note to note."""
    x = np.asarray(x, float)
    if x.ndim > 1:
        x = x.mean(0)
    hop = nfft // 4
    if len(x) < nfft:
        nfft = 1 << int(math.log2(max(len(x), 1024)))
        hop = nfft // 4
    f, t, Z = signal.stft(x, fs, window="hann", nperseg=nfft, noverlap=nfft - hop, boundary=None)
    P = 20 * np.log10(np.abs(Z) + 1e-12)
    band = (f >= fmin) & (f <= fmax)
    k = max(3, int(nfft * 0.115 * 0 + 9))
    prom = np.zeros_like(P)
    width = np.maximum(3, (f * (2 ** (1 / 6) - 2 ** (-1 / 6)) / (fs / nfft)).astype(int))
    for fi in np.where(band)[0]:
        w = width[fi]
        lo, hi = max(0, fi - w), min(len(f), fi + w + 1)
        prom[fi] = P[fi] - np.median(P[lo:hi], axis=0)
    ismax = np.zeros_like(P, dtype=bool)
    ismax[1:-1] = (P[1:-1] > P[:-2]) & (P[1:-1] >= P[2:])
    hit = ismax & (prom >= min_prominence_db) & band[:, None]
    presence = hit.mean(axis=1)
    cand = np.where(presence >= min_presence)[0]
    out = []
    for fi in cand:
        y0, y1, y2 = P[fi - 1].mean(), P[fi].mean(), P[fi + 1].mean()
        d = y0 - 2 * y1 + y2
        frac = 0.5 * (y0 - y2) / d if d else 0.0
        freq = (fi + frac) * fs / nfft
        lvl = P[fi].mean()
        half = lvl - 3
        lo = fi
        while lo > 0 and P[lo].mean() > half:
            lo -= 1
        hi = fi
        while hi < len(f) - 1 and P[hi].mean() > half:
            hi += 1
        bw = max((hi - lo) * fs / nfft, fs / nfft)
        out.append({"hz": round(float(freq), 1), "prominence_db": round(float(prom[fi].mean()), 1),
                    "present": round(float(presence[fi]), 2), "level_db": round(float(lvl), 1),
                    "suggested_notch": {"f": round(float(freq), 1), "g": -6.0,
                                        "q": round(float(min(10.0, max(4.0, freq / bw))), 1)}})
    out.sort(key=lambda r: (-r["present"], -r["prominence_db"]))
    merged = []
    for r in out:
        if all(abs(math.log2(r["hz"] / m["hz"])) > 1 / 24 for m in merged):
            merged.append(r)
    return merged[:top], {"resolution_hz": round(fs / nfft, 2), "frames": int(P.shape[1]), "k": k}


# ------------------------------------------------------- transfer function

def log_grid(fmin=20.0, fmax=20000.0, per_octave=48):
    n = int(math.ceil(math.log2(fmax / fmin) * per_octave)) + 1
    return fmin * 2 ** (np.arange(n) / per_octave)


def _smooth(f, X, grid, frac):
    """Average X over a 1/frac-octave window around each grid frequency."""
    half = 2 ** (0.5 / frac)
    c = np.concatenate([[0], np.cumsum(X)])
    lo = np.searchsorted(f, grid / half)
    hi = np.searchsorted(f, grid * half, side="right")
    hi = np.maximum(hi, lo + 1)
    hi = np.minimum(hi, len(f))
    lo = np.minimum(lo, hi - 1)
    return (c[hi] - c[lo]) / (hi - lo)


def transfer_function(ref, mic, fs, nfft=32768, frac=24, grid=None, max_delay_s=0.5):
    """Dual-FFT transfer function from the signal sent to the PA (ref) to
    what the measurement mic hears (mic). The mic is first aligned by the
    broadband arrival time so the averaging windows overlap; the remaining
    delay is reported, not left in the phase.

    Returns delay, and on a log-frequency grid: magnitude (dB), phase
    (degrees, delay-compensated), coherence (0..1) - plus a long-window
    impulse response for room analysis."""
    ref = np.asarray(ref, float)
    mic = np.asarray(mic, float)
    lag = find_arrival(ref, mic, fs, max_delay_s)
    if lag > 0:
        r, m = ref[:-lag], mic[lag:]
    elif lag < 0:
        r, m = ref[-lag:], mic[:lag]
    else:
        r, m = ref, mic
    nfft = min(nfft, 1 << int(math.log2(max(len(r) // 4, 1024))))
    f, Gxy = signal.csd(r, m, fs, window="hann", nperseg=nfft, noverlap=nfft // 2)
    _, Gxx = signal.welch(r, fs, window="hann", nperseg=nfft, noverlap=nfft // 2)
    _, Gyy = signal.welch(m, fs, window="hann", nperseg=nfft, noverlap=nfft // 2)
    if grid is None:
        grid = log_grid(20.0, min(20000.0, 0.45 * fs))
    # Per-bin H1 and coherence first (each averaged over the Welch frames),
    # then fractional-octave smoothing: magnitude and coherence are
    # smoothed as powers, phase from the complex-smoothed H. Smoothing the
    # complex cross-spectrum before forming coherence would cancel the
    # reverberant field's random phase and understate coherence wherever
    # the room is live.
    Hb = Gxy / np.maximum(Gxx, 1e-30)
    cb = np.abs(Gxy) ** 2 / np.maximum(Gxx * Gyy, 1e-30)
    mag = 10 * np.log10(_smooth(f, np.abs(Hb) ** 2, grid, frac) + 1e-24)
    coh = _smooth(f, cb, grid, frac)
    phase = np.degrees(np.angle(_smooth(f, Hb, grid, frac)))
    # Fine residual delay from the impulse response of the raw H.
    Hraw = Gxy / np.maximum(Gxx, 1e-30)
    ir = np.fft.irfft(Hraw)
    k = int(np.argmax(np.abs(ir[: len(ir) // 2])))
    total = lag + k
    return {"grid_hz": grid, "mag_db": mag, "phase_deg": phase, "coherence": np.clip(coh, 0, 1),
            "delay_samples": int(total), "delay_ms": round(float(total) / fs * 1000, 3),
            "distance_m": round(float(total) / fs * 343.0, 2), "polarity_hint": "inverted" if ir[k] < 0 else "same",
            "nfft": nfft, "frames": int(len(r) // (nfft // 2))}


def find_arrival(ref, mic, fs, max_delay_s=0.5):
    """Arrival time of the direct sound, in samples, from the impulse
    response deconvolved with long Welch windows (H1 = Gxy/Gxx). Dividing
    by the reference spectrum whitens it, so the direct sound stands out
    as one sharp peak even where reverberation outweighs it - where plain
    cross-correlation of pink noise in a live room finds a smear and picks
    a reflection."""
    nfft = 1 << int(math.ceil(math.log2(max(2 * max_delay_s * fs, 8192))))
    nfft = min(nfft, 1 << int(math.log2(max(len(ref) // 4, 8192))))
    _, Gxy = signal.csd(ref, mic, fs, window="hann", nperseg=nfft, noverlap=nfft // 2)
    _, Gxx = signal.welch(ref, fs, window="hann", nperseg=nfft, noverlap=nfft // 2)
    ir = np.fft.irfft(Gxy / np.maximum(Gxx, 1e-30))
    m = min(int(max_delay_s * fs), len(ir) // 2)
    seg = np.abs(ir[:m])
    return int(np.argmax(seg))


def impulse_response(ref, mic, fs, length_s=2.0):
    """Long impulse response for room analysis: H estimated with a window of
    at least length_s (Welch), inverse-transformed and rotated so the
    direct sound sits near the start."""
    ref = np.asarray(ref, float)
    mic = np.asarray(mic, float)
    # The analysis window must be well longer than the decay, or each
    # window cuts the tail off and the reverberation reads short.
    nfft = 1 << int(math.ceil(math.log2(2.0 * length_s * fs)))
    nfft = min(nfft, 1 << int(math.log2(max(len(ref) // 3, 4096))))
    _, Gxy = signal.csd(ref, mic, fs, window="hann", nperseg=nfft, noverlap=nfft // 2)
    _, Gxx = signal.welch(ref, fs, window="hann", nperseg=nfft, noverlap=nfft // 2)
    ir = np.fft.irfft(Gxy / np.maximum(Gxx, 1e-30))
    k = int(np.argmax(np.abs(ir)))
    pre = int(0.005 * fs)
    ir = np.roll(ir, -(k - pre))
    return ir[: int(min(len(ir), length_s * fs))]


# ----------------------------------------------------------------- room

OCTAVES = [125, 250, 500, 1000, 2000, 4000, 8000]


def _decay_fit(edc_db, fs, top, bottom):
    i0 = np.argmax(edc_db <= top)
    i1 = np.argmax(edc_db <= bottom)
    if i1 <= i0 + 2 or edc_db[i1] > bottom + 1e-6:
        return None
    t = np.arange(i0, i1) / fs
    slope, _ = np.polyfit(t, edc_db[i0:i1], 1)
    return round(float(-60.0 / slope), 2) if slope < 0 else None


def room_metrics(ir, fs):
    """ISO 3382-1 parameters per octave band from an impulse response:
    EDT, T20, T30 (s), C50, C80 (dB), D50 (%). The tail is cut where the
    decay meets the noise floor (estimated from the last 10 %), which keeps
    T30 honest when the measurement had limited signal to noise."""
    ir = np.asarray(ir, float)
    out = {}
    for fc in OCTAVES + ["broadband"]:
        if fc == "broadband":
            h = ir
        else:
            if fc * 1.41 >= 0.45 * fs:
                continue
            sos = signal.butter(3, [fc / 1.414, fc * 1.414], btype="band", fs=fs, output="sos")
            h = signal.sosfiltfilt(sos, ir)
        e = h ** 2
        k = int(np.argmax(e))
        e = e[k:]
        noise = e[int(0.9 * len(e)):].mean()
        sm = np.convolve(e, np.ones(int(0.01 * fs)) / int(0.01 * fs), mode="same")
        cross = np.argmax(sm <= noise * 2) if (sm <= noise * 2).any() else len(e)
        e = np.maximum(e[:max(cross, int(0.05 * fs))] - noise, 0)
        if e.sum() <= 0:
            continue
        edc = np.cumsum(e[::-1])[::-1]
        edc_db = 10 * np.log10(edc / edc[0] + 1e-30)
        n50, n80 = int(0.05 * fs), int(0.08 * fs)
        early50, early80, total = e[:n50].sum(), e[:n80].sum(), e.sum()
        row = {"EDT_s": _decay_fit(edc_db, fs, 0.0, -10.0),
               "T20_s": _decay_fit(edc_db, fs, -5.0, -25.0),
               "T30_s": _decay_fit(edc_db, fs, -5.0, -35.0),
               "C50_db": round(float(10 * np.log10(early50 / max(total - early50, 1e-30))), 1),
               "C80_db": round(float(10 * np.log10(early80 / max(total - early80, 1e-30))), 1),
               "D50_pct": round(float(100 * early50 / total), 0),
               "dynamic_range_db": round(float(-edc_db[-1]), 0)}
        out[str(fc)] = row
    return out


# --------------------------------------------------------------- targets

def target_curve(grid, name="house", points=None):
    """Target response, 0 dB at 1 kHz.
    flat   : 0 dB everywhere
    house  : -1 dB/octave above 1 kHz, +3 dB shelf below 150 Hz - a common
             speech-and-worship starting point
    live   : -1.5 dB/octave tilt across the band, +6 dB below 100 Hz -
             a typical contemporary live-music curve
    custom : points [[hz, db], ...] interpolated in log frequency"""
    g = np.asarray(grid, float)
    if name == "flat":
        return np.zeros_like(g)
    if name == "custom" and points:
        p = sorted(points)
        return np.interp(np.log2(g), np.log2([x[0] for x in p]), [x[1] for x in p])
    oct_ = np.log2(g / 1000.0)
    if name == "live":
        tilt = -1.5 * oct_
        lf = 6.0 / (1 + (g / 100.0) ** 2)
        return tilt + lf - (6.0 / (1 + 100.0))
    tilt = np.where(oct_ > 0, -1.0 * oct_, 0.0)
    lf = 3.0 / (1 + (g / 150.0) ** 2)
    return tilt + lf


# ------------------------------------------------------------------ eq

def peq_db(f, fc, g, q, fs=48000.0, kind="peq"):
    """Magnitude (dB) at f of an RBJ peaking or shelving biquad."""
    A = 10 ** (g / 40.0)
    w0 = 2 * math.pi * fc / fs
    alpha = math.sin(w0) / (2 * q)
    cw = math.cos(w0)
    if kind == "peq":
        b = [1 + alpha * A, -2 * cw, 1 - alpha * A]
        a = [1 + alpha / A, -2 * cw, 1 - alpha / A]
    elif kind in ("ls", "hs"):
        sA = math.sqrt(A)
        s = 1 if kind == "ls" else -1
        b = [A * ((A + 1) - s * (A - 1) * cw + 2 * sA * alpha), s * 2 * A * ((A - 1) - s * (A + 1) * cw),
             A * ((A + 1) - s * (A - 1) * cw - 2 * sA * alpha)]
        a = [(A + 1) + s * (A - 1) * cw + 2 * sA * alpha, -s * 2 * ((A - 1) + s * (A + 1) * cw),
             (A + 1) + s * (A - 1) * cw - 2 * sA * alpha]
    else:
        raise ValueError(kind)
    _, h = signal.freqz(b, a, worN=np.asarray(f, float), fs=fs)
    return 20 * np.log10(np.abs(h) + 1e-12)


def fit_eq(grid, measured_db, target_db, coherence=None, bands=6, fmin=60.0,
           fmax=16000.0, max_boost=3.0, max_cut=-12.0, fs=48000.0):
    """Parametric bands that move the measurement toward the target.

    The measurement is level-matched to the target over 200 Hz - 5 kHz
    first, so only the shape is corrected. Points with low coherence (the
    mic isn't hearing the PA there - reflections, noise) count less, and
    narrow dips are left alone: a dip a third of an octave wide is usually
    a cancellation that boost can't fill, only make louder elsewhere.
    Bands are placed greedily at the largest weighted error, then refined
    together by bounded least squares. Returns the bands and the predicted
    error before and after (RMS dB over the fitting range)."""
    g = np.asarray(grid, float)
    m = np.asarray(measured_db, float)
    t = np.asarray(target_db, float)
    sel = (g >= fmin) & (g <= fmax)
    ref = (g >= 200) & (g <= 5000)
    m = m - np.mean(m[ref] - t[ref])
    err = m - t                                   # + = too loud -> cut
    w = np.ones_like(g)
    if coherence is not None:
        w = np.clip((np.asarray(coherence) - 0.3) / 0.6, 0.05, 1.0)
    w = w * sel
    # narrow dips: negative error regions narrower than 1/3 octave
    neg = err < -3
    k = 0
    while k < len(g):
        if neg[k]:
            j = k
            while j < len(g) and neg[j]:
                j += 1
            if math.log2(g[min(j, len(g) - 1)] / g[k]) < 1 / 3:
                w[k:j] *= 0.1
            k = j
        else:
            k += 1
    params = []
    resid = err.copy()
    for _ in range(bands):
        score = np.abs(resid) * w
        score[resid < 0] *= 0.6                   # prefer cuts
        i = int(np.argmax(score))
        if score[i] < 1.0:
            break
        gain = float(np.clip(-resid[i], max_cut, max_boost))
        half = abs(resid[i]) / 2
        lo, hi = i, i
        while lo > 0 and abs(resid[lo]) > half and np.sign(resid[lo]) == np.sign(resid[i]):
            lo -= 1
        while hi < len(g) - 1 and abs(resid[hi]) > half and np.sign(resid[hi]) == np.sign(resid[i]):
            hi += 1
        bw = max(math.log2(g[hi] / g[lo]), 1 / 6)
        q = float(np.clip(math.sqrt(2 ** bw) / (2 ** bw - 1), 0.5, 8.0))
        params.append([math.log(g[i]), gain, math.log(q)])
        resid = resid + peq_db(g, g[i], gain, q, fs)

    def model(p):
        tot = np.zeros_like(g)
        for lf, gn, lq in p.reshape(-1, 3):
            tot += peq_db(g, math.exp(lf), gn, math.exp(lq), fs)
        return tot

    if params:
        p0 = np.array(params, float).ravel()
        lo_b = np.tile([math.log(fmin), max_cut, math.log(0.44)], len(params))
        hi_b = np.tile([math.log(fmax), max_boost, math.log(10.0)], len(params))
        p0 = np.clip(p0, lo_b + 1e-9, hi_b - 1e-9)
        sw = np.sqrt(w)
        res = optimize.least_squares(lambda p: (err + model(p)) * sw, p0, bounds=(lo_b, hi_b),
                                     max_nfev=400)
        p = res.x
    else:
        p = np.zeros(0)
    after = err + (model(p) if p.size else 0)
    rms = lambda e: float(np.sqrt(np.sum(w * e ** 2) / max(np.sum(w), 1e-9)))
    bands_out = [{"f": round(float(math.exp(lf)), 1), "g": round(float(gn), 1), "q": round(float(math.exp(lq)), 2)}
                 for lf, gn, lq in p.reshape(-1, 3)]
    bands_out = [b for b in bands_out if abs(b["g"]) >= 0.5]
    bands_out.sort(key=lambda b: b["f"])
    return {"bands": bands_out, "rms_error_before_db": round(rms(err), 2),
            "rms_error_after_db": round(rms(after), 2),
            "corrected_db": after}


def octave_table(grid, mag_db, coherence=None, frac=3):
    """Level per 1/frac octave (centre frequencies 31.5 Hz .. 16 kHz)."""
    centres = 1000 * 2 ** (np.arange(-15, 15) / frac) if frac == 3 else 1000 * 2 ** np.arange(-5, 5)
    centres = centres[(centres >= 31) & (centres <= 16500)]
    rows = []
    g = np.asarray(grid)
    for c in centres:
        s = (g >= c / 2 ** (0.5 / frac)) & (g < c * 2 ** (0.5 / frac))
        if not s.any():
            continue
        r = {"hz": int(round(c)), "db": round(float(np.mean(np.asarray(mag_db)[s])), 1)}
        if coherence is not None:
            r["coherence"] = round(float(np.mean(np.asarray(coherence)[s])), 2)
        rows.append(r)
    return rows
