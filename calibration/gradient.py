"""Find where a photodiode's response to position is steepest (numpy only, no Qt).

A scan is the diode's mean voltage at a series of positions x, in whatever unit the stage reads
(µm, mm, or micrometer ticks; only differences matter). The slope is found without assuming a
response shape: finite differences pick the steepest stretch, then a cubic fitted to the points
around it places the peak between scan points (a cubic's slope is largest at its inflection).

Voltages are in mV throughout.
"""

from dataclasses import dataclass, field

import numpy as np

CHUNKS = 10  # the record is split into this many pieces to estimate the mean's uncertainty
HALF_WINDOW = 2  # extra points on each side of the steepest segment that the cubic is fitted to
OTHER_FLANK = 0.5  # report an opposite-sign flank whose slope is at least this fraction of the best
NOISE_SIGMA = 3.0  # the steepest step should stand this far above the point uncertainties


@dataclass
class ScanPoint:
    x: float  # position relative to the scan origin
    mean_mv: float
    err_mv: float  # 1-sigma uncertainty of the mean
    noise_mv: float  # rms of the record about its mean


@dataclass
class Flank:
    x: float  # position of the steepest slope
    slope: float  # mV per unit at x
    refined: bool  # True when the cubic placed the peak; False means the steepest segment's midpoint
    window: tuple  # (lo, hi) positions the estimate used


@dataclass
class GradientResult:
    x: np.ndarray  # unique positions, sorted
    v: np.ndarray  # mean mV at each
    err: np.ndarray
    mid: np.ndarray  # segment midpoints
    slope: np.ndarray  # finite-difference slope of each segment, mV per unit
    best: Flank
    other: Flank = None  # steep flank of the opposite sign, e.g. the far side of a shadow
    notes: list = field(default_factory=list)


def measure_point(x, voltage_v):
    """ScanPoint from one voltage record (V) taken at position x."""
    v = np.asarray(voltage_v, float) * 1e3
    chunk_means = np.array([c.mean() for c in np.array_split(v, CHUNKS)])
    err = chunk_means.std(ddof=1) / np.sqrt(CHUNKS)
    return ScanPoint(float(x), float(v.mean()), float(err), float(v.std()))


def combine(points):
    """Unique sorted positions with repeat visits averaged (weighted by their uncertainties)."""
    groups = {}
    for p in points:
        groups.setdefault(round(p.x, 9), []).append(p)
    x = np.array(sorted(groups))
    v, err = [], []
    for key in x:
        ps = groups[key]
        w = np.array([1 / max(p.err_mv, 1e-12) ** 2 for p in ps])
        v.append(float(np.sum(w * [p.mean_mv for p in ps]) / w.sum()))
        err.append(float(1 / np.sqrt(w.sum())))
    return x, np.array(v), np.array(err)


def _flank(x, v, i):
    """Steepest-slope position near segment i (between x[i] and x[i+1])."""
    lo, hi = max(0, i - HALF_WINDOW), min(len(x), i + 2 + HALF_WINDOW)
    mid = 0.5 * (x[i] + x[i + 1])
    fallback = Flank(float(mid), float((v[i + 1] - v[i]) / (x[i + 1] - x[i])), False, (float(x[i]), float(x[i + 1])))
    if hi - lo < 4:
        return fallback
    xc, scale = mid, np.ptp(x[lo:hi]) / 2
    c3, c2, c1, _ = np.polyfit((x[lo:hi] - xc) / scale, v[lo:hi], 3)
    if abs(c3) < 1e-12:
        return fallback
    u = -c2 / (3 * c3)
    slope = (3 * c3 * u**2 + 2 * c2 * u + c1) / scale
    # The inflection must be a peak of |slope| (not a trough) and stay inside the steepest segment's neighbors.
    inside = x[max(lo, i - 1)] <= xc + u * scale <= x[min(hi - 1, i + 2)]
    if np.sign(slope) != -np.sign(c3) or np.sign(slope) != np.sign(fallback.slope) or not inside:
        return fallback
    return Flank(float(xc + u * scale), float(slope), True, (float(x[lo]), float(x[hi - 1])))


def analyze(points):
    """GradientResult for a scan; needs at least three distinct positions."""
    x, v, err = combine(points)
    if len(x) < 3:
        raise ValueError(f"Need at least 3 distinct positions, have {len(x)}")
    slope = np.diff(v) / np.diff(x)
    mid = 0.5 * (x[1:] + x[:-1])
    i = int(np.argmax(np.abs(slope)))
    best = _flank(x, v, i)

    other = None
    opposite = np.flatnonzero(np.sign(slope) == -np.sign(slope[i]))
    if opposite.size:
        j = int(opposite[np.argmax(np.abs(slope[opposite]))])
        if abs(slope[j]) >= OTHER_FLANK * abs(slope[i]):
            other = _flank(x, v, j)

    notes = []
    if i in (0, len(slope) - 1):
        edge = x[0] if i == 0 else x[-1]
        notes.append(f"The steepest segment is at the end of the scan (x = {edge:g}): the slope may keep rising "
                     "past it. Extend the scan in that direction to be sure this is the peak.")
    step = abs(v[i + 1] - v[i])
    if step < NOISE_SIGMA * np.hypot(err[i], err[i + 1]):
        notes.append(f"The steepest change ({step:.3f} mV) is within {NOISE_SIGMA:.0f}σ of the point noise: "
                     "use a bigger step, or Collect again at the same positions to average.")
    if not best.refined:
        notes.append("Peak placed at the steepest segment's midpoint (too few points or no clear peak for the "
                     "cubic): it is only good to about half a step. Add points around it with a finer step.")
    return GradientResult(x, v, err, mid, slope, best, other, notes)


def cubic_curve(result, flank, n=100):
    """Points of the cubic used for a flank, for plotting (None if it was not refined)."""
    if not flank.refined:
        return None
    lo, hi = flank.window
    sel = (result.x >= lo) & (result.x <= hi)
    coef = np.polyfit(result.x[sel] - flank.x, result.v[sel], 3)
    xs = np.linspace(lo, hi, n)
    return xs, np.polyval(coef, xs - flank.x)
