"""Find where a magnet's kick crosses zero along one stage axis (numpy only, no Qt).

A scan moves one stage axis through a series of positions. At each one, a single capture gives each
magnet's step (its kick) on both photodiodes, measured with the step times locked
(translate_align.steps). For a quadrupole the kick is linear in the wire's offset from the magnetic
center, so kick vs. position is a straight line, and where it crosses zero is the position that puts
that magnet's center on the wire along this axis.

Positions are in whatever unit the stage reads (µm, mm, ticks, turns); only differences matter.
Kicks are in mV.
"""

from dataclasses import dataclass, field

import numpy as np

MAX_RATE = 5e6  # traces are block-averaged down to this sample rate before fitting (edges are 15-60 µs wide)
DISAGREE_SIGMA = 3.0  # flag when the two diodes' zero crossings differ by more than this
SCATTER_CHI2 = 4.0  # flag when the points scatter this much more than their error bars
EXTRAPOLATE = 0.1  # flag a zero crossing further past the end of the scan than this fraction of its span


def decimate(trace, max_rate=MAX_RATE):
    """Block-average a trace down to about max_rate. Long records (700k points) are slow to fit."""
    dt = trace.time[1] - trace.time[0]
    k = int(1 / (dt * max_rate))
    if k <= 1:
        return trace
    n = len(trace.time) // k * k
    avg = lambda a: a[:n].reshape(-1, k).mean(axis=1)  # noqa: E731
    return type(trace)(trace.channel, avg(trace.time), avg(trace.voltage), trace.metadata)


@dataclass
class LineFit:
    slope: float  # mV per unit
    slope_err: float
    x0: float  # where the line crosses zero; nan if the slope is zero
    x0_err: float
    chi2: float  # chi-square per degree of freedom; nan with only two positions
    n: int

    @property
    def significant(self):
        """True when the slope is clearly non-zero, so the zero crossing means something."""
        return np.isfinite(self.x0) and abs(self.slope) > 3 * self.slope_err


def fit_line(x, y, err):
    """Weighted straight-line fit and its zero crossing. Needs two distinct positions.

    When the points scatter more than their error bars (chi2 > 1), the uncertainties are scaled up by
    sqrt(chi2), so a bad capture shows up as a larger error instead of a falsely precise answer.
    """
    x, y, err = (np.asarray(a, float) for a in (x, y, err))
    if np.unique(x).size < 2:
        raise ValueError("Need at least two different positions to fit a line")
    w = 1 / np.maximum(err, 1e-9) ** 2
    xm = np.sum(w * x) / w.sum()
    u = x - xm  # centred, so intercept and slope are uncorrelated
    a = np.sum(w * y) / w.sum()
    b = np.sum(w * u * y) / np.sum(w * u**2)
    var_a, var_b = 1 / w.sum(), 1 / np.sum(w * u**2)
    dof = len(x) - 2
    chi2 = float(np.sum(w * (y - a - b * u) ** 2) / dof) if dof > 0 else float("nan")
    scale = max(1.0, chi2) if dof > 0 else 1.0
    if b == 0:
        return LineFit(0.0, float(np.sqrt(scale * var_b)), float("nan"), float("nan"), chi2, len(x))
    x0 = xm - a / b
    x0_err = np.sqrt(scale * (var_a / b**2 + a**2 * var_b / b**4))
    return LineFit(float(b), float(np.sqrt(scale * var_b)), float(x0), float(x0_err), chi2, len(x))


@dataclass
class ScanPoint:
    x: float
    steps: dict  # magnet -> (S, err): 2-vectors over the diode roles, mV (translate_align.alignment.step_vectors)


@dataclass
class ScanResult:
    fits: dict  # magnet -> {role: LineFit}
    target: str  # the magnet this stage centers
    role: str  # the diode used for the answer: the one that pins the zero crossing best
    x0: float
    x0_err: float
    notes: list = field(default_factory=list)


def _best_role(fits):
    usable = {role: f for role, f in fits.items() if f.significant}
    return min(usable, key=lambda role: usable[role].x0_err) if usable else None


def analyze(points, roles, target):
    """Line fit of every magnet's kick on every diode, and the target magnet's zero crossing."""
    x = np.array([p.x for p in points])
    fits = {}
    for magnet in points[0].steps:
        fits[magnet] = {}
        for k, role in enumerate(roles):
            S = [p.steps[magnet][0][k] for p in points]
            E = [p.steps[magnet][1][k] for p in points]
            fits[magnet][role] = fit_line(x, S, E)

    notes = []
    role = _best_role(fits[target])
    if role is None:
        return ScanResult(fits, target, None, float("nan"), float("nan"),
                          [f"{target}'s kick shows no clear slope on either diode: widen the scan (bigger step), "
                           "or check that the steps are still locked (look at the last capture)."])
    best = fits[target][role]

    lo, hi = x.min(), x.max()
    past = max(best.x0 - hi, lo - best.x0)
    if past > max(EXTRAPOLATE * (hi - lo), 2 * best.x0_err):
        notes.append(f"The zero crossing is {past:.3g} past the end of the scan, so it is extrapolated. "
                     "Move there and add points on both sides of it to confirm.")
    if np.isfinite(best.chi2) and best.chi2 > SCATTER_CHI2:
        notes.append(f"The points scatter {np.sqrt(best.chi2):.1f}× more than their error bars: look for a bad "
                     "capture (Undo last) or a bent line (the scan is too wide; scan closer to the zero).")
    other = [r for r in roles if r != role and fits[target][r].significant]
    for r in other:
        f = fits[target][r]
        if abs(f.x0 - best.x0) > DISAGREE_SIGMA * np.hypot(f.x0_err, best.x0_err):
            notes.append(f"{r} puts the zero at {f.x0:.4g} ± {f.x0_err:.2g}, away from {role}'s. The wire is "
                         "probably off center along the other axis too: center that axis, then rescan this one.")
    return ScanResult(fits, target, role, best.x0, best.x0_err, notes)
