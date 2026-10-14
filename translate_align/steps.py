"""Find and measure the two PMQ steps in one photodiode trace (numpy only, no Qt).

A trace is the running kick integral: a baseline, then one step as the wave passes each magnet,
plus the bench's pickup tone (~30.9 kHz fundamental with harmonics). Locating the steps (times and
edge widths) is done once, on a capture where the wire is clearly off-center. After that the steps
are measured with those times locked, which is a linear least-squares fit and keeps working as the
steps shrink toward zero, when there is nothing left to locate.

Times are in µs after the trigger and voltages in mV throughout.
"""

from dataclasses import dataclass

import numpy as np

TONE_HZ = 30.86e3  # pickup fundamental seen on the bench; refined on every trace
TONE_SEARCH = 0.03  # refine within ±3% of TONE_HZ
TONE_HARMONICS = 4
MIN_GAP_US, MAX_GAP_US = 40.0, 400.0  # allowed spacing between the two steps
PRE_US, POST_US = 400.0, 600.0  # fit window around the steps
EDGE_WIDTHS_US = (10, 15, 20, 30, 45, 60, 80, 110, 150, 200, 260)  # candidate 10-90% edge widths
TRIGGER_GUARD_US = 50.0  # ignore the pulse pickup spike at t = 0
DETECT_SIGMA = 5.0  # an edge must stand this far above the noise to be located

_TANH_10_90 = 2 * np.arctanh(0.8)  # 10-90% width of 0.5 * (1 + tanh(x / tau)), in units of tau


class StepError(Exception):
    pass


@dataclass
class StepModel:
    """Where the two steps sit on one channel. Locked after locating; reused for every later fit."""

    t1: float  # first step's center, µs
    w1: float  # first step's 10-90% width, µs
    t2: float
    w2: float
    lo: float  # fit window, µs
    hi: float


@dataclass
class StepFit:
    s1: float  # first step's height, mV (positive = up)
    s2: float
    e1: float  # 1-sigma fit uncertainty, mV
    e2: float
    resid_uv: float  # rms of what the model leaves, µV
    tone_hz: float
    t: np.ndarray  # fit window, for plotting
    clean: np.ndarray  # data with the tone removed
    model: np.ndarray  # baseline + drift + both steps


def _us_mv(trace):
    return trace.time * 1e6, trace.voltage * 1e3


def _edge(t, t0, width):
    return 0.5 * (1 + np.tanh((t - t0) * _TANH_10_90 / width))


def _harmonics(t_us, f0):
    phase = 2e-6 * np.pi * f0 * t_us
    return np.column_stack([fn(k * phase) for k in range(1, TONE_HARMONICS + 1) for fn in (np.sin, np.cos)])


def refine_tone(t_us, y, f0=TONE_HZ):
    """Fundamental (Hz) near f0 that maximizes the summed power of its harmonics."""
    y = (y - np.polyval(np.polyfit(t_us, y, 1), t_us)) * np.hanning(len(y))

    def best(freqs):
        phase = 2e-6 * np.pi * np.outer(freqs, t_us)
        power = sum(np.abs(np.exp(-1j * k * phase) @ y) ** 2 for k in range(1, TONE_HARMONICS + 1))
        return freqs[np.argmax(power)]

    coarse = best(np.arange(f0 * (1 - TONE_SEARCH), f0 * (1 + TONE_SEARCH), 10.0))
    return float(best(np.arange(coarse - 10, coarse + 10.5, 1.0)))


def _remove_tone(t_us, y, f0):
    """Data minus the tone, fitted over the whole record next to a cubic trend."""
    tc = (t_us - t_us.mean()) / np.ptp(t_us)
    H = _harmonics(t_us, f0)
    X = np.column_stack([np.vander(tc, 4), H])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    return y - H @ coef[4:]


def _candidate_edges(t_us, clean, f0):
    """Times of edges that stand out from the noise, as {time: score in noise sigmas}.

    The change across a tone-period boxcar average is checked at three scales, so both sharp
    (30 µs) and slow (150 µs) edges show up. Candidates only need to include the real steps;
    locate_steps picks the pair by fit quality.
    """
    dt = t_us[1] - t_us[0]
    n = max(1, round(1e6 / f0 / dt))
    smooth = np.convolve(clean, np.ones(n) / n, mode="same")
    radius = max(1, round(MIN_GAP_US / 2 / dt))
    found = {}
    for lag in (n, 2 * n, 4 * n):
        change = np.zeros_like(smooth)
        change[lag:-lag] = smooth[2 * lag :] - smooth[: -2 * lag]
        change[: lag + n] = change[-(lag + n) :] = 0  # boxcar edge effects
        change[np.abs(t_us) < TRIGGER_GUARD_US + lag * dt] = 0
        score = np.abs(change) / (1.4826 * np.median(np.abs(change - np.median(change))))
        for i in np.flatnonzero(score > DETECT_SIGMA):
            if score[i] == score[max(0, i - radius) : i + radius + 1].max():
                near = [k for k in found if abs(t_us[k] - t_us[i]) < MIN_GAP_US / 2]
                if all(found[k] < score[i] for k in near):
                    for k in near:
                        del found[k]
                    found[i] = score[i]
    return {float(t_us[i]): float(s) for i, s in found.items()}


def _grid_fit(t_us, y, f0, guess1, guess2, window=None, span=40.0, step=2.0):
    """Best (t1, w1, t2, w2) near the guesses, and the signal power it explains (mV²).
    Baseline, drift and tone are projected out first, so each candidate pair of edges is a 2x2 solve.
    """
    lo, hi = window or (guess1 - PRE_US, guess2 + POST_US)
    lo, hi = max(t_us[0], lo), min(t_us[-1], hi)
    sel = (t_us >= lo) & (t_us <= hi)
    t, yw = t_us[sel], y[sel]
    Q, _ = np.linalg.qr(np.column_stack([np.ones_like(t), (t - lo) / 1e3, _harmonics(t, f0)]))
    project = lambda a: a - (a @ Q) @ Q.T  # noqa: E731

    def edges(center):
        grid = [(t0, w) for t0 in np.arange(center - span, center + span + step / 2, step) for w in EDGE_WIDTHS_US]
        return grid, project(np.array([_edge(t, t0, w) for t0, w in grid]))

    grid1, E1 = edges(guess1)
    grid2, E2 = edges(guess2)
    yp = project(yw)
    g11, g22, g12 = np.einsum("ij,ij->i", E1, E1), np.einsum("ij,ij->i", E2, E2), E1 @ E2.T
    a, b = E1 @ yp, E2 @ yp
    det = g11[:, None] * g22[None, :] - g12**2
    with np.errstate(divide="ignore", invalid="ignore"):
        explained = (g22[None, :] * a[:, None] ** 2 - 2 * g12 * a[:, None] * b[None, :] + g11[:, None] * b[None, :] ** 2) / det
    gap = np.array([t0 for t0, _ in grid2])[None, :] - np.array([t0 for t0, _ in grid1])[:, None]
    explained[(gap < MIN_GAP_US) | ~np.isfinite(explained)] = -np.inf
    i, j = np.unravel_index(np.argmax(explained), explained.shape)
    (t1, w1), (t2, w2) = grid1[i], grid2[j]
    return float(explained[i, j]), StepModel(float(t1), float(w1), float(t2), float(w2), float(lo), float(hi))


def locate_steps(trace, max_pairs=12):
    """Find both steps on a capture where the wire is off-center on this diode's axis."""
    t, y = _us_mv(trace)
    f0 = refine_tone(t, y)
    edges = _candidate_edges(t, _remove_tone(t, y, f0), f0)
    pairs = sorted(
        ((s1 + s2, t1, t2) for t1, s1 in edges.items() for t2, s2 in edges.items() if MIN_GAP_US <= t2 - t1 <= MAX_GAP_US),
        reverse=True,
    )[:max_pairs]
    if not pairs:
        raise StepError(
            f"{trace.channel}: found {len(edges)} clear edge(s), need two {MIN_GAP_US:.0f}-{MAX_GAP_US:.0f} µs apart. "
            "Move the wire off-center on this axis (or check the timebase) and locate again."
        )
    # Compare candidate pairs on one shared window, then refit the winner on its own window.
    window = (min(p[1] for p in pairs) - PRE_US, max(p[2] for p in pairs) + POST_US)
    _, guess1, guess2 = max(pairs, key=lambda p: _grid_fit(t, y, f0, p[1], p[2], window)[0])
    return _grid_fit(t, y, f0, guess1, guess2)[1]


def measure_steps(trace, model):
    """Both step heights with the step times locked to `model`."""
    t, y = _us_mv(trace)
    if model.lo < t[0] or model.hi > t[-1]:
        raise StepError(f"{trace.channel}: the record no longer covers the steps (timebase or delay changed?)")
    f0 = refine_tone(t, y)
    sel = (t >= model.lo) & (t <= model.hi)
    t, y = t[sel], y[sel]
    H = _harmonics(t, f0)
    X = np.column_stack([np.ones_like(t), (t - model.lo) / 1e3, _edge(t, model.t1, model.w1), _edge(t, model.t2, model.w2), H])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    sigma2 = resid @ resid / (len(t) - X.shape[1])
    err = np.sqrt(sigma2 * np.diag(np.linalg.inv(X.T @ X)))
    return StepFit(
        s1=float(coef[2]), s2=float(coef[3]), e1=float(err[2]), e2=float(err[3]),
        resid_uv=float(np.sqrt(sigma2) * 1e3), tone_hz=f0,
        t=t, clean=y - H @ coef[4:], model=X[:, :4] @ coef[:4],
    )
